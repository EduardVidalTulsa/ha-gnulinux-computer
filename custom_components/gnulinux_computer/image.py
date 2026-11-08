# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Image entity with the last screenshot."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GLConfigEntry
from .entity import GLEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: GLConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the screenshot image."""
    async_add_entities([ScreenshotImage(hass, entry, entry.runtime_data)])


class ScreenshotImage(GLEntity, ImageEntity):
    """Last screenshot taken with the screenshot button/action."""

    _attr_translation_key = "screenshot"
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, entry: GLConfigEntry, rt) -> None:
        """Initialize."""
        GLEntity.__init__(self, entry, rt, "screenshot")
        ImageEntity.__init__(self, hass)

    async def async_added_to_hass(self) -> None:
        """Listen for new screenshots."""
        await super().async_added_to_hass()
        self.runtime.screenshot_listeners.append(self._new_image)
        self.async_on_remove(lambda: self.runtime.screenshot_listeners.remove(self._new_image))

    @callback
    def _new_image(self) -> None:
        self._attr_image_last_updated = self.runtime.screenshot_at
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """Return the last screenshot."""
        return self.runtime.screenshot
