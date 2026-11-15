# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Buttons: reboot, check for updates, screenshot."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GLConfigEntry
from .const import CONF_COMMANDS, CONF_MACHINE_ID, POWER_REBOOT
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
            *(CommandButton(entry, rt, name) for name in entry.options.get(CONF_COMMANDS) or {}),
        ]
    )
    # Buttons of commands that were removed from the options
    wanted = {_command_uid(entry, name) for name in entry.options.get(CONF_COMMANDS) or {}}
    prefix = f"{entry.data[CONF_MACHINE_ID]}_command_"
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.domain == "button" and ent.unique_id.startswith(prefix) and ent.unique_id not in wanted:
            registry.async_remove(ent.entity_id)


def _command_uid(entry: GLConfigEntry, name: str) -> str:
    return f"{entry.data[CONF_MACHINE_ID]}_command_{name}"


class CommandButton(GLEntity, ButtonEntity):
    """Runs one of the custom commands defined in the options."""

    _attr_translation_key = "command"

    def __init__(self, entry: GLConfigEntry, rt, name: str) -> None:
        """Initialize."""
        super().__init__(entry, rt, f"command_{name}")
        self._name = name
        self._attr_translation_placeholders = {"name": name}

    async def async_added_to_hass(self) -> None:
        """Show the result of runs started from the action too."""
        await super().async_added_to_hass()
        self.runtime.command_listeners.append(self.async_write_ha_state)
        self.async_on_remove(
            lambda: self.runtime.command_listeners.remove(self.async_write_ha_state)
        )

    @property
    def extra_state_attributes(self) -> dict | None:
        """Exit code and output of the last run."""
        res = self.runtime.command_results.get(self._name)
        if res is None:
            return None
        return {
            "exit_code": res["exit_code"],
            "output": (res["stdout"] or res["stderr"])[-2000:],
            "duration": res["duration"],
        }

    async def async_press(self) -> None:
        """Run it; a non-zero exit code is an error."""
        res = await self.runtime.async_command(self._name, self.entry)
        if res["exit_code"] != 0:
            tail = (res["stderr"] or res["stdout"]).strip()[-300:]
            raise HomeAssistantError(f"{self._name}: codi {res['exit_code']} {tail}")


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
