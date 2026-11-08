# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Update entities: one per package source and one per container."""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GLConfigEntry
from .const import BACKEND_FIRMWARE
from .coordinator import BACKEND_LABELS, UpdatesCoordinator
from .entity import GLEntity

UP_TO_DATE = "al dia"


async def async_setup_entry(
    hass: HomeAssistant, entry: GLConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up update entities, adding new ones as sources/containers appear."""
    rt = entry.runtime_data
    known: set[str] = set()

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


class PackagesUpdate(_Base):
    """Pending updates of one package manager."""

    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES

    def __init__(self, entry: GLConfigEntry, rt, backend: str) -> None:
        """Initialize."""
        super().__init__(entry, rt, f"updates_{backend}")
        self._target = backend
        self._attr_translation_key = backend
        self._attr_title = BACKEND_LABELS.get(backend, backend)
        if backend == BACKEND_FIRMWARE:
            self._attr_supported_features = UpdateEntityFeature.RELEASE_NOTES

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
        rows = "\n".join(
            f"| {p.name} | {p.current or ''} | {p.new or ''} |" for p in res.packages
        )
        return f"| Paquet | Actual | Nova |\n|---|---|---|\n{rows}"


class ContainerUpdate(_Base):
    """Newer image available for a container."""

    _attr_translation_key = "container"

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
