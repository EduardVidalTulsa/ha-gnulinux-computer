# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Wake-on-LAN: local broadcast, or relayed by another computer / a MikroTik router."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .client import CannotConnect, CommandError, LinuxClient, load_private_key
from .const import (
    CONF_BROADCAST,
    CONF_MAC,
    CONF_WOL_METHOD,
    CONF_WOL_RELAY_ENTRY,
    CONF_WOL_ROUTER_HOST,
    CONF_WOL_ROUTER_HOST_KEY,
    CONF_WOL_ROUTER_INTERFACE,
    CONF_WOL_ROUTER_KEY,
    CONF_WOL_ROUTER_PASSWORD,
    CONF_WOL_ROUTER_PORT,
    CONF_WOL_ROUTER_USER,
    DEFAULT_BROADCAST,
    DOMAIN,
    WOL_COMPUTER,
    WOL_MIKROTIK,
)
from .remote import routeros_wol_command, wol_relay_script

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


async def router_client(hass: HomeAssistant, opts: dict) -> LinuxClient:
    """SSH client for a MikroTik router described by options."""
    key = None
    if path := opts.get(CONF_WOL_ROUTER_KEY):
        try:
            key = await hass.async_add_executor_job(load_private_key, path)
        except (OSError, ValueError) as err:
            raise HomeAssistantError(f"No es pot llegir la clau {path}: {err}") from err
    return LinuxClient(
        opts[CONF_WOL_ROUTER_HOST],
        int(opts.get(CONF_WOL_ROUTER_PORT, 22)),
        opts[CONF_WOL_ROUTER_USER],
        password=opts.get(CONF_WOL_ROUTER_PASSWORD) or None,
        private_key=key,
        host_key=opts.get(CONF_WOL_ROUTER_HOST_KEY),
    )


async def async_wake(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Wake the computer of this entry with the configured method."""
    from .coordinator import send_magic_packet  # noqa: PLC0415

    opts = entry.options
    mac = opts.get(CONF_MAC)
    if not mac:
        raise HomeAssistantError("No hi ha cap adreça MAC configurada per a Wake-on-LAN")
    broadcast = opts.get(CONF_BROADCAST) or DEFAULT_BROADCAST
    method = opts.get(CONF_WOL_METHOD)

    if method == WOL_COMPUTER:
        relay = hass.config_entries.async_get_entry(opts.get(CONF_WOL_RELAY_ENTRY, ""))
        if relay is None or relay.domain != DOMAIN or not hasattr(relay, "runtime_data"):
            raise HomeAssistantError("L'ordinador pont per al Wake-on-LAN no està disponible")
        try:
            res = await relay.runtime_data.client.run(wol_relay_script(mac, broadcast), timeout=30)
        except (CannotConnect, CommandError) as err:
            raise HomeAssistantError(f"{relay.title} no respon: {err}") from err
        if not res.ok:
            raise HomeAssistantError(f"{relay.title}: {res.stderr.strip() or res.exit_status}")
        return

    if method == WOL_MIKROTIK:
        client = await router_client(hass, opts)
        try:
            res = await client.run_raw(
                routeros_wol_command(mac, opts.get(CONF_WOL_ROUTER_INTERFACE)), timeout=20
            )
        except (CannotConnect, CommandError) as err:
            raise HomeAssistantError(f"Router {client.host} no respon: {err}") from err
        finally:
            await client.close()
        out = f"{res.stdout}{res.stderr}".strip()
        if not res.ok or "failure" in out.lower() or "bad command" in out.lower():
            raise HomeAssistantError(f"Router {client.host}: {out or res.exit_status}")
        return

    await hass.async_add_executor_job(send_magic_packet, mac, broadcast)
