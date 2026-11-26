# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Repairs: "reboot required", fixed by rebooting the computer."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, POWER_REBOOT


class RebootFlow(RepairsFlow):
    """Confirm, then reboot."""

    def __init__(self, entry_id: str, placeholders: dict[str, str]) -> None:
        """Initialize."""
        self._entry_id = entry_id
        self._placeholders = placeholders

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Start."""
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Ask before rebooting; refuse while updates are being installed."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
            return self.async_abort(reason="not_loaded")
        rt = entry.runtime_data
        if rt.updates.updating_all or rt.updates.installing:
            return self.async_abort(reason="updating")
        placeholders = {"reasons": "", **self._placeholders, "name": entry.title}
        if user_input is not None:
            try:
                await rt.async_power(POWER_REBOOT, entry)
            except HomeAssistantError as err:
                return self.async_abort(
                    reason="reboot_failed", description_placeholders={**placeholders, "error": str(err)}
                )
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm", data_schema=vol.Schema({}), description_placeholders=placeholders
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the flow of an issue."""
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    placeholders = dict(issue.translation_placeholders or {}) if issue else {}
    return RebootFlow((data or {}).get("entry_id", ""), placeholders)
