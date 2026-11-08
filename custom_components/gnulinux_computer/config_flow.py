# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Config flow for the GNU+Linux Computer integration."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import asyncssh
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .client import CannotConnect, LinuxClient, fetch_host_key, load_private_key
from .const import (
    AUTH_GENERATE,
    AUTH_KEY_FILE,
    AUTH_PASSWORD,
    CONF_AUTH_METHOD,
    CONF_BROADCAST,
    CONF_HOST_KEY,
    CONF_HOSTNAME,
    CONF_KEY_PATH,
    CONF_MAC,
    CONF_MACHINE_ID,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_NOTIFY,
    CONF_OS_NAME,
    CONF_STATUS_INTERVAL,
    CONF_SUDO_PASSWORD,
    CONF_UNITS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_BROADCAST,
    DEFAULT_PORT,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    KEY_DIR,
)
from .remote import IDENTIFY_SCRIPT, LIST_UNITS_SCRIPT, parse_identity, parse_unit_list

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")


def _generate_key(path: str, comment: str) -> str:
    """Create an ed25519 key pair (blocking). Return the public key."""
    if os.path.exists(path):
        key = asyncssh.read_private_key(path)
    else:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        key = asyncssh.generate_private_key("ssh-ed25519", comment=comment)
        key.write_private_key(path)
        os.chmod(path, 0o600)
        key.write_public_key(path + ".pub")
    return key.export_public_key().decode().strip()


class GLConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._data: dict[str, Any] = {}
        self._pubkey: str = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options flow."""
        return GLOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Connection parameters."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = dict(user_input)
            try:
                self._data[CONF_HOST_KEY] = await fetch_host_key(
                    user_input[CONF_HOST], user_input[CONF_PORT]
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                method = user_input[CONF_AUTH_METHOD]
                if method == AUTH_PASSWORD:
                    return await self.async_step_password()
                if method == AUTH_KEY_FILE:
                    return await self.async_step_key_file()
                return await self.async_step_generate_key()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_HOST): str,
                        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                        vol.Required(CONF_USERNAME): str,
                        vol.Required(CONF_AUTH_METHOD, default=AUTH_PASSWORD): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=[AUTH_PASSWORD, AUTH_KEY_FILE, AUTH_GENERATE],
                                translation_key="auth_method",
                                mode=selector.SelectSelectorMode.LIST,
                            )
                        ),
                    }
                ),
                user_input,
            ),
            errors=errors,
        )

    async def async_step_password(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Password authentication."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data[CONF_PASSWORD] = user_input[CONF_PASSWORD]
            errors = await self._validate()
            if not errors:
                return await self._create()
        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema(
                {vol.Required(CONF_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                )}
            ),
            errors=errors,
            description_placeholders={"user": self._data[CONF_USERNAME], "host": self._data[CONF_HOST]},
        )

    async def async_step_key_file(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Existing private key on the Home Assistant host."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data[CONF_KEY_PATH] = user_input[CONF_KEY_PATH]
            self._data[CONF_SUDO_PASSWORD] = user_input.get(CONF_SUDO_PASSWORD, "")
            errors = await self._validate()
            if not errors:
                return await self._create()
        return self.async_show_form(
            step_id="key_file",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_KEY_PATH): str,
                        vol.Optional(CONF_SUDO_PASSWORD): selector.TextSelector(
                            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                        ),
                    }
                ),
                user_input or {CONF_KEY_PATH: f"{KEY_DIR}/id_ed25519"},
            ),
            errors=errors,
        )

    async def async_step_generate_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Generate a key pair and show the public key."""
        errors: dict[str, str] = {}
        host = self._data[CONF_HOST]
        user = self._data[CONF_USERNAME]
        path = f"{KEY_DIR}/gnulinux_{re.sub(r'[^A-Za-z0-9._-]', '_', host)}_{user}"
        if not self._pubkey:
            self._pubkey = await self.hass.async_add_executor_job(
                _generate_key, path, f"home-assistant@{DOMAIN}"
            )
        if user_input is not None:
            self._data[CONF_KEY_PATH] = path
            self._data[CONF_SUDO_PASSWORD] = user_input.get(CONF_SUDO_PASSWORD, "")
            errors = await self._validate()
            if not errors:
                return await self._create()
        return self.async_show_form(
            step_id="generate_key",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SUDO_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "pubkey": self._pubkey,
                "user": user,
                "host": host,
                "command": f"echo '{self._pubkey}' >> ~/.ssh/authorized_keys",
            },
        )

    async def _client(self) -> LinuxClient:
        key = None
        if self._data.get(CONF_KEY_PATH) and self._data[CONF_AUTH_METHOD] != AUTH_PASSWORD:
            key = await self.hass.async_add_executor_job(load_private_key, self._data[CONF_KEY_PATH])
        return LinuxClient(
            self._data[CONF_HOST],
            self._data[CONF_PORT],
            self._data[CONF_USERNAME],
            password=self._data.get(CONF_PASSWORD) or None,
            private_key=key,
            sudo_password=self._data.get(CONF_SUDO_PASSWORD) or None,
            host_key=self._data.get(CONF_HOST_KEY),
        )

    async def _validate(self) -> dict[str, str]:
        try:
            client = await self._client()
        except (OSError, ValueError, asyncssh.KeyImportError):
            return {"base": "invalid_key"}
        try:
            if not await client.detect_sudo():
                return {"base": "no_sudo"}
            res = await client.run(IDENTIFY_SCRIPT, timeout=30)
        except CannotConnect as err:
            _LOGGER.debug("Validation failed: %s", err)
            msg = str(err).lower()
            if "permission denied" in msg or "auth" in msg:
                return {"base": "invalid_auth"}
            return {"base": "cannot_connect"}
        finally:
            await client.close()
        ident = parse_identity(str(res.stdout))
        self._data.update(
            {
                CONF_MACHINE_ID: ident.machine_id,
                CONF_HOSTNAME: ident.hostname,
                CONF_OS_NAME: ident.os_name,
                CONF_MANUFACTURER: ident.manufacturer or "",
                CONF_MODEL: ident.model or "",
                "_mac": ident.mac or "",
            }
        )
        await self.async_set_unique_id(ident.machine_id)
        self._abort_if_unique_id_configured(
            updates={k: v for k, v in self._data.items() if not k.startswith("_")}
        )
        return {}

    async def _create(self) -> ConfigFlowResult:
        mac = self._data.pop("_mac", "")
        return self.async_create_entry(
            title=self._data[CONF_HOSTNAME],
            data=self._data,
            options={
                CONF_MAC: mac,
                CONF_BROADCAST: DEFAULT_BROADCAST,
                CONF_STATUS_INTERVAL: DEFAULT_STATUS_INTERVAL,
                CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
                CONF_UNITS: [],
                CONF_NOTIFY: True,
            },
        )


class GLOptionsFlow(OptionsFlow):
    """Options: services to monitor, intervals, Wake-on-LAN."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            mac = user_input.get(CONF_MAC, "").strip()
            if mac and not _MAC_RE.match(mac):
                errors[CONF_MAC] = "invalid_mac"
            else:
                user_input[CONF_MAC] = mac.lower().replace("-", ":")
                return self.async_create_entry(data=user_input)

        current = self.config_entry.options
        units: list[str] = list(current.get(CONF_UNITS, []))
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is not None and runtime.status.online:
            try:
                res = await runtime.client.run(LIST_UNITS_SCRIPT, timeout=30)
                units = sorted(set(units) | set(parse_unit_list(str(res.stdout))))
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Could not list units", exc_info=True)

        schema = vol.Schema(
            {
                vol.Optional(CONF_UNITS, default=current.get(CONF_UNITS, [])): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=units, multiple=True, custom_value=True, sort=True
                    )
                ),
                vol.Optional(CONF_NOTIFY, default=current.get(CONF_NOTIFY, True)): bool,
                vol.Optional(
                    CONF_UPDATE_INTERVAL,
                    default=current.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=1440, step=5, unit_of_measurement="min")
                ),
                vol.Optional(
                    CONF_STATUS_INTERVAL,
                    default=current.get(CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=10, max=600, step=5, unit_of_measurement="s")
                ),
                vol.Optional(CONF_MAC, default=current.get(CONF_MAC, "")): str,
                vol.Optional(
                    CONF_BROADCAST, default=current.get(CONF_BROADCAST, DEFAULT_BROADCAST)
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
