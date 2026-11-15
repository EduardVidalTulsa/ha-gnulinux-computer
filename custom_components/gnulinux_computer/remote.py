# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Remote scripts and output parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import re
import shlex
import xml.etree.ElementTree as ET

from .const import (
    BACKEND_APK,
    BACKEND_APT,
    BACKEND_AUR,
    BACKEND_DNF,
    BACKEND_FIRMWARE,
    BACKEND_FLATPAK,
    BACKEND_OSTREE,
    BACKEND_PACMAN,
    BACKEND_SNAP,
    BACKEND_ZYPPER,
    POWER_HIBERNATE,
    POWER_OFF,
    POWER_REBOOT,
    POWER_SUSPEND,
)

# --------------------------------------------------------------------------
# Generic helpers


def split_sections(text: str) -> dict[str, list[str]]:
    """Split output made of '@section' headers into a dict of lines."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("@") and " " not in line.strip():
            current = line[1:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


# --------------------------------------------------------------------------
# Identification (config flow)

IDENTIFY_SCRIPT = r"""
echo "@machine_id"; cat /etc/machine-id 2>/dev/null || cat /var/lib/dbus/machine-id 2>/dev/null
echo "@hostname"; hostname 2>/dev/null || cat /etc/hostname
echo "@os"; . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-GNU/Linux}"
echo "@vendor"; cat /sys/class/dmi/id/sys_vendor 2>/dev/null
echo "@model"; cat /sys/class/dmi/id/product_name 2>/dev/null || { tr -d '\0' < /proc/device-tree/model 2>/dev/null; echo; }
echo "@mac"; dev=$(ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
[ -n "$dev" ] && cat "/sys/class/net/$dev/address"
echo "@power"; cat /sys/power/state 2>/dev/null
echo "@swap"; awk 'NR>1{s+=$3}END{print s+0}' /proc/swaps 2>/dev/null
echo "@end"
"""


@dataclass
class Identity:
    """Identity of the remote computer."""

    machine_id: str
    hostname: str
    os_name: str
    manufacturer: str | None
    model: str | None
    mac: str | None
    can_suspend: bool = False
    can_hibernate: bool = False


def _first(sec: dict[str, list[str]], key: str) -> str | None:
    for line in sec.get(key, []):
        if line.strip():
            return line.strip()
    return None


def parse_identity(text: str) -> Identity:
    """Parse the identification script output."""
    sec = split_sections(text)
    hostname = _first(sec, "hostname") or "linux"
    states = (_first(sec, "power") or "").split()
    try:
        swap_kb = int(_first(sec, "swap") or 0)
    except ValueError:
        swap_kb = 0
    return Identity(
        machine_id=_first(sec, "machine_id") or hostname,
        hostname=hostname,
        os_name=_first(sec, "os") or "GNU/Linux",
        manufacturer=_first(sec, "vendor"),
        model=_first(sec, "model"),
        mac=_first(sec, "mac"),
        can_suspend="mem" in states,
        can_hibernate="disk" in states and swap_kb > 0,
    )


# --------------------------------------------------------------------------
# Status (fast poll)

STATUS_SCRIPT = r"""
echo "@uptime"; cut -d' ' -f1 /proc/uptime
echo "@load"; cut -d' ' -f1-3 /proc/loadavg
echo "@mem"; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
echo "@volumes"
# busybox df (Alpine) has no -x: then list everything, the parser filters
{ timeout 10 df -PT -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs -x ramfs -x nsfs \
  -x fuse.portal -x fuse.gvfsd-fuse -x fuse.snapfuse -x autofs 2>/dev/null || timeout 10 df -PT 2>/dev/null; } | tail -n +2
echo "@kernel"; uname -r
echo "@gpu"
command -v nvidia-smi >/dev/null 2>&1 && timeout 5 nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits 2>/dev/null | sed 's/^/nvidia /'
for h in /sys/class/drm/card*/device/hwmon/hwmon*; do
  f="$h/power1_average"; [ -r "$f" ] || f="$h/power1_input"
  [ -r "$f" ] && echo "amdgpu_uw $(cat "$f")"
done
echo "@battery"
for p in /sys/class/power_supply/*; do
  [ "$(cat "$p/type" 2>/dev/null)" = Battery ] || continue
  [ "$(cat "$p/scope" 2>/dev/null)" = Device ] && continue
  if [ -r "$p/power_now" ]; then w=$(cat "$p/power_now")
  elif [ -r "$p/current_now" ] && [ -r "$p/voltage_now" ]; then w=$(( $(cat "$p/current_now") / 1000 * $(cat "$p/voltage_now") / 1000 ))
  else continue; fi
  echo "$(basename "$p") $(cat "$p/status" 2>/dev/null) $w"
done
if command -v nmcli >/dev/null 2>&1 && nmcli -t -f TYPE device 2>/dev/null | grep -q '^gsm$'; then
  # Mobile broadband modem managed by NetworkManager
  echo "@wwan"; nmcli -t -f WWAN radio 2>/dev/null
fi
echo "@battery_level"
for p in /sys/class/power_supply/*; do
  [ "$(cat "$p/type" 2>/dev/null)" = Battery ] || continue
  [ "$(cat "$p/scope" 2>/dev/null)" = Device ] && continue
  [ -r "$p/capacity" ] && echo "$(cat "$p/capacity") $(cat "$p/status" 2>/dev/null)"
done
if command -v vcgencmd >/dev/null 2>&1; then
  # /dev/vcio_gencmd is often root-only: then read it with sudo only on slow polls
  if out=$(vcgencmd pmic_read_adc 2>/dev/null); then echo "@pmic"; printf '%s\n' "$out"
  elif [ -n "$GLC_SLOW" ]; then echo "@pmic"; $SUDO vcgencmd pmic_read_adc 2>/dev/null
  fi
fi
if [ -n "$GLC_SLOW" ] && ls -d /sys/class/powercap/*-rapl:* >/dev/null 2>&1; then
  echo "@rapl"
  $SUDO sh -c 'for z in /sys/class/powercap/intel-rapl:[0-9]* /sys/class/powercap/amd-rapl:[0-9]*; do [ -r "$z/energy_uj" ] || continue; case "${z##*/}" in *:*:*) continue;; esac; echo "$(cat "$z/name") $(cat "$z/energy_uj") $(cat "$z/max_energy_range_uj") $(date +%s%N)"; done' 2>/dev/null
fi
echo "@ip"
defdev=$(ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
echo "default $defdev"
ip -o addr show scope global 2>/dev/null | awk '$3=="inet" || $3=="inet6" {print $2, $4}'
if [ -n "$GLC_SLOW" ] && [ -n "$GLC_PUBLIC_IP" ]; then
  # Public address as seen from the Internet (asks api.ipify.org / icanhazip.com)
  pub() {
    if command -v curl >/dev/null 2>&1; then curl "-$1" -fsS --max-time 6 "$2" 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then timeout 8 wget "-$1" -qO- "$2" 2>/dev/null; fi
  }
  echo "@public_ip"
  echo "4 $(pub 4 https://api.ipify.org || pub 4 https://ipv4.icanhazip.com)"
  echo "6 $(pub 6 https://api6.ipify.org || pub 6 https://ipv6.icanhazip.com)"
fi
if [ -n "$GLC_SLOW" ]; then
  # Position, when the computer has a GNSS receiver: gpsd or ModemManager
  if command -v gpspipe >/dev/null 2>&1; then
    echo "@gpsd"; timeout 8 gpspipe -w -n 12 2>/dev/null | grep -m 1 '"class":"TPV".*"lat"'
  fi
  if command -v mmcli >/dev/null 2>&1; then
    echo "@mmloc"; timeout 8 mmcli -m any --location-get -K 2>/dev/null | grep -E 'gps\.(latitude|longitude|altitude)'
  fi
fi
echo "@reboot"
k=$(uname -r)
if [ -d /usr/lib/modules ] && [ ! -d "/usr/lib/modules/$k" ] && [ ! -d "/lib/modules/$k" ]; then
  echo "kernel: $k"
fi
if [ -r /sys/module/nvidia/version ] && command -v modinfo >/dev/null 2>&1; then
  lv=$(cat /sys/module/nvidia/version); iv=$(modinfo -F version nvidia 2>/dev/null)
  [ -n "$iv" ] && [ "$iv" != "$lv" ] && echo "nvidia: $lv -> $iv"
fi
for f in /run/reboot-required /var/run/reboot-required; do
  if [ -f "$f" ]; then echo "reboot-required: $(tr '\n' ' ' < "$f.pkgs" 2>/dev/null)"; break; fi
done
[ -f /run/reboot-needed ] && echo "reboot-needed"
if command -v needs-restarting >/dev/null 2>&1; then
  needs-restarting -r >/dev/null 2>&1; [ $? -eq 1 ] && echo "needs-restarting"
elif command -v dnf5 >/dev/null 2>&1; then
  # dnf5 (Fedora 41+, RHEL 11) has it built in
  dnf5 needs-restarting -r >/dev/null 2>&1; [ $? -eq 1 ] && echo "needs-restarting"
fi
if [ -e /run/ostree-booted ] && command -v rpm-ostree >/dev/null 2>&1; then
  # Atomic desktops / CoreOS / image mode: a new deployment waits for a reboot
  rpm-ostree status --pending-exit-77 >/dev/null 2>&1; [ $? -eq 77 ] && echo "ostree: pending deployment"
fi
set -f
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  echo "@init"; echo systemd
  echo "@state"; systemctl is-system-running 2>/dev/null
  echo "@failed"; systemctl list-units --failed --plain --no-legend --no-pager 2>/dev/null | awk '{print $1}'
  echo "@svc_running"; systemctl list-units --type=service --state=running --no-legend --no-pager --plain 2>/dev/null | awk '{print $1}'
  echo "@svc_files"; systemctl list-unit-files --type=service --no-legend --no-pager --plain 2>/dev/null | awk '{print $1" "$2}'
  echo "@units"
  [ -n "$UNITS" ] && systemctl show --no-pager -p Id,LoadState,ActiveState,SubState,Description,UnitFileState -- $UNITS 2>/dev/null
elif command -v rc-service >/dev/null 2>&1; then
  # OpenRC (Alpine, Gentoo, Artix...)
  echo "@init"; echo openrc
  echo "@state"; rc-status -r 2>/dev/null
  echo "@failed"; rc-status -c 2>/dev/null | awk '$2=="[" {print $1}'
  echo "@svc_running"; rc-status -a 2>/dev/null | awk '$2=="[" && $3=="started" {print $1}' | sort -u
  echo "@svc_files"
  en=$(rc-update show 2>/dev/null | awk '{print $1}' | sort -u)
  for s in $(rc-service -l 2>/dev/null); do
    case " $(echo $en) " in *" $s "*) echo "$s enabled";; *) echo "$s disabled";; esac
  done
  echo "@sysv"
  for u in $UNITS; do rc-service -- "$u" status >/dev/null 2>&1; echo "$u $?"; done
else
  echo "@init"; echo sysv
  echo "@sysv"
  for u in $UNITS; do service "$u" status >/dev/null 2>&1; echo "$u $?"; done
fi
set +f
echo "@end"
"""


@dataclass
class UnitState:
    """State of a service."""

    name: str
    active: bool
    active_state: str
    sub_state: str | None = None
    description: str | None = None
    unit_file_state: str | None = None
    loaded: bool = True


@dataclass
class Volume:
    """A mounted filesystem."""

    mount: str
    device: str
    fstype: str
    size: int  # bytes
    used: int
    available: int
    other_mounts: list[str] = field(default_factory=list)

    @property
    def used_pct(self) -> float | None:
        """Used percentage, as df computes it."""
        total = self.used + self.available
        return round(100 * self.used / total, 1) if total else None


_PREFERRED_MOUNTS = ("/", "/home")
_SKIP_MOUNT_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/snap/", "/var/lib/docker", "/var/lib/containers")
_SKIP_FSTYPES = frozenset(
    "tmpfs devtmpfs overlay squashfs efivarfs ramfs nsfs fuse.portal fuse.gvfsd-fuse "
    "fuse.snapfuse autofs proc sysfs devpts cgroup cgroup2 mqueue debugfs tracefs "
    "securityfs pstore bpf configfs fusectl hugetlbfs binfmt_misc rootfs".split()
)


def _mount_rank(mount: str) -> int:
    if mount in _PREFERRED_MOUNTS:
        return _PREFERRED_MOUNTS.index(mount)
    return len(_PREFERRED_MOUNTS)


def parse_volumes(lines: list[str]) -> list[Volume]:
    """Parse `df -PT` lines, one Volume per filesystem."""
    by_dev: dict[tuple[str, str], Volume] = {}
    for line in lines:
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        device, fstype, size, used, avail, _cap, mount = parts
        if (mount.startswith(_SKIP_MOUNT_PREFIXES) and mount != "/") or fstype in _SKIP_FSTYPES:
            continue
        try:
            vol = Volume(mount, device, fstype, int(size) * 1024, int(used) * 1024, int(avail) * 1024)
        except ValueError:
            continue
        key = (device, size)
        if (cur := by_dev.get(key)) is None:
            by_dev[key] = vol
            continue
        # Same filesystem mounted several times (bind mounts, btrfs subvolumes):
        # keep the most significant mount point as the main one.
        if _mount_rank(mount) < _mount_rank(cur.mount):
            vol.other_mounts = [cur.mount, *cur.other_mounts]
            by_dev[key] = vol
        else:
            cur.other_mounts.append(mount)
    return sorted(by_dev.values(), key=lambda v: v.mount)


@dataclass
class StatusData:
    """Fast-changing status of the computer."""

    uptime: float
    load: tuple[float, float, float]
    mem_total: int
    mem_available: int
    volumes: list[Volume]
    kernel: str | None
    init: str
    system_state: str | None
    failed_units: list[str]
    units: dict[str, UnitState] = field(default_factory=dict)
    reboot_reasons: list[str] = field(default_factory=list)
    services_running: list[str] | None = None
    services_enabled: list[str] | None = None
    services_disabled: list[str] | None = None
    sections: dict[str, list[str]] = field(default_factory=dict, repr=False)
    location: Location | None = None  # only read on slow polls
    addresses: dict[str, list[str]] = field(default_factory=dict)  # interface -> IPs
    primary_ip: str | None = None
    wwan_enabled: bool | None = None  # mobile broadband radio; None: no modem
    battery_level: int | None = None  # percent (mean when there are several)
    battery_status: str | None = None  # Charging, Discharging, Full...
    public_ip: dict[str, str] | None = None  # {"ipv4": ..., "ipv6": ...}; slow polls

    @property
    def mem_used_pct(self) -> float | None:
        """Memory usage in percent."""
        if not self.mem_total:
            return None
        return round(100 * (self.mem_total - self.mem_available) / self.mem_total, 1)


def status_script(units: list[str], slow: bool = False, public_ip: bool = False) -> str:
    """Build the status script. Slow polls also run the readings that need sudo."""
    flag = "GLC_SLOW=1\n" if slow else ""
    if public_ip:
        flag += "GLC_PUBLIC_IP=1\n"
    return f"UNITS={shlex.quote(' '.join(units))}\n{flag}" + STATUS_SCRIPT


def parse_status(text: str, units: list[str]) -> StatusData:
    """Parse the status script output."""
    sec = split_sections(text)
    uptime = float(_first(sec, "uptime") or 0)
    load_parts = (_first(sec, "load") or "0 0 0").split()
    load = tuple(float(x) for x in load_parts[:3])
    mem: dict[str, int] = {}
    for line in sec.get("mem", []):
        key, _, rest = line.partition(":")
        mem[key] = int(rest.split()[0]) * 1024 if rest.split() else 0
    running = enabled = disabled = None
    if "svc_files" in sec:
        running = sorted(u.strip() for u in sec.get("svc_running", []) if u.strip())
        enabled, disabled = [], []
        for line in sec["svc_files"]:
            name, _, state = line.strip().partition(" ")
            if not name or name.endswith("@.service"):
                continue
            if state in ("enabled", "enabled-runtime"):
                enabled.append(name)
            elif state == "disabled":
                disabled.append(name)
        enabled.sort()
        disabled.sort()
    init = _first(sec, "init") or "sysv"
    unit_states: dict[str, UnitState] = {}
    if init == "systemd":
        block: dict[str, str] = {}
        blocks: list[dict[str, str]] = []
        for line in [*sec.get("units", []), ""]:
            if not line.strip():
                if block:
                    blocks.append(block)
                    block = {}
                continue
            key, _, value = line.partition("=")
            block[key] = value
        for blk in blocks:
            name = blk.get("Id", "")
            state = blk.get("ActiveState", "unknown")
            unit_states[name] = UnitState(
                name=name,
                active=state in ("active", "reloading"),
                active_state=state,
                sub_state=blk.get("SubState"),
                description=blk.get("Description"),
                unit_file_state=blk.get("UnitFileState") or None,
                loaded=blk.get("LoadState") != "not-found",
            )
    else:
        for line in sec.get("sysv", []):
            name, _, code = line.rpartition(" ")
            unit_states[name] = UnitState(
                name=name, active=code == "0", active_state="active" if code == "0" else "inactive"
            )
    # Requested units that systemctl did not report (e.g. alias names)
    for unit in units:
        if unit not in unit_states:
            for state in unit_states.values():
                if state.name.removesuffix(".service") == unit.removesuffix(".service"):
                    unit_states[unit] = state
                    break
    return StatusData(
        uptime=uptime,
        load=load,  # type: ignore[arg-type]
        mem_total=mem.get("MemTotal", 0),
        mem_available=mem.get("MemAvailable", 0),
        volumes=parse_volumes(sec.get("volumes", [])),
        kernel=_first(sec, "kernel"),
        init=init,
        system_state=_first(sec, "state"),
        failed_units=[u for u in sec.get("failed", []) if u.strip()],
        units=unit_states,
        reboot_reasons=[r.strip() for r in sec.get("reboot", []) if r.strip()],
        services_running=running,
        services_enabled=enabled,
        services_disabled=disabled,
        sections={k: v for k, v in sec.items() if k in ("gpu", "battery", "pmic", "rapl")},
        location=parse_location(sec),
        **_parse_addresses(sec),
        **_parse_battery(sec),
        wwan_enabled=(
            {"enabled": True, "disabled": False}.get(_first(sec, "wwan") or "")
            if "wwan" in sec
            else None
        ),
    )


def _parse_battery(sec: dict[str, list[str]]) -> dict:
    levels: list[int] = []
    status = None
    for line in sec.get("battery_level", []):
        level, _, state = line.strip().partition(" ")
        if level.isdigit():
            levels.append(min(int(level), 100))
            status = status or state.strip() or None
    if not levels:
        return {}
    return {"battery_level": round(sum(levels) / len(levels)), "battery_status": status}


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _parse_addresses(sec: dict[str, list[str]]) -> dict:
    """Addresses per interface, the one of the default route, and the public ones."""
    addresses: dict[str, list[str]] = {}
    default = None
    for line in sec.get("ip", []):
        name, _, value = line.strip().partition(" ")
        if name == "default":
            default = value.strip() or None
            continue
        addr = value.split("/")[0]
        if _is_ip(addr):
            addresses.setdefault(name, []).append(addr)
    candidates = addresses.get(default or "", []) or [a for v in addresses.values() for a in v]
    primary = next((a for a in candidates if ":" not in a), candidates[0] if candidates else None)
    public = None
    if "public_ip" in sec:
        public = {}
        for line in sec["public_ip"]:
            family, _, value = line.strip().partition(" ")
            if family in ("4", "6") and _is_ip(value.strip()):
                public[f"ipv{family}"] = value.strip()
    return {"addresses": addresses, "primary_ip": primary, "public_ip": public}


@dataclass
class Location:
    """A GNSS fix."""

    latitude: float
    longitude: float
    altitude: float | None = None
    accuracy: float | None = None  # metres
    source: str = "gpsd"


def _coord(value: object) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def parse_location(sec: dict[str, list[str]]) -> Location | None:
    """Position from gpsd (TPV report) or ModemManager (mmcli -K)."""
    for line in sec.get("gpsd", []):
        try:
            tpv = json.loads(line)
        except ValueError:
            continue
        lat, lon = _coord(tpv.get("lat")), _coord(tpv.get("lon"))
        if lat is None or lon is None or int(tpv.get("mode", 0) or 0) < 2:
            continue
        errors = [e for e in (_coord(tpv.get("epx")), _coord(tpv.get("epy"))) if e is not None]
        alt = tpv.get("altMSL", tpv.get("alt"))
        return Location(
            lat,
            lon,
            _coord(alt) if alt is not None else None,
            _coord(tpv["eph"]) if "eph" in tpv else (max(errors) if errors else None),
            "gpsd",
        )
    mm: dict[str, float | None] = {}
    for line in sec.get("mmloc", []):
        key, _, value = line.partition(":")
        mm[key.strip().rsplit(".", 1)[-1]] = _coord(value.strip())
    lat, lon = mm.get("latitude"), mm.get("longitude")
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        return Location(lat, lon, mm.get("altitude"), None, "modemmanager")
    return None


LIST_UNITS_SCRIPT = r"""
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl list-unit-files --type=service --no-legend --no-pager --plain 2>/dev/null \
    | awk '$2=="enabled" || $2=="static" || $2=="alias" || $2=="disabled" {print $1}' | grep -v '@\.service$'
  systemctl list-units --type=service --all --no-legend --no-pager --plain 2>/dev/null | awk '{print $1}'
elif command -v rc-service >/dev/null 2>&1; then
  rc-service -l 2>/dev/null
else
  ls /etc/init.d 2>/dev/null
fi
"""


# Service names never need anything else; a leading "-" would be an option.
UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9_@][A-Za-z0-9_@.:\\+-]*$")


def parse_unit_list(text: str) -> list[str]:
    """Parse the unit list."""
    return sorted({line.strip() for line in text.splitlines() if line.strip()})


def unit_command(init: str, unit: str, command: str) -> str:
    """Build a command to control a service."""
    if not UNIT_NAME_RE.match(unit):
        raise ValueError(f"Invalid service name: {unit!r}")
    if init == "systemd":
        return f"$SUDO systemctl {command} -- {shlex.quote(unit)}"
    if init == "openrc" and command not in ("enable", "disable"):
        return f"$SUDO rc-service -- {shlex.quote(unit)} {command}"
    if command in ("enable", "disable"):
        return (
            f"if command -v rc-update >/dev/null; then $SUDO rc-update "
            f"{'add' if command == 'enable' else 'del'} {shlex.quote(unit)}; "
            f"else $SUDO update-rc.d {shlex.quote(unit)} {command}; fi"
        )
    return f"$SUDO service {shlex.quote(unit)} {command}"


# --------------------------------------------------------------------------
# Power

def power_script(init: str | None, action: str) -> str:
    """Build the power action command. Returns quickly; the action is delayed."""
    systemd_verb = {
        POWER_SUSPEND: "suspend",
        POWER_HIBERNATE: "hibernate",
        POWER_OFF: "poweroff",
        POWER_REBOOT: "reboot",
    }[action]
    fallback = {
        POWER_SUSPEND: "loginctl suspend || pm-suspend || echo mem > /sys/power/state",
        POWER_HIBERNATE: "loginctl hibernate || pm-hibernate || echo disk > /sys/power/state",
        POWER_OFF: "poweroff || shutdown -h now",
        POWER_REBOOT: "reboot || shutdown -r now",
    }[action]
    return f"""
if command -v systemd-run >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  $SUDO systemd-run --quiet --collect --on-active=2 --timer-property=AccuracySec=100ms systemctl {systemd_verb}
else
  $SUDO sh -c {shlex.quote(f'nohup sh -c "sleep 2; {fallback}" >/dev/null 2>&1 &')}
fi
"""


# --------------------------------------------------------------------------
# Screenshot

_SCREENSHOT_INNER = r"""
uid=$(id -u)
p=""
for n in plasmashell kwin_wayland kwin_x11 gnome-shell xfce4-session cinnamon mate-session sway Hyprland labwc wayfire lxqt-session budgie-wm openbox i3 Xwayland; do
  p=$(pgrep -u "$uid" -n -x "$n" 2>/dev/null) && break
done
if [ -n "$p" ] && [ -r "/proc/$p/environ" ]; then
  for kv in $(tr '\0' '\n' < "/proc/$p/environ" | grep -E '^(DISPLAY|WAYLAND_DISPLAY|XAUTHORITY|DBUS_SESSION_BUS_ADDRESS|XDG_RUNTIME_DIR|XDG_CURRENT_DESKTOP|SWAYSOCK|HYPRLAND_INSTANCE_SIGNATURE)='); do
    export "$kv"
  done
fi
: "${XDG_RUNTIME_DIR:=/run/user/$uid}"; export XDG_RUNTIME_DIR
: "${DBUS_SESSION_BUS_ADDRESS:=unix:path=$XDG_RUNTIME_DIR/bus}"; export DBUS_SESSION_BUS_ADDRESS
if [ -z "$WAYLAND_DISPLAY" ] && [ -z "$DISPLAY" ]; then
  WAYLAND_DISPLAY=$(ls "$XDG_RUNTIME_DIR" 2>/dev/null | grep -m1 -E '^wayland-[0-9]+$')
  [ -n "$WAYLAND_DISPLAY" ] && export WAYLAND_DISPLAY || export DISPLAY=:0
fi
f=$(mktemp --suffix=.png 2>/dev/null) || f=$(mktemp)
trap 'rm -f "$f"' EXIT
ok=""
desk=$(printf '%s' "$XDG_CURRENT_DESKTOP" | tr 'A-Z' 'a-z')
try() { "$@" >/dev/null 2>&1 && [ -s "$f" ] && ok=1; }
case "$desk" in
  *kde*) [ -z "$ok" ] && command -v spectacle >/dev/null && try timeout 20 spectacle -b -n -f -o "$f" ;;
  *gnome*) [ -z "$ok" ] && command -v gnome-screenshot >/dev/null && try timeout 20 gnome-screenshot -f "$f" ;;
esac
[ -z "$ok" ] && [ -n "$WAYLAND_DISPLAY" ] && command -v grim >/dev/null && try timeout 20 grim "$f"
[ -z "$ok" ] && command -v spectacle >/dev/null && try timeout 20 spectacle -b -n -f -o "$f"
[ -z "$ok" ] && command -v gnome-screenshot >/dev/null && try timeout 20 gnome-screenshot -f "$f"
[ -z "$ok" ] && [ -n "$DISPLAY" ] && command -v import >/dev/null && try timeout 20 import -window root "$f"
[ -z "$ok" ] && [ -n "$DISPLAY" ] && command -v scrot >/dev/null && try timeout 20 scrot -o "$f"
if [ -z "$ok" ]; then echo "No screenshot tool worked (desktop: $desk)" >&2; exit 3; fi
cat "$f"
"""

SCREENSHOT_SCRIPT = (
    "INNER=$(cat <<'GLCINNER'\n"
    + _SCREENSHOT_INNER
    + "\nGLCINNER\n)\n"
    + r"""
suid=""
for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
  info=$(loginctl show-session "$s" -p Type -p Active -p User 2>/dev/null)
  t=$(printf '%s\n' "$info" | sed -n 's/^Type=//p')
  a=$(printf '%s\n' "$info" | sed -n 's/^Active=//p')
  u=$(printf '%s\n' "$info" | sed -n 's/^User=//p')
  if [ "$a" = yes ] && { [ "$t" = wayland ] || [ "$t" = x11 ]; }; then suid=$u; break; fi
done
if [ -z "$suid" ]; then echo "No active graphical session" >&2; exit 2; fi
if [ "$suid" = "$(id -u)" ]; then
  sh -c "$INNER"
else
  $SUDO -u "#$suid" sh -c "$INNER"
fi
"""
)


# --------------------------------------------------------------------------
# Package updates


@dataclass
class Package:
    """A pending update."""

    name: str
    current: str | None
    new: str | None


@dataclass
class BackendResult:
    """Result of checking one update backend."""

    key: str
    packages: list[Package] = field(default_factory=list)
    error: str | None = None


DETECT_SCRIPT = r"""
for c in pacman checkupdates yay paru dnf5 dnf yum microdnf rpm-ostree bootc zypper apt-get apk flatpak snap fwupdmgr; do
  command -v "$c" >/dev/null 2>&1 && echo "$c"
done
# Image-based systems (Fedora Atomic/Silverblue/CoreOS, RHEL image mode)
[ -e /run/ostree-booted ] && echo "@ostree-booted"
exit 0
"""

_ARROW_RE = re.compile(r"^(\S+)\s+(\S+)\s+->\s+(\S+)")
# "busybox-1.36.1-r15 < 1.36.1-r20" (apk version -v -l '<')
_APK_RE = re.compile(r"^(\S+?)-(\d[^\s-]*(?:-r\d+)?)\s+<\s+(\S+)")


def detect_backends(text: str, is_root: bool) -> dict[str, str]:
    """Return backend key -> helper tool from the detect script output."""
    tools = set(text.split())
    found: dict[str, str] = {}
    if "pacman" in tools and "checkupdates" in tools:
        found[BACKEND_PACMAN] = "pacman"
    if not is_root:
        if "yay" in tools:
            found[BACKEND_AUR] = "yay"
        elif "paru" in tools:
            found[BACKEND_AUR] = "paru"
    if "@ostree-booted" in tools and ("rpm-ostree" in tools or "bootc" in tools):
        # The system is an image: dnf/yum cannot upgrade it.
        found[BACKEND_OSTREE] = "rpm-ostree" if "rpm-ostree" in tools else "bootc"
    elif rpm := next((t for t in ("dnf5", "dnf", "yum", "microdnf") if t in tools), None):
        # dnf5 (Fedora 41+), dnf (Fedora 22+/RHEL 8+), yum (RHEL/CentOS <= 7),
        # microdnf (minimal images; it cannot list updates, only install them)
        found[BACKEND_DNF] = rpm
    elif "zypper" in tools:
        found[BACKEND_ZYPPER] = "zypper"
    if "apt-get" in tools:
        found[BACKEND_APT] = "apt"
    if "apk" in tools:
        found[BACKEND_APK] = "apk"
    if "flatpak" in tools:
        found[BACKEND_FLATPAK] = "flatpak"
    if "snap" in tools:
        found[BACKEND_SNAP] = "snap"
    if "fwupdmgr" in tools:
        found[BACKEND_FIRMWARE] = "fwupdmgr"
    return found


def check_script(backend: str, tool: str) -> str:
    """Command that lists pending updates for a backend."""
    return {
        BACKEND_PACMAN: 'checkupdates --nocolor; rc=$?; [ $rc -eq 2 ] && exit 0; exit $rc',
        BACKEND_AUR: f"{tool} -Qua 2>/dev/null; exit 0",
        BACKEND_DNF: (
            "exit 0"
            if tool == "microdnf"
            else f"{tool} -q check-update 2>/dev/null; rc=$?; [ $rc -eq 100 ] && exit 0; exit $rc"
        ),
        BACKEND_OSTREE: (
            "$SUDO bootc upgrade --check 2>&1"
            if tool == "bootc"
            else "rpm-ostree upgrade --check 2>&1; rc=$?; [ $rc -eq 77 ] && exit 0; exit $rc"
        ),
        BACKEND_APK: "$SUDO apk update -q >/dev/null 2>&1; apk version -v -l '<' 2>/dev/null; exit 0",
        BACKEND_ZYPPER: "$SUDO zypper --non-interactive --quiet --xmlout list-updates",
        BACKEND_APT: (
            "$SUDO apt-get -o DPkg::Lock::Timeout=60 update -qq >/dev/null 2>&1; "
            "apt list --upgradable 2>/dev/null"
        ),
        BACKEND_FLATPAK: (
            "echo @updates; flatpak remote-ls --updates --columns=application,version,branch 2>/dev/null;"
            " echo @installed; flatpak list --columns=application,version,branch 2>/dev/null; exit 0"
        ),
        BACKEND_SNAP: "snap refresh --list 2>/dev/null; exit 0",
        BACKEND_FIRMWARE: (
            "$SUDO fwupdmgr refresh >/dev/null 2>&1; "
            "fwupdmgr get-updates --json 2>/dev/null; exit 0"
        ),
    }[backend]


def install_script(backend: str, tool: str) -> str:
    """Command that installs all pending updates for a backend."""
    return {
        BACKEND_PACMAN: "$SUDO pacman -Syu --noconfirm --noprogressbar",
        # Never build AUR packages on top of outdated repo packages (partial
        # upgrade): bring the system up to date first.
        BACKEND_AUR: (
            "$SUDO pacman -Syu --noconfirm --noprogressbar && "
            f'{tool} -Sua --noconfirm --sudoflags="$SUDOFLAGS" --removemake'
            + (" --answerdiff None --answerclean None" if tool == "yay" else "")
        ),
        BACKEND_DNF: f"$SUDO {tool} -y {'update' if tool == 'yum' else 'upgrade'}",
        # Stages a new deployment; it becomes active on the next reboot.
        BACKEND_OSTREE: "$SUDO bootc upgrade" if tool == "bootc" else "$SUDO rpm-ostree upgrade",
        BACKEND_APK: "$SUDO apk upgrade --update-cache --no-progress",
        BACKEND_ZYPPER: "$SUDO zypper --non-interactive update",
        BACKEND_APT: (
            "$SUDO apt-get -o DPkg::Lock::Timeout=300 update -qq; "
            "$SUDO env DEBIAN_FRONTEND=noninteractive apt-get -y -o DPkg::Lock::Timeout=300 "
            "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade"
        ),
        BACKEND_FLATPAK: (
            "rc=0; $SUDO flatpak update -y --noninteractive --system || rc=$?; "
            "flatpak update -y --noninteractive --user || rc=$?; exit $rc"
        ),
        BACKEND_SNAP: "$SUDO snap refresh",
        BACKEND_FIRMWARE: "$SUDO fwupdmgr update -y --no-reboot-check",
    }[backend]


def parse_check(backend: str, text: str) -> list[Package]:
    """Parse the output of a check command."""
    pkgs: list[Package] = []
    if backend in (BACKEND_PACMAN, BACKEND_AUR):
        for line in text.splitlines():
            if m := _ARROW_RE.match(line.strip()):
                pkgs.append(Package(m[1], m[2], m[3]))
    elif backend == BACKEND_DNF:
        seen: set[str] = set()
        for line in text.splitlines():
            if line.startswith("Obsoleting"):
                break  # what follows are replaced packages, not updates
            parts = line.split()
            if len(parts) != 3 or "." not in parts[0] or line.startswith(("Security:", "Last metadata")):
                continue
            name = parts[0].rsplit(".", 1)[0]
            if name not in seen:
                seen.add(name)
                pkgs.append(Package(name, None, parts[1]))
    elif backend == BACKEND_OSTREE:
        # rpm-ostree: "AvailableUpdate:" block with Version/Commit/Diff lines.
        # bootc: "Update available for: <image>" (+ "Version: ...").
        info: dict[str, str] = {}
        for line in text.splitlines():
            key, sep, value = line.strip().partition(":")
            if sep and value.strip():
                info.setdefault(key.strip().lower(), value.strip())
        if "availableupdate" in text.lower().replace(" ", "") or "update available for" in info:
            new = (
                info.get("version", "").split(" ")[0]
                or info.get("commit", "")[:12]
                or info.get("digest", "")[-12:]
            )
            name = "system image"
            if diff := info.get("diff"):
                name += f" ({diff})"
            pkgs.append(Package(name, None, new or "new"))
    elif backend == BACKEND_APK:
        for line in text.splitlines():
            if m := _APK_RE.match(line.strip()):
                pkgs.append(Package(m[1], m[2], m[3]))
    elif backend == BACKEND_ZYPPER:
        start = text.find("<")
        if start >= 0:
            try:
                root = ET.fromstring(text[start:])
            except ET.ParseError:
                return pkgs
            for upd in root.iter("update"):
                if upd.get("kind", "package") == "package":
                    pkgs.append(Package(upd.get("name", "?"), None, upd.get("edition")))
    elif backend == BACKEND_APT:
        rx = re.compile(r"^([^/\s]+)/\S+\s+(\S+)\s+\S+\s+\[upgradable from: ([^\]]+)\]")
        for line in text.splitlines():
            if m := rx.match(line):
                pkgs.append(Package(m[1], m[3], m[2]))
    elif backend == BACKEND_FLATPAK:
        sec = split_sections(text)
        installed: dict[str, str] = {}
        for line in sec.get("installed", []):
            cols = line.split("\t")
            if len(cols) >= 3:
                installed[f"{cols[0]}//{cols[2]}"] = cols[1]
        seen: set[str] = set()
        for line in sec.get("updates", []):
            cols = line.split("\t")
            if not cols or not cols[0] or line in seen:
                continue
            seen.add(line)
            branch = cols[2] if len(cols) > 2 else ""
            key = f"{cols[0]}//{branch}"
            pkgs.append(
                Package(
                    cols[0] if not branch or branch == "stable" else key,
                    installed.get(key) or None,
                    (cols[1] if len(cols) > 1 else "") or None,
                )
            )
    elif backend == BACKEND_SNAP:
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                pkgs.append(Package(parts[0], None, parts[1]))
    elif backend == BACKEND_FIRMWARE:
        start = text.find("{")
        if start >= 0:
            try:
                data = json.loads(text[start:])
            except ValueError:
                return pkgs
            for dev in data.get("Devices", []):
                releases = dev.get("Releases") or []
                if releases:
                    pkgs.append(
                        Package(dev.get("Name", "?"), dev.get("Version"), releases[0].get("Version"))
                    )
    return pkgs


# --------------------------------------------------------------------------
# Containers

CONTAINERS_SCRIPT = r"""
TAB=$(printf '\t')
FMT='{{json .Name}}	{{json .Config.Image}}	{{json .Image}}	{{json .State.Status}}	{{json .Config.Labels}}'
dump() {
  # $1 = engine label, rest = command prefix
  # Every call has a timeout: a stuck engine (stale lock) must not leave a
  # hung process behind on each check.
  label=$1; shift
  ids=$(timeout 30 "$@" ps -aq 2>/dev/null) || return 0
  [ -n "$ids" ] || return 0
  echo "@engine"; echo "$label"
  echo "@containers"; timeout 30 "$@" inspect --format "$FMT" $ids 2>/dev/null
  imgs=$(timeout 30 "$@" inspect --format '{{.Image}}' $ids 2>/dev/null | sort -u)
  case "$label" in podman*) idf='{{json .ID}}' ;; *) idf='{{json .Id}}' ;; esac
  echo "@images"; [ -n "$imgs" ] && timeout 30 "$@" image inspect --format "$idf	{{json .RepoDigests}}" $imgs 2>/dev/null
}
if command -v docker >/dev/null 2>&1; then
  running=1
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    systemctl is-active --quiet docker.service || running=""
  fi
  if [ -n "$running" ]; then
    if timeout 20 docker info >/dev/null 2>&1; then dump "docker|docker" docker
    elif timeout 20 $SUDO docker info >/dev/null 2>&1; then dump "docker|\$SUDO docker" $SUDO docker
    fi
  fi
fi
if command -v podman >/dev/null 2>&1; then
  if [ "$(id -u)" = 0 ]; then dump "podman-root|podman" podman
  else
    $SUDO true 2>/dev/null && dump "podman-root|\$SUDO podman" $SUDO podman
    dump "podman-user|podman" podman
  fi
fi
echo "@end"
"""


@dataclass
class Container:
    """A container and its image."""

    engine: str  # docker | podman-root | podman-user
    command: str  # command prefix, may contain $SUDO
    name: str
    image: str
    image_id: str
    status: str
    labels: dict[str, str]
    local_digests: list[str]
    remote_digest: str | None = None
    check_error: str | None = None

    @property
    def key(self) -> str:
        """Unique key of the container on this computer."""
        return f"{self.engine}:{self.name}"

    @property
    def local_digest(self) -> str | None:
        """Main local digest."""
        for d in self.local_digests:
            if "@" in d:
                return d.split("@", 1)[1]
        return None

    @property
    def update_available(self) -> bool:
        """True if the registry has a newer image."""
        if not self.remote_digest or not self.local_digests:
            return False
        return not any(d.endswith("@" + self.remote_digest) for d in self.local_digests)

    @property
    def install_command(self) -> str | None:
        """Command to pull and recreate the container, if we know how."""
        lbl = self.labels
        if wd := lbl.get("com.docker.compose.project.working_dir"):
            project = lbl.get("com.docker.compose.project", "")
            service = lbl.get("com.docker.compose.service", "")
            files = " ".join(
                f"-f {shlex.quote(f)}"
                for f in lbl.get("com.docker.compose.project.config_files", "").split(",")
                if f
            )
            base = f"{self.command} compose -p {shlex.quote(project)} {files}"
            return (
                f"cd {shlex.quote(wd)} && {base} pull {shlex.quote(service)} && "
                f"{base} up -d {shlex.quote(service)}"
            )
        if unit := lbl.get("PODMAN_SYSTEMD_UNIT"):
            ctl = "systemctl --user" if self.engine == "podman-user" else "$SUDO systemctl"
            return (
                f"{self.command} pull -- {shlex.quote(self.image)} && "
                f"{ctl} restart -- {shlex.quote(unit)}"
            )
        return None


def parse_containers(text: str) -> list[Container]:
    """Parse the containers script output (sections repeat per engine)."""
    result: list[Container] = []
    engine = command = ""
    section = None
    pending: list[tuple[str, str, str, str, dict[str, str]]] = []
    digests: dict[str, list[str]] = {}

    def flush() -> None:
        for name, image, image_id, status, labels in pending:
            result.append(
                Container(
                    engine=engine,
                    command=command,
                    name=name,
                    image=image,
                    image_id=image_id,
                    status=status,
                    labels=labels,
                    local_digests=digests.get(image_id, []),
                )
            )
        pending.clear()
        digests.clear()

    for line in text.splitlines():
        if line.startswith("@") and " " not in line.strip():
            new = line[1:].strip()
            if new in ("engine", "end"):
                flush()
            section = new
            continue
        if not line.strip():
            continue
        if section == "engine":
            engine, _, command = line.partition("|")
        elif section == "containers":
            cols = line.split("\t")
            if len(cols) < 5:
                continue
            try:
                name = json.loads(cols[0]).lstrip("/")
                image = json.loads(cols[1])
                image_id = json.loads(cols[2])
                status = json.loads(cols[3])
                labels = json.loads(cols[4]) or {}
            except ValueError:
                continue
            pending.append((name, image, image_id, status, labels))
        elif section == "images":
            cols = line.split("\t")
            if len(cols) < 2:
                continue
            try:
                digests[json.loads(cols[0])] = json.loads(cols[1]) or []
            except ValueError:
                continue
    flush()
    return result


# --------------------------------------------------------------------------
# Wake-on-LAN relay


def wol_relay_script(mac: str, broadcast: str) -> str:
    """Send a magic packet from a Linux computer (used as a relay)."""
    return f"""
MAC={shlex.quote(mac)}; BC={shlex.quote(broadcast)}
if command -v wakeonlan >/dev/null 2>&1; then wakeonlan -i "$BC" "$MAC" && exit 0; fi
if command -v wol >/dev/null 2>&1; then wol -i "$BC" "$MAC" && exit 0; fi
for py in python3 python; do
  if command -v $py >/dev/null 2>&1; then
    $py - "$MAC" "$BC" <<'GLCPY' && exit 0
import socket, sys
mac = sys.argv[1].replace(":", "").replace("-", "")
packet = bytes.fromhex("FF" * 6 + mac * 16)
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for port in (9, 7):
        s.sendto(packet, (sys.argv[2], port))
GLCPY
  fi
done
if command -v etherwake >/dev/null 2>&1; then $SUDO etherwake -b "$MAC" && exit 0; fi
echo "No tool to send Wake-on-LAN (install wakeonlan or python3)" >&2
exit 3
"""


_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")
_IFACE_RE = re.compile(r"^[A-Za-z0-9_.:@ +-]{1,64}$")


def routeros_wol_command(mac: str, interface: str | None) -> str:
    """RouterOS command that sends a magic packet."""
    if not _MAC_RE.match(mac):
        raise ValueError(f"Invalid MAC address: {mac!r}")
    cmd = f"/tool wol mac={mac.upper()}"
    if interface:
        # No quotes, ";" or newlines: they would start another RouterOS command.
        if not _IFACE_RE.match(interface):
            raise ValueError(f"Invalid interface name: {interface!r}")
        cmd += f' interface="{interface}"'
    return cmd


# --------------------------------------------------------------------------
# Detached jobs: long installs run on the computer itself (setsid nohup), so
# they survive a restart of Home Assistant or a dropped SSH connection.

JOB_DIR = '"${XDG_CACHE_HOME:-$HOME/.cache}/gnulinux_computer"'

_RUNNER = r"""J=$1
SUDO="$GLC_SUDO"; SUDOFLAGS="$GLC_SUDOFLAGS"
if [ -n "$GLC_PW" ]; then
  SUDO_ASKPASS=$(mktemp); export SUDO_ASKPASS
  trap 'rm -f "$SUDO_ASKPASS"' EXIT
  cat > "$SUDO_ASKPASS" <<'GLCASK'
#!/bin/sh
printf '%s\n' "$GLC_PW"
GLCASK
  chmod 700 "$SUDO_ASKPASS"
fi
export LC_ALL=C LANG=C
date +%s > "$J.started"
( . "$J.payload" ); rc=$?
echo "$rc" > "$J.rc"
rm -f "$J.pid"
"""

PACMAN_PREFLIGHT = r"""if [ -e /var/lib/pacman/db.lck ] && ! pgrep -x pacman >/dev/null 2>&1; then
  echo "La base de dades de pacman està bloquejada (db.lck orfe, cap pacman en marxa)." >&2
  echo "Comprova-ho i elimina-la: sudo rm /var/lib/pacman/db.lck" >&2
  exit 75
fi
"""


def _job_name(job: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", job)


def job_start_script(job: str, payload: str) -> str:
    """Start payload as a detached job unless another job is running."""
    name = _job_name(job)
    return f"""D={JOB_DIR}; mkdir -p "$D"; chmod 700 "$D"
for p in "$D"/*.pid; do
  [ -e "$p" ] || continue
  if kill -0 "$(cat "$p")" 2>/dev/null; then echo "@busy $(basename "$p" .pid)"; exit 0; fi
  rm -f "$p"
done
J="$D/{name}"; rm -f "$J.rc" "$J.log" "$J.started"
cat > "$J.payload" <<'GLCPAYLOAD'
{payload}
GLCPAYLOAD
cat > "$D/runner.sh" <<'GLCRUNNER'
{_RUNNER}GLCRUNNER
GLC_SUDO="$SUDO" GLC_SUDOFLAGS="$SUDOFLAGS" setsid nohup sh "$D/runner.sh" "$J" > "$J.log" 2>&1 < /dev/null &
echo $! > "$J.pid"
echo "@started {name}"
"""


_JOB_REPORT = """echo "@job $(basename "$J")"
echo "@started $(cat "$J.started" 2>/dev/null)"
[ -f "$J.rc" ] && echo "@finished $(stat -c %Y "$J.rc")"
if [ -f "$J.rc" ]; then echo "@done"; cat "$J.rc"
elif [ -f "$J.pid" ] && kill -0 "$(cat "$J.pid")" 2>/dev/null; then echo "@running"
else echo "@missing"; fi
echo "@log"; tail -n "${GLC_LINES:-80}" "$J.log" 2>/dev/null | tail -c 12000
"""


def job_status_script(job: str, lines: int = 80) -> str:
    """Report state, times, exit code and the end of the log of a job."""
    return f"""D={JOB_DIR}; J="$D/{_job_name(job)}"; GLC_LINES={int(lines)}
""" + _JOB_REPORT


def last_job_script(lines: int = 80) -> str:
    """Same report for the most recently started job (if any)."""
    return f"""D={JOB_DIR}; GLC_LINES={int(lines)}
last=$(ls -t "$D"/*.started 2>/dev/null | head -n 1)
[ -n "$last" ] || exit 0
J="${{last%.started}}"
""" + _JOB_REPORT


JOBS_RUNNING_SCRIPT = f"""D={JOB_DIR}
for p in "$D"/*.pid; do
  [ -e "$p" ] || continue
  kill -0 "$(cat "$p")" 2>/dev/null && basename "$p" .pid
done
"""


@dataclass
class JobState:
    """State of a detached job."""

    state: str  # running | done | missing | timeout
    rc: int | None
    log: str
    job: str | None = None
    started: int | None = None  # epoch seconds
    finished: int | None = None


def parse_job_status(text: str) -> JobState:
    """Parse job_status_script output."""
    head, _, log = text.partition("@log\n")
    lines = [line.strip() for line in head.splitlines() if line.strip()]
    state = next((k for k in ("done", "running", "missing") if f"@{k}" in lines), "missing")
    rc = None
    if state == "done":
        idx = lines.index("@done")
        try:
            rc = int(lines[idx + 1])
        except (IndexError, ValueError):
            rc = -1
    meta: dict[str, str] = {}
    for line in lines:
        key, _, value = line.partition(" ")
        if key in ("@job", "@started", "@finished"):
            meta[key[1:]] = value.strip()

    def _int(v: str | None) -> int | None:
        try:
            return int(v) if v else None
        except ValueError:
            return None

    return JobState(
        state, rc, log, job=meta.get("job") or None,
        started=_int(meta.get("started")), finished=_int(meta.get("finished")),
    )


# --------------------------------------------------------------------------
# eSIM profiles (lpac, https://github.com/estkme-group/lpac)

# lpac is configured through LPAC_* environment variables (which modem and
# how to talk to it); they can be put in /etc/default/lpac.
_LPAC = r"""
command -v lpac >/dev/null 2>&1 || exit 0
if [ -r /etc/default/lpac ]; then set -a; . /etc/default/lpac; set +a; fi
lp() {
  # As the user first; modem devices (MBIM/QMI proxy, AT port) usually need root.
  if out=$(timeout 40 lpac "$@" 2>/dev/null) && printf '%s' "$out" | grep -q '"code":0'; then printf '%s\n' "$out"
  else $SUDO env $(env | grep '^LPAC_') timeout 60 lpac "$@" 2>/dev/null; fi
}
"""

ESIM_LIST_SCRIPT = _LPAC + 'echo "@profiles"; lp profile list\necho "@end"\n'

_ICCID_RE = re.compile(r"^[0-9A-Fa-f]{18,22}$")


@dataclass
class EsimProfile:
    """An eSIM profile stored in the eUICC."""

    iccid: str
    name: str
    enabled: bool
    provider: str | None = None

    @property
    def label(self) -> str:
        """Name shown in Home Assistant (unique thanks to the ICCID tail)."""
        return f"{self.name} (…{self.iccid[-4:]})"


def esim_enable_script(iccid: str) -> str:
    """Enable a profile; the modem then registers with that operator."""
    if not _ICCID_RE.match(iccid):
        raise ValueError(f"Invalid ICCID: {iccid!r}")
    return _LPAC + f"lp profile enable {iccid}\n"


def _lpac_payload(text: str) -> dict | None:
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line).get("payload")
        except (ValueError, AttributeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def lpac_ok(text: str) -> tuple[bool, str]:
    """Whether an lpac command succeeded, and its message."""
    payload = _lpac_payload(text)
    if payload is None:
        return False, text.strip()[-300:]
    data = payload.get("data")
    detail = data if isinstance(data, str) and data else payload.get("message", "")
    return payload.get("code") == 0, str(detail)


def parse_esim_profiles(text: str) -> list[EsimProfile] | None:
    """Parse `lpac profile list`. None when there is no usable eSIM."""
    payload = _lpac_payload(text.partition("@profiles")[2])
    if payload is None or payload.get("code") != 0 or not isinstance(payload.get("data"), list):
        return None
    profiles = []
    for item in payload["data"]:
        iccid = str(item.get("iccid") or "")
        if not _ICCID_RE.match(iccid):
            continue
        # Factory test profiles (no real subscription): enabling one by
        # mistake would only cut the connection.
        if str(item.get("profileClass", "")).lower() == "test":
            continue
        name = (
            item.get("profileNickname")
            or item.get("profileName")
            or item.get("serviceProviderName")
            or iccid
        )
        profiles.append(
            EsimProfile(
                iccid=iccid,
                name=str(name),
                enabled=str(item.get("profileState", "")).lower() in ("enabled", "1"),
                provider=item.get("serviceProviderName") or None,
            )
        )
    return profiles


def wwan_script(enable: bool) -> str:
    """Turn the mobile broadband radio on or off (NetworkManager)."""
    state = "on" if enable else "off"
    return f"nmcli radio wwan {state} 2>/dev/null || $SUDO nmcli radio wwan {state}\n"
