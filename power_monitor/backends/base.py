"""Energy measurement backend protocol and domain types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable


# Normalized schema keys used by the collector / DB
DomainKey = str  # "package" | "cores" | "uncore" | "dram" | "psys"


@dataclass
class Domain:
    """A single energy counter exposed by a backend."""

    name: str
    key: DomainKey
    read_joules: Callable[[], float]
    max_joules: Optional[float] = None  # wrap range, if known


@runtime_checkable
class EnergyBackend(Protocol):
    """Pluggable energy counter source."""

    name: str

    def discover(self) -> list[Domain]:
        """Return available domains, or empty if this backend cannot run."""
        ...

    def requires_elevated(self) -> bool:
        """True if elevated privileges are required to read counters."""
        ...


def energy_delta(prev_j: float, curr_j: float, max_j: Optional[float]) -> float:
    """Compute energy delta in joules, handling counter wrap.

    If curr < prev, treat as wrap: use max_j when known, else assume wrap
    through zero with range unknown (use curr as approximate delta).
    """
    if curr_j >= prev_j:
        return curr_j - prev_j
    if max_j is not None and max_j > 0:
        return (max_j - prev_j) + curr_j
    return curr_j
