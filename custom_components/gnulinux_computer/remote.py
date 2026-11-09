# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Remote scripts and output parsers."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import shlex
import xml.etree.ElementTree as ET

from .const import (
    BACKEND_APT,
    BACKEND_AUR,
    BACKEND_DNF,
    BACKEND_FIRMWARE,
    BACKEND_FLATPAK,
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


def _first(sec: dict[str, list[str]], key: str) -> str | None:
    for line in sec.get(key, []):
        if line.strip():
            return line.strip()
    return None


def parse_identity(text: str) -> Identity:
    """Parse the identification script output."""
    sec = split_sections(text)
    hostname = _first(sec, "hostname") or "linux"
    return Identity(
        machine_id=_first(sec, "machine_id") or hostname,
        hostname=hostname,
        os_name=_first(sec, "os") or "GNU/Linux",
        manufacturer=_first(sec, "vendor"),
        model=_first(sec, "model"),
        mac=_first(sec, "mac"),
    )


# --------------------------------------------------------------------------
# Status (fast poll)

STATUS_SCRIPT = r"""
echo "@uptime"; cut -d' ' -f1 /proc/uptime
echo "@load"; cut -d' ' -f1-3 /proc/loadavg
echo "@mem"; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
echo "@volumes"
timeout 10 df -PT -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs -x ramfs -x nsfs \
  -x fuse.portal -x fuse.gvfsd-fuse -x fuse.snapfuse -x autofs 2>/dev/null | tail -n +2
echo "@kernel"; uname -r
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
fi
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  echo "@init"; echo systemd
  echo "@state"; systemctl is-system-running 2>/dev/null
  echo "@failed"; systemctl list-units --failed --plain --no-legend --no-pager 2>/dev/null | awk '{print $1}'
  echo "@svc_running"; systemctl list-units --type=service --state=running --no-legend --no-pager --plain 2>/dev/null | awk '{print $1}'
  echo "@svc_files"; systemctl list-unit-files --type=service --no-legend --no-pager --plain 2>/dev/null | awk '{print $1" "$2}'
  echo "@units"
  [ -n "$UNITS" ] && systemctl show --no-pager -p Id,LoadState,ActiveState,SubState,Description,UnitFileState $UNITS 2>/dev/null
else
  echo "@init"; echo sysv
  echo "@sysv"
  for u in $UNITS; do service "$u" status >/dev/null 2>&1; echo "$u $?"; done
fi
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
        if mount.startswith(_SKIP_MOUNT_PREFIXES) and mount != "/":
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

    @property
    def mem_used_pct(self) -> float | None:
        """Memory usage in percent."""
        if not self.mem_total:
            return None
        return round(100 * (self.mem_total - self.mem_available) / self.mem_total, 1)


def status_script(units: list[str]) -> str:
    """Build the status script."""
    return f"UNITS={shlex.quote(' '.join(units))}\n" + STATUS_SCRIPT


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
    )


LIST_UNITS_SCRIPT = r"""
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl list-unit-files --type=service --no-legend --no-pager --plain 2>/dev/null \
    | awk '$2=="enabled" || $2=="static" || $2=="alias" || $2=="disabled" {print $1}' | grep -v '@\.service$'
  systemctl list-units --type=service --all --no-legend --no-pager --plain 2>/dev/null | awk '{print $1}'
else
  ls /etc/init.d 2>/dev/null
fi
"""


def parse_unit_list(text: str) -> list[str]:
    """Parse the unit list."""
    return sorted({line.strip() for line in text.splitlines() if line.strip()})


def unit_command(init: str, unit: str, command: str) -> str:
    """Build a command to control a service."""
    if init == "systemd":
        return f"$SUDO systemctl {command} -- {shlex.quote(unit)}"
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
f=$(mktemp --suffix=.png)
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
for c in pacman checkupdates yay paru dnf zypper apt-get flatpak snap fwupdmgr; do
  command -v "$c" >/dev/null 2>&1 && echo "$c"
done
"""

_ARROW_RE = re.compile(r"^(\S+)\s+(\S+)\s+->\s+(\S+)")


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
    if "dnf" in tools:
        found[BACKEND_DNF] = "dnf"
    elif "zypper" in tools:
        found[BACKEND_ZYPPER] = "zypper"
    if "apt-get" in tools:
        found[BACKEND_APT] = "apt"
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
        BACKEND_DNF: 'dnf -q check-update 2>/dev/null; rc=$?; [ $rc -eq 100 ] && exit 0; exit $rc',
        BACKEND_ZYPPER: "$SUDO zypper --non-interactive --quiet --xmlout list-updates",
        BACKEND_APT: "$SUDO apt-get update -qq >/dev/null 2>&1; apt list --upgradable 2>/dev/null",
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
        BACKEND_AUR: (
            f'{tool} -Sua --noconfirm --sudoflags="$SUDOFLAGS" --removemake'
            + (" --answerdiff None --answerclean None" if tool == "yay" else "")
        ),
        BACKEND_DNF: "$SUDO dnf -y upgrade",
        BACKEND_ZYPPER: "$SUDO zypper --non-interactive update",
        BACKEND_APT: (
            "$SUDO env DEBIAN_FRONTEND=noninteractive apt-get -y "
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
        for line in text.splitlines():
            parts = line.split()
            if line.startswith(("Obsoleting", "Security:", "Last metadata")) or len(parts) != 3:
                continue
            name = parts[0].rsplit(".", 1)[0]
            pkgs.append(Package(name, None, parts[1]))
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
  label=$1; shift
  ids=$("$@" ps -aq 2>/dev/null) || return 0
  [ -n "$ids" ] || return 0
  echo "@engine"; echo "$label"
  echo "@containers"; "$@" inspect --format "$FMT" $ids 2>/dev/null
  imgs=$("$@" inspect --format '{{.Image}}' $ids 2>/dev/null | sort -u)
  case "$label" in podman*) idf='{{json .ID}}' ;; *) idf='{{json .Id}}' ;; esac
  echo "@images"; [ -n "$imgs" ] && "$@" image inspect --format "$idf	{{json .RepoDigests}}" $imgs 2>/dev/null
}
if command -v docker >/dev/null 2>&1; then
  running=1
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    systemctl is-active --quiet docker.service || running=""
  fi
  if [ -n "$running" ]; then
    if docker info >/dev/null 2>&1; then dump "docker|docker" docker
    elif $SUDO docker info >/dev/null 2>&1; then dump "docker|\$SUDO docker" $SUDO docker
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
                f"{self.command} pull {shlex.quote(self.image)} && "
                f"{ctl} restart {shlex.quote(unit)}"
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


def routeros_wol_command(mac: str, interface: str | None) -> str:
    """RouterOS command that sends a magic packet."""
    cmd = f"/tool wol mac={mac.upper()}"
    if interface:
        cmd += f' interface="{interface}"'
    return cmd
