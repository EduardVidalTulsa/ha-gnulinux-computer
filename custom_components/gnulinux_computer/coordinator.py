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
import time
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
    CONF_NOTIFY,
    CONF_STATUS_INTERVAL,
    CONF_UNITS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_UPDATES_AVAILABLE,
    POWER_ON,
    allowed_power_options,
)
from .power import SLOW_INTERVAL, PowerValues, RaplTracker, compute as compute_power
from .registry import check_digests
from .remote import (
    JOBS_RUNNING_SCRIPT,
    PACMAN_PREFLIGHT,
    JobState,
    job_start_script,
    job_status_script,
    last_job_script,
    parse_job_status,
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

JOB_ALL = "all"
JOB_POLL = 15  # seconds between checks of a running install
JOB_TIMEOUT = 4 * 3600

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
        self.rapl = RaplTracker()
        self.power = PowerValues()
        self._last_slow: float | None = None

    async def _async_update_data(self) -> StatusData:
        now = time.monotonic()
        slow = (
            self._last_slow is None
            or now - self._last_slow >= SLOW_INTERVAL
            or self.rapl.wants_quick_read
        )
        try:
            res = await self.client.run(status_script(self.units, slow=slow), timeout=30)
        except (CannotConnect, CommandError) as err:
            raise UpdateFailed(str(err)) from err
        if slow:
            self._last_slow = now
        data = parse_status(str(res.stdout), self.units)
        self.power = compute_power(data.sections, self.rapl, self.power)
        return data

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
        self.last_job: JobState | None = None
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
            await self._refresh_last_job()
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
        script = install_script(target, tool)
        if target in ("pacman", "aur"):
            script = PACMAN_PREFLIGHT + script
        return script

    # ---------------------------------------------------------------- jobs
    # Installs run detached on the computer (see remote.job_start_script), so
    # a restart of Home Assistant or a dropped connection does not kill them.

    async def _start_job(self, job: str, payload: str) -> None:
        try:
            res = await self.client.run(job_start_script(job, payload), timeout=60)
        except (CannotConnect, CommandError) as err:
            raise HomeAssistantError(f"No s'ha pogut començar l'actualització: {err}") from err
        out = str(res.stdout)
        if "@busy" in out:
            busy = out.split("@busy", 1)[1].split()[0]
            raise HomeAssistantError(
                f"Ja hi ha una actualització en marxa a {self.config_entry.title} ({busy})"
            )
        if "@started" not in out:
            raise HomeAssistantError(
                f"No s'ha pogut començar l'actualització: {res.stderr.strip() or out.strip()}"
            )

    async def _wait_job(self, job: str) -> JobState:
        """Poll a detached job until it ends. Tolerates the computer being unreachable."""
        deadline = time.monotonic() + JOB_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(JOB_POLL)
            try:
                res = await self.client.run(job_status_script(job), timeout=30)
            except (CannotConnect, CommandError):
                continue  # rebooting, network blip... keep waiting
            st = parse_job_status(str(res.stdout))
            self.last_job = st
            self.async_update_listeners()
            if st.state != "running":
                return st
        return JobState("timeout", None, "")

    async def _run_job(self, job: str, payload: str, targets: list[str], *, start: bool = True) -> JobState:
        self.installing.update(targets)
        self.async_update_listeners()
        try:
            if start:
                await self._start_job(job, payload)
            return await self._wait_job(job)
        finally:
            self.installing.difference_update(targets)
            self.async_update_listeners()

    async def async_install(self, target: str) -> None:
        """Install updates for a backend key or a container key ("ct:<key>")."""
        if not self.status.online:
            raise HomeAssistantError(f"{self.config_entry.title} no és accessible")
        payload = self._install_command(target)
        try:
            st = await self._run_job(target, payload, [target])
        finally:
            # AUR packages often depend on repo ones; re-check everything.
            await self.async_refresh()
        self._report(st, {target: st.rc})

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
        """Install every pending update, one source after another, in one detached job."""
        if not self.status.online:
            raise HomeAssistantError(f"{self.config_entry.title} no és accessible")
        if self.updating_all:
            raise HomeAssistantError("Ja s'està actualitzant")
        targets = self.pending_targets()
        if not targets:
            return
        parts = []
        for target in targets:
            body = self._install_command(target)
            parts.append(
                f'echo "@@target {target}"\n( {body}\n)\necho "@@rc {target} $?"'
            )
        payload = "\n".join(parts) + "\nexit 0"
        self.updating_all = True
        try:
            st = await self._run_job(JOB_ALL, payload, targets)
        finally:
            self.updating_all = False
            await self.async_refresh()
        self._report(st, _parse_rcs(st.log))

    async def _refresh_last_job(self) -> None:
        try:
            res = await self.client.run(last_job_script(), timeout=30)
        except CommandError:
            return
        out = str(res.stdout)
        self.last_job = parse_job_status(out) if out.strip() else None

    async def async_job_log(self, lines: int) -> JobState | None:
        """Output of the last update job (for the update_log action)."""
        try:
            res = await self.client.run(last_job_script(lines), timeout=60)
        except (CannotConnect, CommandError) as err:
            raise HomeAssistantError(f"{self.config_entry.title} no respon: {err}") from err
        out = str(res.stdout)
        return parse_job_status(out) if out.strip() else None

    async def async_resume_jobs(self) -> None:
        """After a restart of Home Assistant, follow jobs still running on the computer."""
        try:
            res = await self.client.run(JOBS_RUNNING_SCRIPT, timeout=30)
        except (CannotConnect, CommandError):
            return
        for job in str(res.stdout).split():
            targets = self.pending_targets() if job == JOB_ALL else [job.replace("_", ":", 2) if job.startswith("ct_") else job]
            _LOGGER.info("%s: following running job %s", self.config_entry.title, job)
            self.config_entry.async_create_background_task(
                self.hass, self._resume(job, targets), f"{DOMAIN} resume {job}"
            )

    async def _resume(self, job: str, targets: list[str]) -> None:
        if job == JOB_ALL:
            self.updating_all = True
        try:
            st = await self._run_job(job, "", targets, start=False)
        finally:
            self.updating_all = False
            await self.async_refresh()
        try:
            self._report(st, _parse_rcs(st.log) if job == JOB_ALL else {job: st.rc})
        except HomeAssistantError:
            pass  # already notified

    def _report(self, st: JobState, rcs: dict[str, int | None]) -> None:
        """Notify and raise when a job (or part of it) failed."""
        failed = {t: rc for t, rc in rcs.items() if rc not in (0, None)}
        if st.state == "timeout":
            msg = "L'actualització encara no ha acabat després de 4 hores."
        elif st.state == "missing":
            msg = "L'actualització ha desaparegut (s'ha reiniciat l'ordinador?)."
        elif failed or st.rc not in (0, None):
            tail = st.log.strip()[-1500:]
            what = ", ".join(f"{t} (codi {rc})" for t, rc in failed.items()) or f"codi {st.rc}"
            msg = f"Ha fallat: {what}\n\n```\n{tail}\n```"
        else:
            persistent_notification.async_dismiss(
                self.hass, f"{DOMAIN}_{self.config_entry.entry_id}_errors"
            )
            return
        persistent_notification.async_create(
            self.hass,
            msg,
            title=f"Errors actualitzant {self.config_entry.title}",
            notification_id=f"{DOMAIN}_{self.config_entry.entry_id}_errors",
        )
        raise HomeAssistantError(msg.split("\n", 1)[0])


def _parse_rcs(log: str) -> dict[str, int | None]:
    """Exit code per target from the markers of the update-all job."""
    rcs: dict[str, int | None] = {}
    for line in log.splitlines():
        if line.startswith("@@target "):
            rcs.setdefault(line.split(maxsplit=1)[1], None)
        elif line.startswith("@@rc "):
            parts = line.split()
            if len(parts) == 3:
                try:
                    rcs[parts[1]] = int(parts[2])
                except ValueError:
                    rcs[parts[1]] = -1
    return rcs


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
            from .wake import async_wake  # noqa: PLC0415

            await async_wake(self.status.hass, entry)
            self.last_power_target = POWER_ON
            return
        if action not in allowed_power_options(entry.options):
            raise HomeAssistantError(
                f"L'acció '{action}' no està permesa a {entry.title} (opcions de la integració)"
            )
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
