# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Position of computers that have a GNSS receiver (gpsd or ModemManager)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.const import ATTR_GPS_ACCURACY, ATTR_LATITUDE, ATTR_LONGITUDE, STATE_NOT_HOME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GLConfigEntry
from .coordinator import StatusCoordinator
from .entity import GLEntity
from .remote import Location


async def async_setup_entry(
    hass: HomeAssistant, entry: GLConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the tracker the first time the computer reports a position."""
    rt = entry.runtime_data
    added = False

    @callback
    def add() -> None:
        nonlocal added
        if not added and rt.status.location is not None:
            added = True
            async_add_entities([LocationTracker(entry, rt)])

    add()
    entry.async_on_unload(rt.status.async_add_listener(add))


class LocationTracker(GLEntity, CoordinatorEntity[StatusCoordinator], TrackerEntity, RestoreEntity):
    """Last known position of the computer."""

    _attr_translation_key = "location"
    _attr_source_type = SourceType.GPS

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, "location")
        CoordinatorEntity.__init__(self, rt.status)

    async def async_added_to_hass(self) -> None:
        """After a restart, keep the last real fix until a new one arrives.

        A receiver indoors often has no fix: do not fall back to the rough
        IP position when we knew better.
        """
        await super().async_added_to_hass()
        cur = self.coordinator.location
        if cur is not None and cur.source != "ip":
            return
        last = await self.async_get_last_state()
        if last is None or last.attributes.get("fix_source") in (None, "ip"):
            return
        try:
            self.coordinator.location = Location(
                float(last.attributes[ATTR_LATITUDE]),
                float(last.attributes[ATTR_LONGITUDE]),
                last.attributes.get("altitude"),
                last.attributes.get(ATTR_GPS_ACCURACY) or None,
                last.attributes["fix_source"],
            )
        except (KeyError, TypeError, ValueError):
            return
        self.async_write_ha_state()

    @property
    def state(self) -> str | None:
        """Zone from a real fix; for a position guessed from the IP, the city.

        The IP only gives an area of tens of km: saying "home" because the
        home zone falls inside it would be a guess.
        """
        loc = self.coordinator.location
        if loc is not None and loc.source == "ip":
            info = self.coordinator.ip_info
            return info.get("city") or info.get("country") or STATE_NOT_HOME
        return super().state

    @property
    def available(self) -> bool:
        """The last known position stays while the computer is off."""
        return self.coordinator.location is not None

    @property
    def latitude(self) -> float | None:
        """Latitude."""
        loc = self.coordinator.location
        return loc.latitude if loc else None

    @property
    def longitude(self) -> float | None:
        """Longitude."""
        loc = self.coordinator.location
        return loc.longitude if loc else None

    @property
    def location_accuracy(self) -> float:
        """Accuracy in metres."""
        loc = self.coordinator.location
        return float(loc.accuracy) if loc and loc.accuracy else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Altitude and where the fix comes from."""
        loc = self.coordinator.location
        if loc is None:
            return None
        attrs: dict[str, Any] = {"altitude": loc.altitude, "fix_source": loc.source}
        if loc.source == "ip":
            # Approximate: where the public IP is registered
            info = self.coordinator.ip_info
            attrs.update({k: info.get(k) for k in ("city", "region", "country", "country_code", "is_eu")})
        return attrs
