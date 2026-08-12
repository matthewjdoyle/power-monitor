#!/usr/bin/env python3
"""
power-monitor-collector — cross-platform energy monitoring daemon.

Auto-selects a measurement backend:
  Windows: Energy Meter Interface (EMI)
  Linux:   powercap RAPL (Intel + modern AMD), then amd_energy hwmon

Samples every INTERVAL seconds, computes instantaneous power (delta_energy /
delta_time), and logs to SQLite.

Configuration via environment variables:
  POWER_MONITOR_DATA_DIR         Data directory
  POWER_MONITOR_DB               SQLite database path
  POWER_MONITOR_SAMPLE_INTERVAL  Seconds between samples (default: 10)
"""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

from power_monitor.backends import energy_delta, probe_backends, select_backend
from power_monitor.config import DATA_DIR, DB_PATH, SAMPLE_INTERVAL
from power_monitor.schema import DOMAIN_TO_COLUMN, POWER_COLUMNS, init_db

if sys.platform.startswith("win"):
    import ctypes
else:
    ctypes = None  # type: ignore

INTERVAL = SAMPLE_INTERVAL

# We'll keep the DB owned by the regular user so the CLI (running as user)
# can read it (Linux only).
DB_UID = None
DB_GID = None

running = True


def handle_signal(signum, frame):
    global running
    running = False


def _is_elevated() -> bool:
    if sys.platform.startswith("win"):
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def resolve_db_owner():
    """Figure out the UID/GID of the home directory so we can chown the DB."""
    global DB_UID, DB_GID
    if sys.platform.startswith("win") or not hasattr(os, "chown"):
        return
    home_dir = os.path.expanduser("~")
    try:
        st = os.stat(home_dir)
        DB_UID = st.st_uid
        DB_GID = st.st_gid
    except OSError:
        pass


def ensure_db_owned(db_path: Path):
    """Make sure the DB file is owned by the regular user, not root (Linux)."""
    if DB_UID is None or not hasattr(os, "chown"):
        return
    if db_path.exists():
        try:
            os.chown(db_path, DB_UID, DB_GID)
        except OSError:
            pass


def cmd_probe() -> int:
    """Print discovered backends/domains and exit."""
    print(f"Platform: {sys.platform}")
    print(f"Data dir: {DATA_DIR}")
    print(f"DB path:  {DB_PATH}")
    print()
    results = probe_backends()
    if not results:
        print("No backends available on this platform.")
        return 1

    any_ok = False
    for entry in results:
        print(f"Backend: {entry['backend']}")
        print(f"  requires_elevated: {entry.get('requires_elevated')}")
        if entry.get("error"):
            print(f"  error: {entry['error']}")
        raw = entry.get("raw_channels")
        if raw is not None:
            if not raw:
                print("  raw EMI channels: (none)")
            else:
                print("  raw EMI channels:")
                for ch in raw:
                    key = ch.get("key") or "(unmapped)"
                    print(f"    [{ch['channel']}] {ch['name']!r} -> {key}")
        domains = entry.get("domains") or []
        if domains:
            any_ok = True
            print("  selected domains:")
            for d in domains:
                print(f"    {d['key']:8s}  {d['name']}")
        else:
            print("  selected domains: (none)")
        print()

    if not any_ok:
        print("ERROR: No usable energy domains found.")
        if sys.platform.startswith("win"):
            print(
                "EMI is typically available on Windows 11 bare metal. "
                "VMs and some OEM systems may not expose energy meters."
            )
        else:
            print(
                "On Linux, ensure RAPL is exposed at /sys/class/powercap/intel-rapl/ "
                "(works for Intel and modern AMD) or that the amd_energy hwmon driver is loaded."
            )
        return 1

    print("OK: at least one backend discovered usable domains.")
    return 0


def run_collector(db_path: Path, interval: int) -> int:
    global running

    backend = select_backend()
    if backend is None:
        print(
            "ERROR: No energy measurement backend available. "
            "Run with --probe for details.",
            file=sys.stderr,
        )
        return 1

    if backend.requires_elevated() and not _is_elevated():
        print(
            f"ERROR: backend '{backend.name}' requires elevated privileges "
            f"(root/Administrator).",
            file=sys.stderr,
        )
        return 1

    domains = backend.discover()
    if not domains:
        print("ERROR: Backend discovered no domains.", file=sys.stderr)
        return 1

    # Need a package domain to insert rows (schema requires package for skip logic)
    if not any(d.key == "package" for d in domains):
        print(
            "ERROR: No package/socket energy domain found. "
            f"Discovered: {[d.name for d in domains]}",
            file=sys.stderr,
        )
        return 1

    resolve_db_owner()
    print(
        f"Using backend={backend.name}; domains="
        f"{[(d.key, d.name) for d in domains]}",
        file=sys.stderr,
    )

    conn = init_db(db_path)
    ensure_db_owned(db_path)

    prev_energy: dict[str, float | None] = {}
    prev_time = time.time()
    for domain in domains:
        try:
            prev_energy[domain.name] = domain.read_joules()
        except (OSError, ValueError) as e:
            print(f"WARN: cannot read {domain.name}: {e}", file=sys.stderr)
            prev_energy[domain.name] = None

    print(f"Collector running. interval={interval}s, db={db_path}", file=sys.stderr)

    while running:
        time.sleep(interval)
        if not running:
            break

        now = time.time()
        dt = now - prev_time
        row: dict = {"timestamp": now}

        for domain in domains:
            try:
                energy_j = domain.read_joules()
            except (OSError, ValueError):
                continue

            prev = prev_energy.get(domain.name)
            if prev is not None:
                de = energy_delta(prev, energy_j, domain.max_joules)
                power_w = de / dt if dt > 0 else 0.0
            else:
                power_w = None

            prev_energy[domain.name] = energy_j
            column = DOMAIN_TO_COLUMN.get(domain.key)
            if column:
                row[column] = power_w

        prev_time = now

        if "package_w" not in row:
            continue

        for col in POWER_COLUMNS:
            row.setdefault(col, None)

        try:
            conn.execute(
                """INSERT INTO power_samples
                   (timestamp, package_w, cores_w, uncore_w, dram_w, psys_w)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    row["timestamp"],
                    row["package_w"],
                    row["cores_w"],
                    row["uncore_w"],
                    row["dram_w"],
                    row["psys_w"],
                ),
            )
            conn.commit()
        except sqlite3.Error as e:
            print(f"DB error: {e}", file=sys.stderr)

        ensure_db_owned(db_path)

    conn.close()
    print("Collector stopped.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Power monitor collector — sample CPU energy counters to SQLite"
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="List available backends/domains and exit",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=INTERVAL,
        help=f"Sample interval in seconds (default: {INTERVAL})",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help=f"SQLite database path (default: {DB_PATH})",
    )
    parser.add_argument(
        "--logfile",
        type=Path,
        default=None,
        help="Append collector stderr/stdout to this file (for headless/pythonw runs)",
    )
    args = parser.parse_args(argv)

    if args.logfile is not None:
        args.logfile.parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(args.logfile, "a", encoding="utf-8", buffering=1)
        sys.stdout = log_fp
        sys.stderr = log_fp

    if args.probe:
        sys.exit(cmd_probe())

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    sys.exit(run_collector(args.db, args.interval))


if __name__ == "__main__":
    main()
