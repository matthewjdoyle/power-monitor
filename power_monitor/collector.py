#!/usr/bin/env python3
"""
power-monitor-collector -- RAPL energy monitoring daemon.

Reads Intel RAPL energy counters from /sys/class/powercap/intel-rapl/ every
INTERVAL seconds, computes instantaneous power (delta_energy / delta_time),
and logs to a SQLite database.

Must run as root -- RAPL sysfs files are readable only by root.
Runs as a foreground process; managed by systemd.

Configuration via environment variables:
  POWER_MONITOR_DATA_DIR     Data directory (default: ~/.local/share/power-monitor)
  POWER_MONITOR_DB           SQLite database path (default: <data_dir>/power.db)
  POWER_MONITOR_SAMPLE_INTERVAL  Seconds between samples (default: 10)
"""

import os
import sys
import time
import signal
import socket
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

# -- Config ----------------------------------------------------------------
INTERVAL = int(os.environ.get("POWER_MONITOR_SAMPLE_INTERVAL", "10"))
DATA_DIR = Path(os.environ.get("POWER_MONITOR_DATA_DIR",
                               os.path.expanduser("~/.local/share/power-monitor")))
DB_PATH = Path(os.environ.get("POWER_MONITOR_DB", str(DATA_DIR / "power.db")))
RAPL_BASE = Path("/sys/class/powercap")

# We'll keep the DB owned by the regular user so the CLI (running as user)
# can read it.
DB_UID = None
DB_GID = None

# Graceful shutdown
running = True


def handle_signal(signum, frame):
    global running
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# -- RAPL discovery --------------------------------------------------------

def discover_rapl_domains() -> list[tuple[str, Path]]:
    """
    Scan /sys/class/powercap/intel-rapl*/ for energy_uj files.
    Returns a list of (name, energy_uj_path) tuples.
    """
    domains = []
    for rapl_dir in sorted(RAPL_BASE.glob("intel-rapl:*")):
        energy_file = rapl_dir / "energy_uj"
        if energy_file.exists():
            name_file = rapl_dir / "name"
            if name_file.exists():
                name = name_file.read_text().strip()
            else:
                name = rapl_dir.name
            domains.append((name, energy_file))

        for sub_dir in sorted(rapl_dir.glob("intel-rapl:*:*")):
            energy_file = sub_dir / "energy_uj"
            name_file = sub_dir / "name"
            if energy_file.exists() and name_file.exists():
                name = name_file.read_text().strip()
                domains.append((name, energy_file))

    return domains


def read_energy(domain_path: Path) -> float:
    """Read energy_uj and return joules (float)."""
    raw = domain_path.read_text().strip()
    microjoules = int(raw)
    return microjoules / 1_000_000.0


# -- Database --------------------------------------------------------------

def init_db() -> sqlite3.Connection:
    """Create database and tables if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
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


def resolve_db_owner():
    """Figure out the UID/GID of the home directory so we can chown the DB."""
    global DB_UID, DB_GID
    home_dir = os.path.expanduser("~")
    try:
        st = os.stat(home_dir)
        DB_UID = st.st_uid
        DB_GID = st.st_gid
    except OSError:
        pass


def ensure_db_owned():
    """Make sure the DB file is owned by the regular user, not root."""
    if DB_UID is not None and DB_PATH.exists():
        try:
            os.chown(DB_PATH, DB_UID, DB_GID)
        except OSError:
            pass


# -- Main loop -------------------------------------------------------------

def main():
    global running

    resolve_db_owner()

    domains = discover_rapl_domains()
    if not domains:
        print("ERROR: No RAPL domains found. Exiting.", file=sys.stderr)
        sys.exit(1)

    domain_map = {name: path for name, path in domains}
    print(f"Discovered RAPL domains: {list(domain_map.keys())}", file=sys.stderr)

    conn = init_db()
    ensure_db_owned()

    prev_energy = {}
    prev_time = time.time()
    for name, path in domains:
        try:
            prev_energy[name] = read_energy(path)
        except (OSError, ValueError) as e:
            print(f"WARN: cannot read {name} ({path}): {e}", file=sys.stderr)
            prev_energy[name] = None

    print(f"Collector running. interval={INTERVAL}s, db={DB_PATH}", file=sys.stderr)

    while running:
        time.sleep(INTERVAL)
        if not running:
            break

        now = time.time()
        dt = now - prev_time

        row = {"timestamp": now}

        for name, path in domains:
            try:
                energy_j = read_energy(path)
            except (OSError, ValueError):
                continue

            if prev_energy.get(name) is not None:
                prev = prev_energy[name]
                de = energy_j - prev if energy_j >= prev else energy_j
                power_w = de / dt if dt > 0 else 0.0
            else:
                power_w = None

            prev_energy[name] = energy_j

            col = name.replace("-", "_").replace(" ", "_")
            if "package" in col:
                row["package_w"] = power_w
            elif "uncore" in col:
                row["uncore_w"] = power_w
            elif "core" in col:
                row["cores_w"] = power_w
            elif "dram" in col:
                row["dram_w"] = power_w
            elif "psys" in col:
                row["psys_w"] = power_w

        prev_time = now

        if "package_w" not in row:
            continue

        for col in ("package_w", "cores_w", "uncore_w", "dram_w", "psys_w"):
            row.setdefault(col, None)

        try:
            conn.execute(
                """INSERT INTO power_samples
                   (timestamp, package_w, cores_w, uncore_w, dram_w, psys_w)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (row["timestamp"], row["package_w"], row["cores_w"],
                 row["uncore_w"], row["dram_w"], row["psys_w"])
            )
            conn.commit()
        except sqlite3.Error as e:
            print(f"DB error: {e}", file=sys.stderr)

        ensure_db_owned()

    conn.close()
    print("Collector stopped.", file=sys.stderr)


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("ERROR: power-monitor-collector must run as root (needs RAPL sysfs access).",
              file=sys.stderr)
        sys.exit(1)
    main()