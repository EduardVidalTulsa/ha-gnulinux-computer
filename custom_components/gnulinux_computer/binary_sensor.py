# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Binary sensors: connectivity and monitored services."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GLConfigEntry
from .coordinator import StatusCoordinator
from .entity import GLEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: GLConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up binary sensors."""
    rt = entry.runtime_data
    entities: list[BinarySensorEntity] = [OnlineSensor(entry, rt), RebootRequiredSensor(entry, rt)]
    entities += [UnitSensor(entry, rt, unit) for unit in rt.status.units]
    async_add_entities(entities)


class OnlineSensor(GLEntity, CoordinatorEntity[StatusCoordinator], BinarySensorEntity):
    """Whether the computer answers over SSH. Always available."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "online"

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, "online")
        CoordinatorEntity.__init__(self, rt.status)

    @property
    def available(self) -> bool:
        """Always available."""
        return True

    @property
    def is_on(self) -> bool:
        """Online."""
        return self.coordinator.online


class RebootRequiredSensor(GLEntity, CoordinatorEntity[StatusCoordinator], BinarySensorEntity):
    """On when the running kernel/drivers no longer match the installed ones."""

    _attr_translation_key = "reboot_required"

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, "reboot_required")
        CoordinatorEntity.__init__(self, rt.status)

    @property
    def available(self) -> bool:
        """Available while online."""
        return self.coordinator.online

    @property
    def is_on(self) -> bool:
        """Reboot needed."""
        return bool(self.coordinator.data.reboot_reasons)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Why."""
        if not self.coordinator.data:
            return None
        return {"reasons": self.coordinator.data.reboot_reasons}


class UnitSensor(GLEntity, CoordinatorEntity[StatusCoordinator], BinarySensorEntity):
    """A monitored service: on = running."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = "unit"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: GLConfigEntry, rt, unit: str) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, f"unit_{unit}")
        CoordinatorEntity.__init__(self, rt.status)
        self.unit = unit
        self._attr_translation_placeholders = {"unit": unit.removesuffix(".service")}

    @property
    def available(self) -> bool:
        """Available when online and the unit was reported."""
        return self.coordinator.online and self.unit in self.coordinator.data.units

    @property
    def is_on(self) -> bool | None:
        """Running."""
        state = self.coordinator.data.units.get(self.unit)
        return state.active if state else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Unit details."""
        state = self.coordinator.data.units.get(self.unit) if self.coordinator.data else None
        if not state:
            return None
        return {
            "unit": self.unit,
            "active_state": state.active_state,
            "sub_state": state.sub_state,
            "description": state.description,
            "unit_file_state": state.unit_file_state,
        }
