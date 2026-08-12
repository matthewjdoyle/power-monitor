"""Energy measurement backends and auto-selection."""

from __future__ import annotations

import sys
from typing import Optional

from power_monitor.backends.base import Domain, EnergyBackend, energy_delta
from power_monitor.backends.linux_amd_energy import LinuxAmdEnergyBackend
from power_monitor.backends.linux_powercap import LinuxPowercapBackend
from power_monitor.backends.windows_emi import WindowsEmiBackend, probe_emi_channels

__all__ = [
    "Domain",
    "EnergyBackend",
    "energy_delta",
    "LinuxPowercapBackend",
    "LinuxAmdEnergyBackend",
    "WindowsEmiBackend",
    "probe_emi_channels",
    "select_backend",
    "probe_backends",
]


def select_backend() -> Optional[EnergyBackend]:
    """Auto-select the first backend that discovers at least one domain.

    Windows: EMI only.
    Linux: powercap (Intel + modern AMD), then amd_energy hwmon.
    """
    if sys.platform.startswith("win"):
        candidates: list[EnergyBackend] = [WindowsEmiBackend()]
    elif sys.platform.startswith("linux"):
        candidates = [LinuxPowercapBackend(), LinuxAmdEnergyBackend()]
    else:
        candidates = []

    for backend in candidates:
        try:
            domains = backend.discover()
        except OSError:
            continue
        if domains:
            return backend
    return None


def probe_backends() -> list[dict]:
    """Return diagnostic info for all candidate backends on this platform."""
    results: list[dict] = []
    if sys.platform.startswith("win"):
        candidates: list[EnergyBackend] = [WindowsEmiBackend()]
    elif sys.platform.startswith("linux"):
        candidates = [LinuxPowercapBackend(), LinuxAmdEnergyBackend()]
    else:
        candidates = []

    for backend in candidates:
        entry: dict = {
            "backend": backend.name,
            "requires_elevated": backend.requires_elevated(),
            "domains": [],
            "error": None,
        }
        if backend.name == "windows_emi":
            entry["raw_channels"] = probe_emi_channels()
        try:
            domains = backend.discover()
            entry["domains"] = [
                {"name": d.name, "key": d.key, "max_joules": d.max_joules}
                for d in domains
            ]
        except OSError as e:
            entry["error"] = str(e)
        results.append(entry)
    return results
