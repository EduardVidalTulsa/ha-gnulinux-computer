# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Eduard Vidal Tulsà
"""Power consumption from RAPL, GPUs, battery and the Raspberry Pi 5 PMIC."""

from __future__ import annotations

from dataclasses import dataclass, field
import re

# RAPL energy counters (and often the Raspberry Pi PMIC) can only be read by
# root, so those reads go through sudo, which leaves lines in the journal: they
# only run on "slow" polls. RAPL counts energy, so a long interval still gives
# the exact average power over that interval.
SLOW_INTERVAL = 300  # seconds

_PMIC_RE = re.compile(r"^\s*(\S+)_([AV])\s+\w+\(\d+\)=([\d.]+)[AV]\s*$")


@dataclass
class PowerValues:
    """Power readings in watts (None = not available)."""

    cpu: float | None = None  # sum of RAPL package domains
    platform: float | None = None  # RAPL psys (whole platform, Intel)
    gpu: float | None = None
    battery: float | None = None  # only while discharging
    pmic: float | None = None  # Raspberry Pi 5 rails
    rapl_domains: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> tuple[float | None, str | None]:
        """Best estimate of the whole computer and where it comes from."""
        if self.battery is not None:
            return round(self.battery, 1), "battery"
        gpu = self.gpu or 0.0
        if self.platform is not None:
            return round(self.platform + gpu, 1), "psys+gpu" if self.gpu else "psys"
        if self.cpu is not None:
            return round(self.cpu + gpu, 1), "cpu+gpu" if self.gpu else "cpu"
        if self.pmic is not None:
            return round(self.pmic, 1), "pmic"
        if self.gpu is not None:
            return round(gpu, 1), "gpu"
        return None, None


def _gpu(lines: list[str]) -> float | None:
    total, found = 0.0, False
    for line in lines:
        kind, _, value = line.strip().partition(" ")
        try:
            if kind == "nvidia":
                total += float(value)
                found = True
            elif kind == "amdgpu_uw":
                total += int(value) / 1e6
                found = True
        except ValueError:
            continue  # "[N/A]" on some boards
    return round(total, 1) if found else None


def _battery(lines: list[str]) -> float | None:
    total, found = 0.0, False
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "Discharging":
            try:
                total += abs(int(parts[-1])) / 1e6
                found = True
            except ValueError:
                continue
    return round(total, 1) if found else None


def _pmic(lines: list[str]) -> float | None:
    amps: dict[str, float] = {}
    volts: dict[str, float] = {}
    for line in lines:
        if m := _PMIC_RE.match(line):
            (amps if m[2] == "A" else volts)[m[1]] = float(m[3])
    rails = set(amps) & set(volts)
    if not rails:
        return None
    return round(sum(amps[r] * volts[r] for r in rails), 2)


class RaplTracker:
    """Turns RAPL energy counter samples into average power."""

    def __init__(self) -> None:
        """Initialize."""
        self._last: dict[str, tuple[int, int, int]] = {}  # name -> (uj, max_uj, ns)
        self.watts: dict[str, float] = {}
        self.available: bool | None = None  # None until the first read

    @property
    def wants_quick_read(self) -> bool:
        """After the first sample, read again soon to get a first value."""
        return self.available is not False and not self.watts and bool(self._last)

    def update(self, lines: list[str] | None) -> None:
        """Feed the @rapl section (None = not read this time)."""
        if lines is None:
            return
        samples: dict[str, tuple[int, int, int]] = {}
        for line in lines:
            parts = line.split()
            if len(parts) != 4:
                continue
            name = parts[0]
            try:
                uj, max_uj, ns = int(parts[1]), int(parts[2]), int(parts[3])
            except ValueError:
                continue
            key = name
            n = 1
            while key in samples:  # package-0 on several sockets keeps its name
                n += 1
                key = f"{name}#{n}"
            samples[key] = (uj, max_uj, ns)
        self.available = bool(samples)
        for key, (uj, max_uj, ns) in samples.items():
            if (prev := self._last.get(key)) is not None:
                d_uj = uj - prev[0]
                if d_uj < 0:  # counter wrapped
                    d_uj += max_uj or prev[1]
                d_s = (ns - prev[2]) / 1e9
                if d_s > 0:
                    self.watts[key] = round(d_uj / 1e6 / d_s, 1)
        self._last = samples


def compute(
    sections: dict[str, list[str]], rapl: RaplTracker, previous: PowerValues | None = None
) -> PowerValues:
    """Build PowerValues from the status sections and the RAPL tracker.

    A PMIC reading only comes on slow polls when it needs sudo: keep the
    previous value in between.
    """
    rapl.update(sections.get("rapl"))
    pmic = _pmic(sections["pmic"]) if "pmic" in sections else (previous.pmic if previous else None)
    packages = [w for k, w in rapl.watts.items() if k.startswith("package")]
    psys = [w for k, w in rapl.watts.items() if k.startswith("psys")]
    return PowerValues(
        cpu=round(sum(packages), 1) if packages else None,
        platform=round(sum(psys), 1) if psys else None,
        gpu=_gpu(sections.get("gpu", [])),
        battery=_battery(sections.get("battery", [])),
        pmic=pmic,
        rapl_domains=dict(rapl.watts),
    )
