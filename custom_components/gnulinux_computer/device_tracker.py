# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Position of computers that have a GNSS receiver (gpsd or ModemManager)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GLConfigEntry
from .coordinator import StatusCoordinator
from .entity import GLEntity


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


class LocationTracker(GLEntity, CoordinatorEntity[StatusCoordinator], TrackerEntity):
    """Last known position of the computer."""

    _attr_translation_key = "location"
    _attr_source_type = SourceType.GPS

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, "location")
        CoordinatorEntity.__init__(self, rt.status)

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
        return {"altitude": loc.altitude, "fix_source": loc.source}
