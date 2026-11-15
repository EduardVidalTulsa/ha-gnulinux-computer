# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""The GNU+Linux Computer integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .client import CannotConnect, LinuxClient, fetch_host_key, load_private_key
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
from .keys import async_use_shared_key

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
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
SERVICE_RUN_COMMAND = "run_command"
SERVICE_SHARED_KEY = "use_shared_key"
ATTR_ENTRY = "config_entry_id"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions."""

    def _admin(handler):
        """Only administrators (or automations) may call the action.

        The actions run as root on the computers, so plain users are refused.
        """

        async def wrapped(call: ServiceCall) -> ServiceResponse:
            if call.context.user_id:
                user = await hass.auth.async_get_user(call.context.user_id)
                if user is None or not user.is_admin:
                    raise Unauthorized(context=call.context)
            return await handler(call)

        return wrapped

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
        _admin(unit_control),
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
        _admin(update_log),
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
        _admin(screenshot),
        schema=vol.Schema({vol.Required(ATTR_ENTRY): cv.string}),
    )

    async def run_command(call: ServiceCall) -> ServiceResponse:
        # Only the name of a command defined in the options: never free text.
        entry = hass.config_entries.async_get_entry(call.data[ATTR_ENTRY])
        result = await _runtime(call).async_command(call.data["command"], entry)
        return result if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_COMMAND,
        _admin(run_command),
        schema=vol.Schema(
            {vol.Required(ATTR_ENTRY): cv.string, vol.Required("command"): cv.string}
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def use_shared_key(call: ServiceCall) -> ServiceResponse:
        """Move one computer (or all of them) to the shared SSH key of the pool."""
        if entry_id := call.data.get(ATTR_ENTRY):
            _runtime(call)
            entries = [hass.config_entries.async_get_entry(entry_id)]
        else:
            entries = hass.config_entries.async_entries(DOMAIN)
        results: dict[str, str] = {}
        for entry in entries:
            try:
                results[entry.title] = await async_use_shared_key(
                    hass, entry, remove_old=call.data["remove_old_key"]
                )
            except HomeAssistantError as err:
                results[entry.title] = f"error: {err}"
        _LOGGER.info("use_shared_key: %s", results)
        return {"computers": results} if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_SHARED_KEY,
        _admin(use_shared_key),
        schema=vol.Schema(
            {
                vol.Optional(ATTR_ENTRY): cv.string,
                vol.Optional("remove_old_key", default=True): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
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
    if not data.get(CONF_HOST_KEY):
        # Entries created without the config flow: pin the host key now
        # (trust on first use) instead of connecting without verifying it.
        try:
            host_key = await fetch_host_key(data[CONF_HOST], data.get(CONF_PORT, DEFAULT_PORT))
        except CannotConnect:
            host_key = None  # asleep: pinned at the next start
        else:
            _LOGGER.warning("%s: SSH host key pinned on first use", data[CONF_HOST])
            hass.config_entries.async_update_entry(entry, data={**data, CONF_HOST_KEY: host_key})
            data = entry.data
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
    if client.auth_error:
        # Rejected credentials: do not keep trying, ask for new ones (reauth).
        await client.close()
        raise ConfigEntryAuthFailed(client.auth_error)
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
