# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Sensors for the GNU+Linux Computer integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import GLConfigEntry
from .coordinator import BACKEND_LABELS, StatusCoordinator, UpdatesCoordinator, UpdatesData
from .const import CONF_PUBLIC_IP
from .entity import GLEntity
from .power import PowerValues
from .remote import StatusData, Volume


@dataclass(frozen=True, kw_only=True)
class StatusSensorDescription(SensorEntityDescription):
    """Describes a status sensor."""

    value: Callable[[StatusData], Any]
    attrs: Callable[[StatusData], dict[str, Any]] | None = None


def _boot_time(s: StatusData) -> datetime:
    boot = dt_util.utcnow() - timedelta(seconds=s.uptime)
    return boot.replace(second=0, microsecond=0)


STATUS_SENSORS = (
    StatusSensorDescription(
        key="failed_units",
        translation_key="failed_units",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: len(s.failed_units),
        attrs=lambda s: {"units": s.failed_units},
    ),
    StatusSensorDescription(
        key="system_state",
        translation_key="system_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda s: s.system_state,
    ),
    StatusSensorDescription(
        key="boot_time",
        translation_key="boot_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=_boot_time,
        attrs=lambda s: {"kernel": s.kernel},
    ),
    StatusSensorDescription(
        key="load_1m",
        translation_key="load_1m",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda s: s.load[0],
        attrs=lambda s: {"load_5m": s.load[1], "load_15m": s.load[2]},
    ),
    StatusSensorDescription(
        key="memory_used",
        translation_key="memory_used",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda s: s.mem_used_pct,
    ),
    StatusSensorDescription(
        key="ip_address",
        translation_key="ip_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda s: s.primary_ip,
        attrs=lambda s: {"addresses": s.addresses},
    ),
    StatusSensorDescription(
        key="services_running",
        translation_key="services_running",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: len(s.services_running) if s.services_running is not None else None,
        attrs=lambda s: {"services": s.services_running},
    ),
    StatusSensorDescription(
        key="services_enabled",
        translation_key="services_enabled",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: len(s.services_enabled) if s.services_enabled is not None else None,
        attrs=lambda s: {
            "services": s.services_enabled,
            "not_running": [
                u for u in (s.services_enabled or []) if u not in set(s.services_running or [])
            ],
        },
    ),
    StatusSensorDescription(
        key="services_disabled",
        translation_key="services_disabled",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: len(s.services_disabled) if s.services_disabled is not None else None,
        attrs=lambda s: {"services": s.services_disabled},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: GLConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up sensors."""
    rt = entry.runtime_data
    entities: list[SensorEntity] = [StatusSensor(entry, rt, d) for d in STATUS_SENSORS]
    entities += [UpdatesTotalSensor(entry, rt), LastCheckSensor(entry, rt), LastJobSensor(entry, rt)]
    if entry.options.get(CONF_PUBLIC_IP, True):
        entities.append(PublicIpSensor(entry, rt))
    async_add_entities(entities)

    known: set[str] = set()

    @callback
    def add_volumes() -> None:
        if not rt.status.online:
            return
        new = [
            VolumeSensor(entry, rt, vol.mount)
            for vol in rt.status.data.volumes
            if vol.mount not in known
        ]
        known.update(v.mount for v in new)
        if new:
            async_add_entities(new)

    add_volumes()
    entry.async_on_unload(rt.status.async_add_listener(add_volumes))

    known_ifaces: set[str] = set()

    @callback
    def add_interfaces() -> None:
        # One sensor per network interface with an address: LAN, Wi-Fi, mobile
        # data (wwan), VPNs (tailscale, wireguard)... not container bridges.
        if not rt.status.online:
            return
        new = [
            InterfaceIpSensor(entry, rt, name)
            for name in rt.status.data.addresses
            if name not in known_ifaces and not name.startswith(_SKIP_IFACES)
        ]
        known_ifaces.update(e.interface for e in new)
        if new:
            async_add_entities(new)

    add_interfaces()
    entry.async_on_unload(rt.status.async_add_listener(add_interfaces))

    added_power: set[str] = set()

    @callback
    def add_power() -> None:
        if not rt.status.online:
            return
        new = [
            PowerSensor(entry, rt, key)
            for key in POWER_KEYS
            if key not in added_power and _power_value(rt.status.power, key)[0] is not None
        ]
        added_power.update(s.key for s in new)
        if new:
            async_add_entities(new)

    add_power()
    entry.async_on_unload(rt.status.async_add_listener(add_power))

    has_battery = False

    @callback
    def add_battery() -> None:
        nonlocal has_battery
        if not has_battery and rt.status.online and rt.status.data.battery_level is not None:
            has_battery = True
            async_add_entities(
                [
                    StatusSensor(
                        entry,
                        rt,
                        StatusSensorDescription(
                            key="battery_level",
                            translation_key="battery_level",
                            device_class=SensorDeviceClass.BATTERY,
                            native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT,
                            value=lambda s: s.battery_level,
                            attrs=lambda s: {"status": s.battery_status},
                        ),
                    )
                ]
            )

    add_battery()
    entry.async_on_unload(rt.status.async_add_listener(add_battery))


class StatusSensor(GLEntity, CoordinatorEntity[StatusCoordinator], SensorEntity):
    """Sensor fed by the status coordinator."""

    entity_description: StatusSensorDescription
    # Service lists can be long: keep them out of the recorder.
    _unrecorded_attributes = frozenset({"services", "not_running", "units"})

    def __init__(self, entry: GLConfigEntry, rt, description: StatusSensorDescription) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, description.key)
        CoordinatorEntity.__init__(self, rt.status)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Available while online."""
        return self.coordinator.online

    @property
    def native_value(self) -> Any:
        """Value."""
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Attributes."""
        if self.entity_description.attrs and self.coordinator.data:
            return self.entity_description.attrs(self.coordinator.data)
        return None


class _UpdatesBase(GLEntity, CoordinatorEntity[UpdatesCoordinator], SensorEntity):
    def __init__(self, entry: GLConfigEntry, rt, key: str) -> None:
        GLEntity.__init__(self, entry, rt, key)
        CoordinatorEntity.__init__(self, rt.updates)

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None


class UpdatesTotalSensor(_UpdatesBase):
    """Number of pending updates (all sources)."""

    _attr_translation_key = "updates"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        super().__init__(entry, rt, "updates_total")

    @property
    def native_value(self) -> int:
        """Total."""
        return self.coordinator.data.total

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Per-source counts and errors."""
        data: UpdatesData = self.coordinator.data
        attrs: dict[str, Any] = {
            BACKEND_LABELS.get(k, k): len(b.packages) for k, b in data.backends.items()
        }
        attrs["Contenidors"] = sum(1 for c in data.containers.values() if c.update_available)
        if errors := {k: b.error for k, b in data.backends.items() if b.error}:
            attrs["errors"] = errors
        return attrs


class LastCheckSensor(_UpdatesBase):
    """When updates were last checked."""

    _attr_translation_key = "last_check"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        super().__init__(entry, rt, "last_update_check")

    @property
    def native_value(self) -> datetime | None:
        """Timestamp."""
        return self.coordinator.data.checked_at


class VolumeSensor(GLEntity, CoordinatorEntity[StatusCoordinator], SensorEntity):
    """Usage of a mounted filesystem."""

    _attr_translation_key = "volume"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, entry: GLConfigEntry, rt, mount: str) -> None:
        """Initialize. '/' keeps the unique id of the old root disk sensor."""
        key = "disk_root_used" if mount == "/" else f"volume_{mount}"
        GLEntity.__init__(self, entry, rt, key)
        CoordinatorEntity.__init__(self, rt.status)
        self.mount = mount
        self._attr_translation_placeholders = {"mount": mount}

    @property
    def _volume(self) -> Volume | None:
        if not self.coordinator.online:
            return None
        for vol in self.coordinator.data.volumes:
            if vol.mount == self.mount:
                return vol
        return None

    @property
    def available(self) -> bool:
        """Available while mounted."""
        return self._volume is not None

    @property
    def native_value(self) -> float | None:
        """Used percentage."""
        vol = self._volume
        return vol.used_pct if vol else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Sizes and details."""
        vol = self._volume
        if vol is None:
            return None
        gib = 1024**3
        return {
            "device": vol.device,
            "filesystem": vol.fstype,
            "size_gib": round(vol.size / gib, 1),
            "used_gib": round(vol.used / gib, 1),
            "free_gib": round(vol.available / gib, 1),
            "also_mounted_on": vol.other_mounts,
        }


POWER_KEYS = ("power_total", "power_cpu", "power_platform", "power_gpu", "power_battery", "power_pmic")


def _power_value(power: PowerValues, key: str) -> tuple[float | None, str | None]:
    if key == "power_total":
        return power.total
    return getattr(power, key.removeprefix("power_")), None


class PowerSensor(GLEntity, CoordinatorEntity[StatusCoordinator], SensorEntity):
    """Power consumption (W). The total is an estimate, not a wall meter."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, entry: GLConfigEntry, rt, key: str) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, key)
        CoordinatorEntity.__init__(self, rt.status)
        self.key = key
        self._attr_translation_key = key
        if key != "power_total":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        """Available while online and measurable."""
        return self.coordinator.online and self.native_value is not None

    @property
    def native_value(self) -> float | None:
        """Watts."""
        return _power_value(self.coordinator.power, self.key)[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Source of the estimate / RAPL domains."""
        power = self.coordinator.power
        if self.key == "power_total":
            return {"source": power.total[1]}
        if self.key in ("power_cpu", "power_platform") and power.rapl_domains:
            return {"rapl_domains": power.rapl_domains, "averaged_over_s": 300}
        return None


def _ts(epoch: int | None) -> str | None:
    return dt_util.utc_from_timestamp(epoch).isoformat() if epoch else None


class LastJobSensor(_UpdatesBase):
    """Result and output of the last update that ran on the computer."""

    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["running", "success", "failed"]
    _unrecorded_attributes = frozenset({"log"})

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        super().__init__(entry, rt, "last_update_job")

    @property
    def available(self) -> bool:
        """Available once a job is known."""
        return self.coordinator.last_job is not None

    @property
    def native_value(self) -> str | None:
        """running / success / failed."""
        job = self.coordinator.last_job
        if job is None:
            return None
        if job.state == "running":
            return "running"
        return "success" if job.state == "done" and job.rc == 0 else "failed"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """What ran, when, exit code and the last lines of output."""
        job = self.coordinator.last_job
        if job is None:
            return None
        return {
            "job": job.job,
            "started": _ts(job.started),
            "finished": _ts(job.finished),
            "exit_code": job.rc,
            "log": job.log.rstrip()[-8000:],
        }


_SKIP_IFACES = ("docker", "br-", "veth", "virbr", "cni", "podman", "lxc", "vnet", "flannel", "cali")


class InterfaceIpSensor(GLEntity, CoordinatorEntity[StatusCoordinator], SensorEntity):
    """Address of one network interface."""

    _attr_translation_key = "ip_interface"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: GLConfigEntry, rt, interface: str) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, f"ip_{interface}")
        CoordinatorEntity.__init__(self, rt.status)
        self.interface = interface
        self._attr_translation_placeholders = {"interface": interface}

    @property
    def _addresses(self) -> list[str]:
        data = self.coordinator.data
        return data.addresses.get(self.interface, []) if data else []

    @property
    def native_value(self) -> str | None:
        """IPv4 address when there is one, otherwise the first IPv6 (unknown when down)."""
        addrs = self._addresses
        return next((a for a in addrs if ":" not in a), addrs[0] if addrs else None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Every address of the interface."""
        return {"addresses": self._addresses}


class PublicIpSensor(GLEntity, CoordinatorEntity[StatusCoordinator], SensorEntity):
    """Address the computer has on the Internet (asked every 5 minutes)."""

    _attr_translation_key = "public_ip"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, "public_ip")
        CoordinatorEntity.__init__(self, rt.status)

    @property
    def native_value(self) -> str | None:
        """IPv4 when there is one, otherwise IPv6."""
        ips = self.coordinator.public_ip
        return ips.get("ipv4") or ips.get("ipv6")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Both families."""
        ips = self.coordinator.public_ip
        return {"ipv4": ips.get("ipv4"), "ipv6": ips.get("ipv6")}
