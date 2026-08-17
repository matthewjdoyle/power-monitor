"""Linux powercap RAPL backend (Intel and modern AMD).

Modern kernels expose AMD Zen RAPL counters under the intel-rapl powercap
tree despite the name. Domain names come from each directory's `name` file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from power_monitor.backends.base import Domain
from power_monitor.schema import map_domain_name_to_key

RAPL_BASE = Path("/sys/class/powercap")


def _read_energy_uj(path: Path) -> float:
    """Read energy_uj and return joules."""
    microjoules = int(path.read_text().strip())
    return microjoules / 1_000_000.0


def _read_max_joules(rapl_dir: Path) -> Optional[float]:
    """Read max_energy_range_uj if present, as joules."""
    max_file = rapl_dir / "max_energy_range_uj"
    if not max_file.exists():
        return None
    try:
        return int(max_file.read_text().strip()) / 1_000_000.0
    except (OSError, ValueError):
        return None


def _domain_from_dir(rapl_dir: Path) -> Optional[Domain]:
    energy_file = rapl_dir / "energy_uj"
    if not energy_file.exists():
        return None
    name_file = rapl_dir / "name"
    if name_file.exists():
        name = name_file.read_text().strip()
    else:
        name = rapl_dir.name
    key = map_domain_name_to_key(name)
    if key is None:
        return None
    max_j = _read_max_joules(rapl_dir)
    # Bind path in default arg to avoid late-binding issues
    return Domain(
        name=name,
        key=key,
        read_joules=lambda p=energy_file: _read_energy_uj(p),
        max_joules=max_j,
    )


class LinuxPowercapBackend:
    """Read RAPL energy counters from /sys/class/powercap/intel-rapl:*."""

    name = "linux_powercap"

    def __init__(self, base: Path = RAPL_BASE):
        self._base = base

    def requires_elevated(self) -> bool:
        return True

    def discover(self) -> list[Domain]:
        if not self._base.exists():
            return []

        domains: list[Domain] = []
        seen_keys: set[str] = set()

        for rapl_dir in sorted(self._base.glob("intel-rapl:*")):
            # Skip nested dirs at this level (intel-rapl:0:0 matches :* too on
            # some glob implementations — filter by depth of colon count)
            if rapl_dir.name.count(":") != 1:
                continue
            domain = _domain_from_dir(rapl_dir)
            if domain is not None and domain.key not in seen_keys:
                # Prefer first package (socket 0) for single-socket systems
                domains.append(domain)
                seen_keys.add(domain.key)

            for sub_dir in sorted(rapl_dir.glob("intel-rapl:*:*")):
                if sub_dir.name.count(":") != 2:
                    continue
                sub = _domain_from_dir(sub_dir)
                if sub is not None and sub.key not in seen_keys:
                    domains.append(sub)
                    seen_keys.add(sub.key)

        return domains
