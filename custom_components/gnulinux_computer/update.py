# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Update entities.

One per computer ("System") that installs everything, one source after
another, so Settings shows one line per computer. The per-source and
per-container entities stay available but are disabled by default.
"""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GLConfigEntry
from .const import BACKEND_FIRMWARE, CONF_OS_NAME
from .coordinator import BACKEND_LABELS, INSTALL_ORDER, SYSTEM_BACKENDS, UpdatesCoordinator
from .entity import GLEntity

UP_TO_DATE = "al dia"


async def async_setup_entry(
    hass: HomeAssistant, entry: GLConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up update entities, adding new ones as sources/containers appear."""
    rt = entry.runtime_data
    known: set[str] = set()
    async_add_entities([SystemUpdate(entry, rt)])

    @callback
    def add_new() -> None:
        data = rt.updates.data
        if data is None:
            return
        new: list[UpdateEntity] = []
        for key in data.tools:
            if key not in known:
                known.add(key)
                new.append(PackagesUpdate(entry, rt, key))
        for key, container in data.containers.items():
            if f"ct:{key}" not in known and container.local_digests:
                known.add(f"ct:{key}")
                new.append(ContainerUpdate(entry, rt, key))
        if new:
            async_add_entities(new)

    add_new()
    entry.async_on_unload(rt.updates.async_add_listener(add_new))


class _Base(GLEntity, CoordinatorEntity[UpdatesCoordinator], UpdateEntity):
    _target: str

    def __init__(self, entry: GLConfigEntry, rt, key: str) -> None:
        GLEntity.__init__(self, entry, rt, key)
        CoordinatorEntity.__init__(self, rt.updates)

    @property
    def in_progress(self) -> bool:
        return self._target in self.coordinator.installing

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        await self.coordinator.async_install(self._target)


def _package_table(packages) -> str:
    rows = "\n".join(f"| {p.name} | {p.current or ''} | {p.new or ''} |" for p in packages)
    return f"| Paquet | Actual | Nova |\n|---|---|---|\n{rows}"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


class SystemUpdate(_Base):
    """Everything pending on the computer, installed as one ordered job."""

    _attr_translation_key = "system"
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.RELEASE_NOTES
        | UpdateEntityFeature.PROGRESS
    )

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        super().__init__(entry, rt, "updates_system")
        self._target = "system"
        self._attr_title = entry.data.get(CONF_OS_NAME) or None

    def _pending(self):
        """(sources with packages in install order, installable containers, manual containers)."""
        data = self.coordinator.data
        if data is None:
            return [], [], []
        order = [k for k in INSTALL_ORDER if k in data.backends]
        order += [k for k in data.backends if k not in order and k != BACKEND_FIRMWARE]
        sources = [data.backends[k] for k in order if data.backends[k].packages]
        cts = [c for c in data.containers.values() if c.update_available]
        return (
            sources,
            [c for c in cts if c.install_command],
            [c for c in cts if not c.install_command],
        )

    @property
    def available(self) -> bool:
        """Available once checked."""
        return self.coordinator.data is not None

    @property
    def in_progress(self) -> bool:
        """While "update all" runs (from here or from the button)."""
        return self.coordinator.updating_all

    @property
    def update_percentage(self) -> int | None:
        """Share of the sources already done."""
        job = self.coordinator.last_job
        if not self.coordinator.updating_all or job is None or job.step is None:
            return None
        number, total, _ = job.step
        return int(100 * (number - 1) / total) if total else None

    @property
    def installed_version(self) -> str:
        """Pseudo version."""
        return UP_TO_DATE

    @property
    def latest_version(self) -> str:
        """Count + fingerprint of everything pending (skipping works per set)."""
        sources, auto, manual = self._pending()
        keys = sorted(
            [f"{b.key}:{p.name}={p.new}" for b in sources for p in b.packages]
            + [f"ct:{c.key}:{c.remote_digest}" for c in auto + manual]
        )
        if not keys:
            return UP_TO_DATE
        digest = hashlib.sha1("|".join(keys).encode()).hexdigest()[:6]
        return f"{len(keys)} pendents ({digest})"

    @property
    def release_summary(self) -> str | None:
        """One line: what, in which order."""
        job = self.coordinator.last_job
        if self.coordinator.updating_all and job is not None and job.step is not None:
            number, total, target = job.step
            label = BACKEND_LABELS.get(target, target.removeprefix("ct:"))
            return f"Instal·lant {number}/{total}: {label}"
        sources, auto, manual = self._pending()
        parts = [f"{BACKEND_LABELS.get(b.key, b.key)} {len(b.packages)}" for b in sources]
        if auto or manual:
            parts.append(_plural(len(auto) + len(manual), "contenidor", "contenidors"))
        text = " → ".join(parts)
        return (text[:252] + "...") if len(text) > 255 else (text or None)

    async def async_release_notes(self) -> str | None:
        """Install plan, then the packages of every source."""
        data = self.coordinator.data
        if data is None:
            return None
        sources, auto, manual = self._pending()
        errors = [b for b in data.backends.values() if b.error]
        if not sources and not auto and not manual and not errors:
            return "Cap actualització pendent."
        out: list[str] = []
        steps = [
            f"**{BACKEND_LABELS.get(b.key, b.key)}** — {_plural(len(b.packages), 'paquet', 'paquets')}"
            for b in sources
        ] + [f"**Contenidor {c.name}** — `{c.image}`" for c in auto]
        if steps:
            out.append(
                "*Instal·lar* ho fa tot en una sola tasca a l'ordinador, **una font darrere "
                "l'altra i mai a la vegada**, en aquest ordre:\n\n"
                + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
            )
            if any(b.key in SYSTEM_BACKENDS for b in sources):
                out.append(
                    "Primer els paquets del sistema; si fallen, **s'atura** i no s'instal·la "
                    "res més (ni AUR, ni Flatpak/Snap, ni contenidors). La tasca continua "
                    "encara que Home Assistant es reiniciï."
                )
        if manual:
            out.append(
                "**Cal fer a mà** (contenidors que no són de compose/quadlet): "
                + ", ".join(f"{c.name} (`{c.image}`)" for c in manual)
            )
        fw = data.backends.get(BACKEND_FIRMWARE)
        if fw and fw.packages:
            out.append("El **firmware** (fwupd) no s'inclou: té la seva pròpia entitat.")
        for b in errors:
            out.append(f"**Error comprovant {BACKEND_LABELS.get(b.key, b.key)}:** `{b.error}`")
        for b in sources:
            out.append(f"### {BACKEND_LABELS.get(b.key, b.key)}\n\n{_package_table(b.packages)}")
        if auto or manual:
            rows = "\n".join(
                f"| {c.name} | `{c.image}` | {'sí' if c.install_command else 'no'} |"
                for c in auto + manual
            )
            out.append(f"### Contenidors\n\n| Nom | Imatge | Automàtic |\n|---|---|---|\n{rows}")
        return "\n\n".join(out)

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Everything, in order."""
        if not self.coordinator.pending_targets():
            raise HomeAssistantError(
                "No hi ha res que es pugui instal·lar automàticament (mira les notes)"
            )
        await self.coordinator.async_install_all()


class PackagesUpdate(_Base):
    """Pending updates of one package manager."""

    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: GLConfigEntry, rt, backend: str) -> None:
        """Initialize."""
        super().__init__(entry, rt, f"updates_{backend}")
        self._target = backend
        self._attr_translation_key = backend
        self._attr_title = BACKEND_LABELS.get(backend, backend)
        if backend == BACKEND_FIRMWARE:
            # Not part of "update all": stays visible on its own.
            self._attr_supported_features = UpdateEntityFeature.RELEASE_NOTES
            self._attr_entity_registry_enabled_default = True

    @property
    def _result(self):
        data = self.coordinator.data
        return data.backends.get(self._target) if data else None

    @property
    def available(self) -> bool:
        """Available when this source was checked."""
        return self._result is not None and self._target in self.coordinator.data.tools

    @property
    def installed_version(self) -> str:
        """Pseudo version."""
        return UP_TO_DATE

    @property
    def latest_version(self) -> str:
        """Count + fingerprint of the pending set, so skipping works per set."""
        res = self._result
        if res is None or not res.packages:
            return UP_TO_DATE
        digest = hashlib.sha1(
            "|".join(sorted(f"{p.name}={p.new}" for p in res.packages)).encode()
        ).hexdigest()[:6]
        return f"{len(res.packages)} pendents ({digest})"

    @property
    def release_summary(self) -> str | None:
        """Short list of packages."""
        res = self._result
        if res is None:
            return None
        if res.error:
            return f"Error: {res.error}"[:255]
        names = ", ".join(p.name for p in res.packages)
        return names[:252] + "..." if len(names) > 255 else (names or None)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Error, if any."""
        res = self._result
        if res and res.error:
            return {"error": res.error}
        return None

    async def async_release_notes(self) -> str | None:
        """Markdown table of the pending updates."""
        res = self._result
        if res is None:
            return None
        if res.error:
            return f"**Error:** `{res.error}`"
        if not res.packages:
            return "Cap actualització pendent."
        return _package_table(res.packages)


class ContainerUpdate(_Base):
    """Newer image available for a container."""

    _attr_translation_key = "container"
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: GLConfigEntry, rt, key: str) -> None:
        """Initialize."""
        super().__init__(entry, rt, f"container_{key}")
        self._key = key
        self._target = f"ct:{key}"
        container = rt.updates.data.containers[key]
        self._attr_translation_placeholders = {"name": container.name}

    @property
    def _container(self):
        data = self.coordinator.data
        return data.containers.get(self._key) if data else None

    @property
    def available(self) -> bool:
        """Available while the container exists and was checked."""
        c = self._container
        return c is not None and (c.remote_digest is not None or c.check_error is None)

    @property
    def supported_features(self) -> UpdateEntityFeature:
        """Install only for compose / quadlet-managed containers."""
        c = self._container
        if c is not None and c.install_command:
            return UpdateEntityFeature.INSTALL
        return UpdateEntityFeature(0)

    @property
    def title(self) -> str | None:
        """Image reference."""
        c = self._container
        return c.image if c else None

    @property
    def installed_version(self) -> str | None:
        """Short local digest."""
        c = self._container
        if c is None or not c.local_digest:
            return None
        return c.local_digest.removeprefix("sha256:")[:12]

    @property
    def latest_version(self) -> str | None:
        """Short registry digest."""
        c = self._container
        if c is None:
            return None
        if not c.update_available:
            return self.installed_version
        return (c.remote_digest or "").removeprefix("sha256:")[:12]

    @property
    def release_summary(self) -> str | None:
        """Explain how to update when we can't."""
        c = self._container
        if c is None:
            return None
        if c.check_error:
            return f"Error: {c.check_error}"[:255]
        if c.update_available and not c.install_command:
            return "Imatge nova disponible. Contenidor no gestionat per compose/quadlet: cal recrear-lo a mà."
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Container details."""
        c = self._container
        if c is None:
            return None
        return {
            "engine": c.engine,
            "image": c.image,
            "status": c.status,
            "compose_project": c.labels.get("com.docker.compose.project"),
            "systemd_unit": c.labels.get("PODMAN_SYSTEMD_UNIT"),
        }
