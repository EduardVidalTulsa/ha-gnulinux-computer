# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Base entity for the GNU+Linux Computer integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import (
    CONF_HOSTNAME,
    CONF_MAC,
    CONF_MACHINE_ID,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_OS_NAME,
    DOMAIN,
)
from .coordinator import Runtime


def device_info(entry: ConfigEntry) -> DeviceInfo:
    """Device info for the computer."""
    data = entry.data
    info = DeviceInfo(
        identifiers={(DOMAIN, data[CONF_MACHINE_ID])},
        name=data.get(CONF_HOSTNAME) or entry.title,
        manufacturer=data.get(CONF_MANUFACTURER) or None,
        model=data.get(CONF_MODEL) or None,
        sw_version=data.get(CONF_OS_NAME),
    )
    if mac := entry.options.get(CONF_MAC):
        info["connections"] = {(CONNECTION_NETWORK_MAC, mac.lower())}
    return info


class GLEntity(Entity):
    """Common base."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, runtime: Runtime, key: str) -> None:
        """Initialize."""
        self.entry = entry
        self.runtime = runtime
        self._attr_unique_id = f"{entry.data[CONF_MACHINE_ID]}_{key}"
        self._attr_device_info = device_info(entry)
        self._host = entry.data[CONF_HOST]
