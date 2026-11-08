# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Buttons: reboot, check for updates, screenshot."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GLConfigEntry
from .const import POWER_REBOOT
from .entity import GLEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: GLConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up buttons."""
    rt = entry.runtime_data
    async_add_entities(
        [
            RebootButton(entry, rt),
            CheckUpdatesButton(entry, rt),
            UpdateAllButton(entry, rt),
            ScreenshotButton(entry, rt),
        ]
    )


class RebootButton(GLEntity, ButtonEntity):
    """Reboot the computer."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_translation_key = "reboot"

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        super().__init__(entry, rt, "reboot")

    async def async_press(self) -> None:
        """Reboot."""
        await self.runtime.async_power(POWER_REBOOT, self.entry)


class CheckUpdatesButton(GLEntity, ButtonEntity):
    """Check for updates now."""

    _attr_translation_key = "check_updates"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        super().__init__(entry, rt, "check_updates")

    async def async_press(self) -> None:
        """Refresh the updates coordinator."""
        await self.runtime.updates.async_refresh()


class UpdateAllButton(GLEntity, ButtonEntity):
    """Install all pending updates (system, AUR, Flatpak, Snap, containers)."""

    _attr_translation_key = "update_all"

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        super().__init__(entry, rt, "update_all")

    async def async_press(self) -> None:
        """Install everything in the background (it can take a long time)."""
        updates = self.runtime.updates
        if not updates.pending_targets():
            return
        if updates.updating_all:
            raise HomeAssistantError("Ja s'està actualitzant")

        async def _run() -> None:
            try:
                await updates.async_install_all()
            except HomeAssistantError:
                pass  # already logged and notified

        self.hass.async_create_background_task(_run(), f"{self.entity_id} update all")


class ScreenshotButton(GLEntity, ButtonEntity):
    """Take a screenshot."""

    _attr_translation_key = "screenshot"

    def __init__(self, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        super().__init__(entry, rt, "take_screenshot")

    async def async_press(self) -> None:
        """Take it."""
        await self.runtime.async_screenshot()
