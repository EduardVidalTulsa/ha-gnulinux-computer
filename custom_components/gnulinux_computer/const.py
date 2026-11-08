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

DEFAULT_PORT = 22
DEFAULT_BROADCAST = "255.255.255.255"
DEFAULT_STATUS_INTERVAL = 30  # seconds
DEFAULT_UPDATE_INTERVAL = 60  # minutes
KEY_DIR = "/config/.ssh"

# Update backends
BACKEND_PACMAN = "pacman"
BACKEND_AUR = "aur"
BACKEND_DNF = "dnf"
BACKEND_ZYPPER = "zypper"
BACKEND_APT = "apt"
BACKEND_FLATPAK = "flatpak"
BACKEND_SNAP = "snap"
BACKEND_FIRMWARE = "firmware"
BACKENDS = (
    BACKEND_PACMAN,
    BACKEND_AUR,
    BACKEND_DNF,
    BACKEND_ZYPPER,
    BACKEND_APT,
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

EVENT_UPDATES_AVAILABLE = f"{DOMAIN}_updates_available"
