# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Power state select: on / suspend / hibernate / off / reboot."""

from __future__ import annotations

import asyncio

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GLConfigEntry
from .const import POWER_OFF, POWER_ON, POWER_OPTIONS, POWER_REBOOT, allowed_power_options
from .coordinator import StatusCoordinator, UpdatesCoordinator
from .entity import GLEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: GLConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the power select."""
    rt = entry.runtime_data
    async_add_entities([PowerSelect(entry, rt)])
    added = False

    @callback
    def add_esim() -> None:
        # Only computers with an eSIM (lpac lists its profiles) get the select.
        nonlocal added
        if not added and rt.updates.data is not None and rt.updates.data.esim:
            added = True
            async_add_entities([EsimSelect(entry, rt)])

    add_esim()
    entry.async_on_unload(rt.updates.async_add_listener(add_esim))


class EsimSelect(GLEntity, CoordinatorEntity[UpdatesCoordinator], SelectEntity):
    """Active eSIM profile; choosing another one enables it (lpac)."""

    _attr_translation_key = "esim_profile"

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, "esim_profile")
        CoordinatorEntity.__init__(self, rt.updates)

    @property
    def _profiles(self):
        data = self.coordinator.data
        return (data.esim if data else None) or []

    @property
    def options(self) -> list[str]:
        """Profiles stored in the eSIM."""
        return [p.label for p in self._profiles]

    @property
    def current_option(self) -> str | None:
        """The enabled profile."""
        return next((p.label for p in self._profiles if p.enabled), None)

    @property
    def extra_state_attributes(self) -> dict | None:
        """Details of the enabled profile."""
        if (cur := next((p for p in self._profiles if p.enabled), None)) is None:
            return None
        return {"iccid": cur.iccid, "provider": cur.provider}

    async def async_select_option(self, option: str) -> None:
        """Enable another profile."""
        profile = next((p for p in self._profiles if p.label == option), None)
        if profile is None:
            raise HomeAssistantError(f"Perfil eSIM desconegut: {option}")
        if not profile.enabled:
            await self.coordinator.async_esim_enable(profile.iccid)


class PowerSelect(GLEntity, CoordinatorEntity[StatusCoordinator], SelectEntity, RestoreEntity):
    """Power state. Shows 'on' while reachable, otherwise the last requested state."""

    _attr_translation_key = "power"

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, "power")
        CoordinatorEntity.__init__(self, rt.status)
        self._attr_options = allowed_power_options(entry.options)
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
    def current_option(self) -> str | None:
        """Current state; None (unknown) when off in a way that is not offered."""
        if self.coordinator.online:
            return POWER_ON
        return self._off_state if self._off_state in self.options else None

    async def async_select_option(self, option: str) -> None:
        """Change power state."""
        await self.runtime.async_power(option, self.entry)
        if option not in (POWER_ON, POWER_REBOOT):
            self._off_state = option
        self.async_write_ha_state()
        await asyncio.sleep(5)
        await self.coordinator.async_request_refresh()
