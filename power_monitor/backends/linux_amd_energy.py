"""Linux amd_energy hwmon fallback backend.

Used when powercap intel-rapl is unavailable. Exposes socket/package and
per-core energy via /sys/class/hwmon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from power_monitor.backends.base import Domain

HWMON_BASE = Path("/sys/class/hwmon")


def _read_energy_uj_file(path: Path) -> float:
    """Read an energy*_input file (microjoules) and return joules."""
    return int(path.read_text().strip()) / 1_000_000.0


def _find_amd_energy_dirs(base: Path = HWMON_BASE) -> list[Path]:
    if not base.exists():
        return []
    found = []
    for hwmon in sorted(base.glob("hwmon*")):
        name_file = hwmon / "name"
        if not name_file.exists():
            continue
        try:
            name = name_file.read_text().strip()
        except OSError:
            continue
        if name == "amd_energy":
            found.append(hwmon)
    return found


def _label_for(energy_input: Path) -> str:
    # energy1_input → energy1_label
    label_path = energy_input.with_name(energy_input.name.replace("_input", "_label"))
    if label_path.exists():
        try:
            return label_path.read_text().strip()
        except OSError:
            pass
    return energy_input.stem


class LinuxAmdEnergyBackend:
    """Read amd_energy hwmon energy counters."""

    name = "linux_amd_energy"

    def __init__(self, base: Path = HWMON_BASE):
        self._base = base

    def requires_elevated(self) -> bool:
        # Often readable without root depending on permissions; still typically
        # needs elevated access similar to RAPL on locked-down systems.
        return True

    def discover(self) -> list[Domain]:
        domains: list[Domain] = []
        socket_energy: Optional[Domain] = None
        core_paths: list[Path] = []

        for hwmon in _find_amd_energy_dirs(self._base):
            for energy_file in sorted(hwmon.glob("energy*_input")):
                label = _label_for(energy_file).lower()
                if "socket" in label or "package" in label or "ept" in label:
                    # Prefer a single socket/package domain
                    if socket_energy is None:
                        socket_energy = Domain(
                            name=label,
                            key="package",
                            read_joules=lambda p=energy_file: _read_energy_uj_file(p),
                            max_joules=None,
                        )
                elif "core" in label:
                    core_paths.append(energy_file)

        if socket_energy is not None:
            domains.append(socket_energy)

        if core_paths:
            # Aggregate all core energy counters into one "cores" reading
            paths = list(core_paths)

            def read_cores(ps: list[Path] = paths) -> float:
                total = 0.0
                for p in ps:
                    try:
                        total += _read_energy_uj_file(p)
                    except (OSError, ValueError):
                        continue
                return total

            domains.append(
                Domain(
                    name="cores_aggregate",
                    key="cores",
                    read_joules=read_cores,
                    max_joules=None,
                )
            )

        return domains
