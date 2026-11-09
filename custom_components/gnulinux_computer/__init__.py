# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""The GNU+Linux Computer integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .client import LinuxClient, load_private_key
from .const import (
    AUTH_PASSWORD,
    CONF_AUTH_METHOD,
    CONF_HOST_KEY,
    CONF_KEY_PATH,
    CONF_SUDO_PASSWORD,
    DEFAULT_PORT,
    DOMAIN,
    UNIT_COMMANDS,
)
from .coordinator import Runtime, StatusCoordinator, UpdatesCoordinator

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.UPDATE,
]

type GLConfigEntry = ConfigEntry[Runtime]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_UNIT = "service_control"
SERVICE_SCREENSHOT = "take_screenshot"
SERVICE_UPDATE_LOG = "update_log"
ATTR_ENTRY = "config_entry_id"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions."""

    def _runtime(call: ServiceCall) -> Runtime:
        entry = hass.config_entries.async_get_entry(call.data[ATTR_ENTRY])
        if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
            raise ServiceValidationError("Ordinador desconegut o no carregat")
        return entry.runtime_data

    async def unit_control(call: ServiceCall) -> None:
        await _runtime(call).async_unit(call.data["unit"], call.data["command"])

    async def screenshot(call: ServiceCall) -> None:
        await _runtime(call).async_screenshot()

    hass.services.async_register(
        DOMAIN,
        SERVICE_UNIT,
        unit_control,
        schema=vol.Schema(
            {
                vol.Required(ATTR_ENTRY): cv.string,
                vol.Required("unit"): cv.string,
                vol.Required("command"): vol.In(UNIT_COMMANDS),
            }
        ),
    )
    async def update_log(call: ServiceCall) -> ServiceResponse:
        job = await _runtime(call).updates.async_job_log(call.data["lines"])
        if job is None:
            return {"job": None, "state": None, "log": ""}
        return {
            "job": job.job,
            "state": "running" if job.state == "running" else ("success" if job.state == "done" and job.rc == 0 else "failed"),
            "exit_code": job.rc,
            "started": job.started,
            "finished": job.finished,
            "log": job.log,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_LOG,
        update_log,
        schema=vol.Schema(
            {
                vol.Required(ATTR_ENTRY): cv.string,
                vol.Optional("lines", default=200): vol.All(vol.Coerce(int), vol.Range(min=10, max=5000)),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCREENSHOT,
        screenshot,
        schema=vol.Schema({vol.Required(ATTR_ENTRY): cv.string}),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GLConfigEntry) -> bool:
    """Set up a computer from a config entry."""
    data = entry.data
    key = None
    if data.get(CONF_AUTH_METHOD) != AUTH_PASSWORD and data.get(CONF_KEY_PATH):
        try:
            key = await hass.async_add_executor_job(load_private_key, data[CONF_KEY_PATH])
        except (OSError, ValueError) as err:
            raise ConfigEntryError(f"Cannot read key {data[CONF_KEY_PATH]}: {err}") from err
    client = LinuxClient(
        data[CONF_HOST],
        data.get(CONF_PORT, DEFAULT_PORT),
        data[CONF_USERNAME],
        password=data.get(CONF_PASSWORD) or None,
        private_key=key,
        sudo_password=data.get(CONF_SUDO_PASSWORD) or None,
        host_key=data.get(CONF_HOST_KEY),
    )
    status = StatusCoordinator(hass, entry, client)
    updates = UpdatesCoordinator(hass, entry, client, status)
    entry.runtime_data = Runtime(client=client, status=status, updates=updates)

    # The computer may be asleep: never fail setup because it is offline.
    await status.async_refresh()
    await updates.async_refresh()
    if status.online:
        # Installs started before a restart keep running on the computer.
        await updates.async_resume_jobs()
    entry.async_on_unload(status.async_add_listener(updates.status_changed))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: GLConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: GLConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.client.close()
    return ok
