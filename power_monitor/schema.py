"""SQLite schema and primary-metric helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional


POWER_COLUMNS = ("package_w", "cores_w", "uncore_w", "dram_w", "psys_w")

# Domain key → DB column
DOMAIN_TO_COLUMN = {
    "package": "package_w",
    "cores": "cores_w",
    "uncore": "uncore_w",
    "dram": "dram_w",
    "psys": "psys_w",
}


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create database and tables if they don't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS power_samples (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            package_w REAL,
            cores_w   REAL,
            uncore_w  REAL,
            dram_w    REAL,
            psys_w    REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON power_samples(timestamp)")
    conn.commit()
    return conn


def _row_get(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        if hasattr(row, "get"):
            return row.get(key)
        return None


def primary_power(row: Mapping[str, Any]) -> Optional[float]:
    """Return PSYS power when present, otherwise package power."""
    psys = _row_get(row, "psys_w")
    package = _row_get(row, "package_w")
    if psys is not None:
        return float(psys)
    if package is not None:
        return float(package)
    return None


def primary_label(has_psys: bool) -> str:
    """Human-readable label for the primary power metric."""
    return "PSYS (platform)" if has_psys else "Package"


def primary_sql_expr() -> str:
    """SQL expression that coalesces psys_w to package_w."""
    return "COALESCE(psys_w, package_w)"


def map_domain_name_to_key(name: str) -> Optional[str]:
    """Map a hardware domain/channel name to a normalized schema key.

    Returns one of: package, cores, uncore, dram, psys — or None if unknown.
    """
    col = name.lower().replace("-", "_").replace(" ", "_")

    if "psys" in col or "platform" in col:
        return "psys"
    if "dram" in col:
        return "dram"
    if "uncore" in col or "pp1" in col or "igpu" in col:
        return "uncore"
    if "vddcr_soc" in col:
        return "uncore"
    if "pp0" in col:
        return "cores"
    if "core" in col and "uncore" not in col and "package" not in col and "pkg" not in col:
        return "cores"
    if "package" in col or "pkg" in col or "socket" in col or "apu" in col:
        return "package"
    if "vddcr_vdd" in col:
        return "package"
    if col in ("current_socket_energy", "apu_energy"):
        return "package"
    return None
