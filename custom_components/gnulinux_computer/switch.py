# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Mobile data switch for computers with a modem managed by NetworkManager."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import GLConfigEntry
from .client import CannotConnect, CommandError
from .coordinator import StatusCoordinator
from .entity import GLEntity
from .remote import wwan_script


async def async_setup_entry(
    hass: HomeAssistant, entry: GLConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the switch the first time a modem is seen."""
    rt = entry.runtime_data
    added = False

    @callback
    def add() -> None:
        nonlocal added
        if not added and rt.status.online and rt.status.data.wwan_enabled is not None:
            added = True
            async_add_entities([MobileDataSwitch(entry, rt)])

    add()
    entry.async_on_unload(rt.status.async_add_listener(add))


class MobileDataSwitch(GLEntity, CoordinatorEntity[StatusCoordinator], SwitchEntity):
    """Mobile broadband radio (SIM / eSIM modem) of the computer."""

    _attr_translation_key = "mobile_data"

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, "mobile_data")
        CoordinatorEntity.__init__(self, rt.status)

    @property
    def available(self) -> bool:
        """Only while the computer is reachable."""
        return self.coordinator.online and self.coordinator.data.wwan_enabled is not None

    @property
    def is_on(self) -> bool | None:
        """True when the modem radio is enabled."""
        return self.coordinator.data.wwan_enabled if self.coordinator.data else None

    async def _set(self, enable: bool) -> None:
        try:
            res = await self.runtime.client.run(wwan_script(enable), timeout=30)
        except (CannotConnect, CommandError) as err:
            raise HomeAssistantError(f"{self.entry.title}: {err}") from err
        if not res.ok:
            raise HomeAssistantError(res.stderr.strip() or f"exit {res.exit_status}")
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable mobile data."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable mobile data (careful if the computer is reached through it)."""
        await self._set(False)
