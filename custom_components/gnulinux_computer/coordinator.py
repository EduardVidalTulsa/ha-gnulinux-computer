# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Coordinators for the GNU+Linux Computer integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import socket
from typing import TYPE_CHECKING

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import CannotConnect, CommandError, LinuxClient
from .const import (
    CONF_BROADCAST,
    CONF_MAC,
    CONF_NOTIFY,
    CONF_STATUS_INTERVAL,
    CONF_UNITS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_BROADCAST,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_UPDATES_AVAILABLE,
    POWER_ON,
)
from .registry import check_digests
from .remote import (
    CONTAINERS_SCRIPT,
    DETECT_SCRIPT,
    SCREENSHOT_SCRIPT,
    BackendResult,
    Container,
    StatusData,
    check_script,
    detect_backends,
    install_script,
    parse_check,
    parse_containers,
    parse_status,
    power_script,
    status_script,
    unit_command,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

BACKEND_LABELS = {
    "pacman": "pacman",
    "aur": "AUR",
    "dnf": "DNF",
    "zypper": "zypper",
    "apt": "APT",
    "flatpak": "Flatpak",
    "snap": "Snap",
    "firmware": "Firmware (fwupd)",
}

# System packages first, then the AUR (may depend on them), then the rest.
INSTALL_ORDER = ("pacman", "dnf", "zypper", "apt", "aur", "flatpak", "snap")


@dataclass
class UpdatesData:
    """Result of an update check."""

    backends: dict[str, BackendResult] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    containers: dict[str, Container] = field(default_factory=dict)
    checked_at: datetime | None = None

    @property
    def total(self) -> int:
        """Total number of pending updates."""
        return sum(len(b.packages) for b in self.backends.values()) + sum(
            1 for c in self.containers.values() if c.update_available
        )

    def keys(self) -> set[str]:
        """Identity of every pending update (for change detection)."""
        out = {
            f"{b.key}:{p.name}:{p.new}" for b in self.backends.values() for p in b.packages
        }
        out |= {
            f"ct:{c.key}:{c.remote_digest}"
            for c in self.containers.values()
            if c.update_available
        }
        return out


class StatusCoordinator(DataUpdateCoordinator[StatusData]):
    """Polls the fast-changing status."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: LinuxClient) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} status {client.host}",
            update_interval=timedelta(
                seconds=entry.options.get(CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL)
            ),
        )
        self.client = client
        self.units: list[str] = list(entry.options.get(CONF_UNITS, []))

    async def _async_update_data(self) -> StatusData:
        try:
            res = await self.client.run(status_script(self.units), timeout=30)
        except (CannotConnect, CommandError) as err:
            raise UpdateFailed(str(err)) from err
        return parse_status(str(res.stdout), self.units)

    @property
    def online(self) -> bool:
        """Return True if the last poll succeeded."""
        return self.last_update_success and self.data is not None


class UpdatesCoordinator(DataUpdateCoordinator[UpdatesData]):
    """Checks for pending updates (slow)."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: LinuxClient,
        status: StatusCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} updates {client.host}",
            update_interval=timedelta(
                minutes=entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
            ),
        )
        self.client = client
        self.status = status
        self.installing: set[str] = set()
        self.updating_all = False
        self._store: Store[dict] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._known: set[str] | None = None
        self._was_offline = False

    async def _async_update_data(self) -> UpdatesData:
        if self._known is None:
            stored = await self._store.async_load() or {}
            self._known = set(stored.get("known", []))
        if not self.status.online:
            # Machine asleep or off: keep the last known result.
            self._was_offline = True
            if self.data is not None:
                return self.data
            raise UpdateFailed("Computer not reachable")
        self._was_offline = False
        try:
            data = await self._check()
        except CannotConnect as err:
            if self.data is not None:
                return self.data
            raise UpdateFailed(str(err)) from err
        await self._notify(data)
        return data

    @callback
    def status_changed(self) -> None:
        """Re-check soon after the computer comes back online."""
        if self._was_offline and self.status.online:
            self._was_offline = False
            self.hass.async_create_task(self.async_request_refresh())

    async def _check(self) -> UpdatesData:
        detect = await self.client.run(DETECT_SCRIPT, timeout=30)
        tools = detect_backends(str(detect.stdout), self.client.is_root)
        data = UpdatesData(tools=tools, checked_at=dt_util.utcnow())

        async def one(key: str, tool: str) -> BackendResult:
            try:
                res = await self.client.run(check_script(key, tool), timeout=600)
            except CommandError as err:
                return BackendResult(key, error=str(err))
            out = str(res.stdout)
            if not res.ok:
                return BackendResult(
                    key, error=(res.stderr.strip() or out.strip())[-500:] or f"exit {res.exit_status}"
                )
            return BackendResult(key, packages=parse_check(key, out))

        results = await asyncio.gather(*(one(k, t) for k, t in tools.items()))
        data.backends = {r.key: r for r in results}

        try:
            res = await self.client.run(CONTAINERS_SCRIPT, timeout=120)
            containers = parse_containers(str(res.stdout))
        except CommandError as err:
            _LOGGER.warning("Container listing failed on %s: %s", self.client.host, err)
            containers = []
        if containers:
            refs = {c.image for c in containers if c.local_digests}
            digests = await check_digests(async_get_clientsession(self.hass), refs)
            for c in containers:
                found = digests.get(c.image)
                if isinstance(found, Exception):
                    c.check_error = str(found)
                else:
                    c.remote_digest = found
        data.containers = {c.key: c for c in containers}
        return data

    async def _notify(self, data: UpdatesData) -> None:
        keys = data.keys()
        new = keys - (self._known or set())
        notif_id = f"{DOMAIN}_{self.config_entry.entry_id}"
        name = self.config_entry.title
        if not keys:
            persistent_notification.async_dismiss(self.hass, notif_id)
        elif new:
            lines = []
            for b in data.backends.values():
                if b.packages:
                    lines.append(f"- **{BACKEND_LABELS.get(b.key, b.key)}**: {len(b.packages)}")
            cts = [c for c in data.containers.values() if c.update_available]
            if cts:
                lines.append(
                    "- **Contenidors**: " + ", ".join(f"{c.name} ({c.image})" for c in cts)
                )
            self.hass.bus.async_fire(
                EVENT_UPDATES_AVAILABLE,
                {
                    "config_entry_id": self.config_entry.entry_id,
                    "computer": name,
                    "total": data.total,
                    "new": len(new),
                    "summary": {
                        **{k: len(b.packages) for k, b in data.backends.items() if b.packages},
                        **({"containers": len(cts)} if cts else {}),
                    },
                },
            )
            if self.config_entry.options.get(CONF_NOTIFY, True):
                persistent_notification.async_create(
                    self.hass,
                    f"{data.total} actualitzacions pendents a **{name}**:\n\n" + "\n".join(lines),
                    title=f"Actualitzacions disponibles: {name}",
                    notification_id=notif_id,
                )
        if keys != self._known:
            self._known = keys
            await self._store.async_save({"known": sorted(keys)})

    # ------------------------------------------------------------------ actions

    def _install_command(self, target: str) -> str:
        data = self.data
        if target.startswith("ct:"):
            container = data.containers.get(target[3:]) if data else None
            if container is None or not container.install_command:
                raise HomeAssistantError("Aquest contenidor no es pot actualitzar automàticament")
            return container.install_command
        tool = data.tools.get(target) if data else None
        if not tool:
            raise HomeAssistantError(f"Gestor {target} no disponible")
        return install_script(target, tool)

    async def _run_install(self, target: str) -> None:
        """Run one install; raise HomeAssistantError on failure. No refresh."""
        script = self._install_command(target)
        self.installing.add(target)
        self.async_update_listeners()
        try:
            res = await self.client.run(script, timeout=3 * 3600)
        except (CannotConnect, CommandError) as err:
            raise HomeAssistantError(f"Error actualitzant {target}: {err}") from err
        finally:
            self.installing.discard(target)
            self.async_update_listeners()
        if not res.ok:
            tail = (res.stderr.strip() or str(res.stdout).strip())[-800:]
            raise HomeAssistantError(f"L'actualització de {target} ha fallat: {tail}")

    async def async_install(self, target: str) -> None:
        """Install updates for a backend key or a container key ("ct:<key>")."""
        if not self.status.online:
            raise HomeAssistantError(f"{self.config_entry.title} no és accessible")
        try:
            await self._run_install(target)
        finally:
            # AUR packages often depend on repo ones; re-check everything.
            await self.async_refresh()

    def pending_targets(self) -> list[str]:
        """Targets with pending updates, in a safe installation order."""
        data = self.data
        if data is None:
            return []
        targets = [
            key
            for key in INSTALL_ORDER
            if key in data.tools and (b := data.backends.get(key)) and b.packages
        ]
        targets += [
            f"ct:{c.key}"
            for c in data.containers.values()
            if c.update_available and c.install_command
        ]
        return targets

    async def async_install_all(self) -> None:
        """Install every pending update, one source after another."""
        if not self.status.online:
            raise HomeAssistantError(f"{self.config_entry.title} no és accessible")
        if self.updating_all:
            raise HomeAssistantError("Ja s'està actualitzant")
        targets = self.pending_targets()
        if not targets:
            return
        self.updating_all = True
        self.installing.update(targets)
        self.async_update_listeners()
        errors: list[str] = []
        try:
            for target in targets:
                try:
                    await self._run_install(target)
                except HomeAssistantError as err:
                    _LOGGER.error("%s: %s", self.config_entry.title, err)
                    errors.append(str(err))
                    if not self.status.online:
                        break
        finally:
            self.updating_all = False
            self.installing.difference_update(targets)
            await self.async_refresh()
        if errors:
            persistent_notification.async_create(
                self.hass,
                "\n\n".join(errors),
                title=f"Errors actualitzant {self.config_entry.title}",
                notification_id=f"{DOMAIN}_{self.config_entry.entry_id}_errors",
            )
            raise HomeAssistantError("; ".join(e[:200] for e in errors))



@dataclass
class Runtime:
    """Runtime data stored in the config entry."""

    client: LinuxClient
    status: StatusCoordinator
    updates: UpdatesCoordinator
    screenshot: bytes | None = None
    screenshot_at: datetime | None = None
    screenshot_listeners: list[Callable[[], None]] = field(default_factory=list)
    last_power_target: str | None = None

    async def async_screenshot(self) -> bytes:
        """Take a screenshot of the active graphical session."""
        try:
            res = await self.client.run(SCREENSHOT_SCRIPT, timeout=60, binary=True)
        except (CannotConnect, CommandError) as err:
            raise HomeAssistantError(f"No s'ha pogut fer la captura: {err}") from err
        data = res.stdout if isinstance(res.stdout, bytes) else b""
        if not res.ok or not data.startswith(b"\x89PNG"):
            raise HomeAssistantError(
                f"No s'ha pogut fer la captura: {res.stderr.strip() or 'sense sortida'}"
            )
        self.screenshot = data
        self.screenshot_at = dt_util.utcnow()
        for listener in self.screenshot_listeners:
            listener()
        return data

    async def async_power(self, action: str, entry: ConfigEntry) -> None:
        """Run a power action."""
        if action == POWER_ON:
            if self.status.online:
                return
            mac = entry.options.get(CONF_MAC)
            if not mac:
                raise HomeAssistantError("No hi ha cap adreça MAC configurada per a Wake-on-LAN")
            await self.status.hass.async_add_executor_job(
                send_magic_packet, mac, entry.options.get(CONF_BROADCAST, DEFAULT_BROADCAST)
            )
            self.last_power_target = POWER_ON
            return
        if not self.status.online:
            raise HomeAssistantError(f"{entry.title} no és accessible")
        init = self.status.data.init if self.status.data else None
        try:
            res = await self.client.run(power_script(init, action), timeout=30)
        except (CannotConnect, CommandError) as err:
            raise HomeAssistantError(str(err)) from err
        if not res.ok:
            raise HomeAssistantError(res.stderr.strip() or f"exit {res.exit_status}")
        self.last_power_target = action
        await self.client.close()

    async def async_unit(self, unit: str, command: str) -> None:
        """Control a service."""
        init = self.status.data.init if self.status.data else "systemd"
        try:
            res = await self.client.run(unit_command(init, unit, command), timeout=120)
        except (CannotConnect, CommandError) as err:
            raise HomeAssistantError(str(err)) from err
        if not res.ok:
            raise HomeAssistantError(res.stderr.strip() or f"exit {res.exit_status}")
        await self.status.async_request_refresh()


def send_magic_packet(mac: str, broadcast: str) -> None:
    """Send a Wake-on-LAN magic packet."""
    clean = mac.replace(":", "").replace("-", "").replace(".", "")
    if len(clean) != 12:
        raise HomeAssistantError(f"MAC invàlida: {mac}")
    payload = bytes.fromhex("FF" * 6 + clean * 16)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            sock.sendto(payload, (broadcast, port))
