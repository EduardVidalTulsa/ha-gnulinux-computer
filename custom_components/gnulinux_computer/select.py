# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Power state select: on / suspend / hibernate / off / reboot."""

from __future__ import annotations

import asyncio

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GLConfigEntry
from .const import POWER_OFF, POWER_ON, POWER_OPTIONS, POWER_REBOOT
from .coordinator import StatusCoordinator
from .entity import GLEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: GLConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the power select."""
    async_add_entities([PowerSelect(entry, entry.runtime_data)])


class PowerSelect(GLEntity, CoordinatorEntity[StatusCoordinator], SelectEntity, RestoreEntity):
    """Power state. Shows 'on' while reachable, otherwise the last requested state."""

    _attr_translation_key = "power"
    _attr_options = POWER_OPTIONS

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, "power")
        CoordinatorEntity.__init__(self, rt.status)
        self._off_state: str = POWER_OFF

    async def async_added_to_hass(self) -> None:
        """Restore the last off-state."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in POWER_OPTIONS:
            if last.state not in (POWER_ON, POWER_REBOOT):
                self._off_state = last.state

    @property
    def available(self) -> bool:
        """Always available (needed to wake the computer)."""
        return True

    @property
    def current_option(self) -> str:
        """Current state."""
        if self.coordinator.online:
            return POWER_ON
        return self._off_state

    async def async_select_option(self, option: str) -> None:
        """Change power state."""
        await self.runtime.async_power(option, self.entry)
        if option not in (POWER_ON, POWER_REBOOT):
            self._off_state = option
        self.async_write_ha_state()
        await asyncio.sleep(5)
        await self.coordinator.async_request_refresh()
