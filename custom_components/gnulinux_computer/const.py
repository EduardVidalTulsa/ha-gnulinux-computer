# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Constants for the GNU+Linux Computer integration."""

from __future__ import annotations

DOMAIN = "gnulinux_computer"

CONF_AUTH_METHOD = "auth_method"
AUTH_PASSWORD = "password"
AUTH_KEY_FILE = "key_file"
AUTH_GENERATE = "generate_key"

CONF_KEY_PATH = "key_path"
CONF_SUDO_PASSWORD = "sudo_password"
CONF_HOST_KEY = "host_key"
CONF_MACHINE_ID = "machine_id"
CONF_HOSTNAME = "hostname"
CONF_OS_NAME = "os_name"
CONF_MODEL = "model"
CONF_MANUFACTURER = "manufacturer"

# Options
CONF_MAC = "mac"
CONF_BROADCAST = "broadcast_address"
CONF_STATUS_INTERVAL = "status_interval"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_UNITS = "units"
CONF_NOTIFY = "notify"
CONF_PUBLIC_IP = "public_ip"
CONF_IP_LOCATION = "ip_location"
# Rough position from the public IP (city level at best): metres of uncertainty
IP_LOCATION_ACCURACY = 25000

DEFAULT_PORT = 22
DEFAULT_BROADCAST = "255.255.255.255"
DEFAULT_STATUS_INTERVAL = 30  # seconds
DEFAULT_UPDATE_INTERVAL = 60  # minutes
KEY_DIR = "/config/.ssh"
# One key pair for every computer (and router) of the pool: if Home Assistant
# is ever compromised, a single public key has to be removed everywhere.
SHARED_KEY_PATH = f"{KEY_DIR}/gnulinux_computer"
SHARED_KEY_COMMENT = f"home-assistant@{DOMAIN}"

# Update backends
BACKEND_PACMAN = "pacman"
BACKEND_AUR = "aur"
BACKEND_DNF = "dnf"
BACKEND_ZYPPER = "zypper"
BACKEND_APT = "apt"
BACKEND_APK = "apk"
BACKEND_OSTREE = "ostree"
BACKEND_FLATPAK = "flatpak"
BACKEND_SNAP = "snap"
BACKEND_FIRMWARE = "firmware"
BACKENDS = (
    BACKEND_PACMAN,
    BACKEND_AUR,
    BACKEND_DNF,
    BACKEND_ZYPPER,
    BACKEND_APT,
    BACKEND_APK,
    BACKEND_OSTREE,
    BACKEND_FLATPAK,
    BACKEND_SNAP,
    BACKEND_FIRMWARE,
)

# Power
POWER_ON = "on"
POWER_SUSPEND = "suspend"
POWER_HIBERNATE = "hibernate"
POWER_OFF = "poweroff"
POWER_REBOOT = "reboot"
POWER_OPTIONS = [POWER_ON, POWER_SUSPEND, POWER_HIBERNATE, POWER_OFF, POWER_REBOOT]

UNIT_COMMANDS = ["start", "stop", "restart", "reload", "enable", "disable"]

# Custom commands: {name: shell command}, defined by an administrator in the
# options. Only these can be run from Home Assistant (never free text).
CONF_COMMANDS = "commands"
COMMAND_TIMEOUT = 300  # seconds
COMMAND_OUTPUT_MAX = 8000  # characters kept from the end of the output

EVENT_UPDATES_AVAILABLE = f"{DOMAIN}_updates_available"
EVENT_COMMAND_FINISHED = f"{DOMAIN}_command_finished"

# Wake-on-LAN method
CONF_WOL_METHOD = "wol_method"
WOL_LOCAL = "local"
WOL_COMPUTER = "computer"
WOL_MIKROTIK = "mikrotik"
# Removed in 0.3.8 (it drove the core MikroTik integration's private API session).
WOL_LEGACY_HA_MIKROTIK = "ha_mikrotik"
CONF_WOL_RELAY_ENTRY = "wol_relay_entry"
CONF_WOL_ROUTER_HOST = "wol_router_host"
CONF_WOL_ROUTER_PORT = "wol_router_port"
CONF_WOL_ROUTER_USER = "wol_router_user"
CONF_WOL_ROUTER_PASSWORD = "wol_router_password"
CONF_WOL_ROUTER_KEY = "wol_router_key"
CONF_WOL_ROUTER_INTERFACE = "wol_router_interface"
CONF_WOL_ROUTER_HOST_KEY = "wol_router_host_key"

# Allowed power actions (servers usually allow none of them)
CONF_ALLOW_SUSPEND = "allow_suspend"
CONF_ALLOW_HIBERNATE = "allow_hibernate"
CONF_ALLOW_POWEROFF = "allow_poweroff"
ALLOW_KEYS = {
    POWER_SUSPEND: CONF_ALLOW_SUSPEND,
    POWER_HIBERNATE: CONF_ALLOW_HIBERNATE,
    POWER_OFF: CONF_ALLOW_POWEROFF,
}


def allowed_power_options(options: dict) -> list[str]:
    """Power select options for these entry options (missing key = allowed)."""
    return [
        opt
        for opt in POWER_OPTIONS
        if opt not in ALLOW_KEYS or options.get(ALLOW_KEYS[opt], True)
    ]
