#!/usr/bin/env python3
"""
Generate realistic synthetic energy datasets and documentation plots for power-monitor.

Creates three distinct hardware profiles:
1. nas_server:
   - 24/7 low-power home/storage server (~10W idle)
   - Nightly backup & snapshot sync (02:30-03:45)
   - Sunday ZFS scrub / SMART check
   - Evening media streaming / transcode pulses
   - Package-only primary metric (common on AMD/embedded boards)

2. ml_inference:
   - 24/7 high-TDP compute workstation / server (~50W idle, ~125W PSYS)
   - Business hour API inference bursts (peaks to 230W package / 350W PSYS)
   - Nightly sustained batch embedding / validation run (01:00-04:15)
   - Full domain breakdown with PSYS platform primary metric

3. laptop_daily:
   - Modern productivity laptop with human daily routine
   - Real suspend/sleep timestamp gaps (rendered as NaN gaps by power-monitor)
   - Morning/afternoon work sessions with compiler turbo-boost spikes
   - Lunch screen-lock idle drops
   - Sustained afternoon video call
   - PSYS platform metric (includes screen, WiFi, and motherboard power)

Outputs SQLite databases (power.db), daily summaries (daily_summary.csv),
and publication-quality plots in examples/<profile>/plots/.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

# Ensure power_monitor is importable
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from power_monitor.schema import init_db
from power_monitor.cli import daily_summary

PROFILES = ("nas_server", "ml_inference", "laptop_daily")

HOSTNAMES = {
    "nas_server": "truenas-core",
    "ml_inference": "gpu-inference-01",
    "laptop_daily": "thinkpad-x1",
}


def _ou_step(current: float, mean: float, theta: float, sigma: float, dt: float, rng: np.random.Generator) -> float:
    """Ornstein-Uhlenbeck mean-reverting process step."""
    drift = theta * (mean - current) * dt
    diffusion = sigma * np.sqrt(dt) * rng.standard_normal()
    return current + drift + diffusion


def generate_nas_samples(start_ts: float, end_ts: float, interval: float = 10.0, seed: int = 42) -> list[tuple]:
    """Generate 24/7 NAS server samples.
    
    Package-only primary metric (psys_w is None).
    """
    rng = np.random.Generator(np.random.PCG64(seed))
    ts = start_ts
    samples = []
    
    base_pkg = 9.8
    current_pkg = base_pkg
    
    while ts <= end_ts:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
        weekday = dt.weekday()  # 0=Mon, 6=Sun
        
        # 1. Ambient diurnal baseline temperature/fan drift (~0.4W)
        diurnal = 0.3 * np.sin(2 * np.pi * (hour - 6) / 24.0)
        target_idle = base_pkg + diurnal
        
        # Mean-revert the base idle
        current_pkg = _ou_step(current_pkg, target_idle, theta=0.08, sigma=0.15, dt=interval, rng=rng)
        pkg_w = max(7.5, current_pkg)
        
        # 2. Nightly backup & snapshot sync (02:30 - 03:45 every night)
        if 2.5 <= hour < 3.75:
            # Trapezoidal load profile with hash/disk jitter
            progress = (hour - 2.5) / 1.25
            shape = np.sin(progress * np.pi) ** 0.8
            boost = shape * 24.0 + rng.uniform(-2.5, 3.5)
            pkg_w += max(0.0, boost)
            
        # 3. Sunday RAID/ZFS scrub (03:30 - 07:30 on Sunday)
        if weekday == 6 and 3.5 <= hour < 7.5:
            progress = (hour - 3.5) / 4.0
            scrub_boost = 18.0 + 3.0 * np.sin(progress * 12 * np.pi) + rng.uniform(-1.5, 2.0)
            pkg_w += max(0.0, scrub_boost)
            
        # 4. Evening media streaming / transcode (20:00 - 23:15)
        # Plex transcoder chunking: 30s encode burst, then 45s idle buffer pause
        if 20.0 <= hour < 23.25 and (weekday in (4, 5, 6) or rng.uniform() > 0.3):
            cycle_s = (ts % 75)
            if cycle_s < 28:
                # Transcoding chunk
                pkg_w += 22.0 + rng.uniform(-2.0, 4.0)
            else:
                # Direct stream / buffer idle
                pkg_w += 3.5 + rng.uniform(-0.5, 1.0)
                
        # 5. Background micro-spikes (cloud sync, torrent checks, docker metrics)
        # Occurs randomly with 0.8% probability per 10s step (~3 times per hour)
        if rng.uniform() < 0.008:
            pkg_w += rng.uniform(4.0, 11.0)
            
        # Physical domain breakdown
        pkg_w = float(round(pkg_w, 3))
        cores_w = float(round(max(1.8, 0.25 * pkg_w + 0.52 * max(0.0, pkg_w - base_pkg) + rng.normal(0, 0.2)), 3))
        uncore_w = float(round(max(4.0, pkg_w - cores_w - 0.3), 3))
        dram_w = float(round(max(1.5, 1.8 + 0.04 * pkg_w + 0.04 * cores_w + rng.normal(0, 0.08)), 3))
        psys_w = None  # AMD / desktop board without platform power telemetry
        
        samples.append((ts, pkg_w, cores_w, uncore_w, dram_w, psys_w))
        ts += interval

    return samples


def generate_ml_samples(start_ts: float, end_ts: float, interval: float = 10.0, seed: int = 101) -> list[tuple]:
    """Generate 24/7 high-TDP ML inference server samples.
    
    Includes both psys_w and package_w telemetry.
    """
    rng = np.random.Generator(np.random.PCG64(seed))
    ts = start_ts
    samples = []
    
    base_pkg = 48.0
    current_pkg = base_pkg
    
    # Track simulated active batch query burst
    query_burst_remaining = 0.0
    query_burst_power = 0.0
    
    while ts <= end_ts:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
        weekday = dt.weekday()
        is_weekend = (weekday >= 5)
        
        # Base server idle walk
        current_pkg = _ou_step(current_pkg, base_pkg, theta=0.06, sigma=0.4, dt=interval, rng=rng)
        pkg_w = max(42.0, current_pkg)
        
        # 1. Scheduled Nightly Batch Processing / Re-indexing (01:00 - 04:15 every day)
        if 1.0 <= hour < 4.25:
            elapsed_batch_m = (hour - 1.0) * 60.0
            # Thermal limit throttling behavior: starts at 225W, heatsinks saturate over 25m, settling at 208W
            thermal_droop = 17.0 * (1.0 - np.exp(-elapsed_batch_m / 20.0))
            batch_power = 225.0 - thermal_droop + rng.normal(0, 4.5)
            pkg_w = max(pkg_w, batch_power)
            
        # 2. Business Hours API Inference Traffic (09:00 - 18:30)
        elif 9.0 <= hour < 18.5:
            # Query arrival probability depends on hour (bell curve peaking at 14:00)
            traffic_density = np.exp(-0.5 * ((hour - 14.0) / 3.0) ** 2)
            if is_weekend:
                traffic_density *= 0.25
                
            if query_burst_remaining <= 0:
                # Chance of new query burst starting
                p_arrival = 0.15 * traffic_density
                if rng.uniform() < p_arrival:
                    query_burst_remaining = rng.uniform(15.0, 75.0)  # burst duration in seconds
                    query_burst_power = rng.uniform(90.0, 165.0)     # added package watts
            
            if query_burst_remaining > 0:
                pkg_w += query_burst_power + rng.normal(0, 6.0)
                query_burst_remaining -= interval
            else:
                # Ambient background traffic
                if rng.uniform() < 0.04 * traffic_density:
                    pkg_w += rng.uniform(25.0, 60.0)
                    
        # 3. Off-peak evening traffic (18:30 - 01:00)
        else:
            if rng.uniform() < 0.015:
                pkg_w += rng.uniform(20.0, 55.0)
                
        # Physical domain breakdown
        pkg_w = float(round(pkg_w, 3))
        cores_w = float(round(max(12.0, 14.0 + 0.72 * max(0.0, pkg_w - base_pkg) + rng.normal(0, 1.2)), 3))
        uncore_w = float(round(max(18.0, pkg_w - cores_w + rng.normal(0, 0.4)), 3))
        dram_w = float(round(max(14.0, 15.5 + 0.11 * max(0.0, pkg_w - base_pkg) + rng.normal(0, 0.6)), 3))
        # PSYS represents platform total: motherboard, VRM losses (scaling non-linearly), fans, PCIe
        vrm_loss = 0.12 * pkg_w + 0.0004 * (pkg_w ** 2)
        psys_w = float(round(pkg_w + dram_w + 52.0 + vrm_loss + rng.normal(0, 1.5), 3))
        
        samples.append((ts, pkg_w, cores_w, uncore_w, dram_w, psys_w))
        ts += interval

    return samples


def generate_laptop_samples(start_ts: float, end_ts: float, interval: float = 10.0, seed: int = 7) -> list[tuple]:
    """Generate daily-use productivity laptop samples with realistic sleep/suspend gaps."""
    rng = np.random.Generator(np.random.PCG64(seed))
    ts = start_ts
    samples = []
    
    base_idle_pkg = 3.8
    current_pkg = base_idle_pkg
    
    # State tracking
    compiling_remaining = 0.0
    compiling_power = 0.0
    
    while ts <= end_ts:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
        weekday = dt.weekday()
        is_weekend = (weekday >= 5)
        
        # ── Schedule & Suspend Gaps ──────────────────────────────────────
        # During suspend, the collector daemon is stopped; NO samples are recorded.
        # This creates real time gaps (>60s) for power-monitor to display properly.
        is_sleeping = False
        
        if not is_weekend:
            # Weekday sleep: 23:30 to 08:15 next morning
            if hour >= 23.5 or hour < 8.25:
                is_sleeping = True
            # Dinner / commute gap: 18:00 to 19:30
            elif 18.0 <= hour < 19.5:
                is_sleeping = True
        else:
            # Weekend sleep: 01:15 to 09:45
            if hour >= 1.25 and hour < 9.75:
                is_sleeping = True
            # Weekend afternoon away gap: 14:00 to 17:00
            elif 14.0 <= hour < 17.0:
                is_sleeping = True

        if is_sleeping:
            # Advance time without adding a sample (genuine collector downtime gap)
            ts += interval
            current_pkg = base_idle_pkg
            continue
            
        # ── Active Machine Workloads ─────────────────────────────────────
        # 1. Screen-lock / Lunch idle (12:30 - 13:30 on weekdays)
        if not is_weekend and 12.5 <= hour < 13.5:
            current_pkg = _ou_step(current_pkg, base_idle_pkg, theta=0.1, sigma=0.1, dt=interval, rng=rng)
            pkg_w = max(2.8, current_pkg)
            screen_w = 0.5  # screen dimmed / off
            
        # 2. Afternoon Video Conference Call (14:00 - 15:15 on weekdays)
        # Constant camera capture + hardware video decode + screen backlight
        elif not is_weekend and 14.0 <= hour < 15.25:
            call_base = 23.5 + 2.0 * np.sin(hour * 4)
            current_pkg = _ou_step(current_pkg, call_base, theta=0.1, sigma=0.5, dt=interval, rng=rng)
            pkg_w = max(18.0, current_pkg)
            screen_w = 4.2
            
        # 3. Morning / Afternoon Dev Work Session (08:15 - 12:30, 15:15 - 18:00)
        elif not is_weekend and ((8.25 <= hour < 12.5) or (15.25 <= hour < 18.0)):
            screen_w = 4.0
            # Base productivity desk activity (VSCode, browser, terminal)
            dev_idle = 12.5 + rng.normal(0, 0.4)
            current_pkg = _ou_step(current_pkg, dev_idle, theta=0.08, sigma=0.5, dt=interval, rng=rng)
            pkg_w = max(8.0, current_pkg)
            
            # Check for compiler runs (e.g. rust/cargo build, docker build)
            if compiling_remaining <= 0:
                # ~4 times per morning/afternoon
                if rng.uniform() < 0.009:
                    compiling_remaining = rng.uniform(30.0, 110.0)
                    compiling_power = rng.uniform(42.0, 58.0)
                    
            if compiling_remaining > 0:
                # Intel/AMD PL2 Turbo boost peak decaying to PL1 sustained power
                boost_decay = np.exp(-max(0.0, 80.0 - compiling_remaining) / 25.0)
                active_comp_w = 32.0 + (compiling_power - 32.0) * boost_decay + rng.normal(0, 1.8)
                pkg_w = max(pkg_w, active_comp_w)
                compiling_remaining -= interval
            else:
                # Occasional quick page loads / git status / test runs
                if rng.uniform() < 0.03:
                    pkg_w += rng.uniform(5.0, 18.0)
                    
        # 4. Evening casual use (19:30 - 23:30) or Weekend casual use
        else:
            screen_w = 3.5
            casual_base = 9.5
            current_pkg = _ou_step(current_pkg, casual_base, theta=0.08, sigma=0.3, dt=interval, rng=rng)
            pkg_w = max(5.0, current_pkg)
            # Occasional media streaming / web video
            if rng.uniform() < 0.02:
                pkg_w += rng.uniform(4.0, 10.0)

        # Physical domain allocation
        pkg_w = float(round(pkg_w, 3))
        cores_w = float(round(max(0.8, 1.2 + 0.64 * max(0.0, pkg_w - base_idle_pkg) + rng.normal(0, 0.2)), 3))
        uncore_w = float(round(max(1.5, pkg_w - cores_w - 0.2), 3))
        dram_w = float(round(max(0.7, 0.9 + 0.05 * pkg_w + rng.normal(0, 0.05)), 3))
        # Laptop platform total: includes screen backlight, WiFi radio, trackpad/keyboard, audio codec
        psys_w = float(round(pkg_w + dram_w + screen_w + 3.0 + 0.06 * pkg_w + rng.normal(0, 0.25), 3))
        
        samples.append((ts, pkg_w, cores_w, uncore_w, dram_w, psys_w))
        ts += interval

    return samples


def populate_database(db_path: Path, samples: list[tuple]):
    """Insert generated power samples in a single high-speed transaction."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
        
    conn = init_db(db_path)
    print(f"  Inserting {len(samples):,} rows into {db_path} ...")
    conn.executemany(
        """
        INSERT INTO power_samples (timestamp, package_w, cores_w, uncore_w, dram_w, psys_w)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        samples,
    )
    conn.commit()
    conn.close()


def generate_daily_csv(db_path: Path, csv_path: Path, days: int = 30):
    """Generate daily_summary.csv by computing official rollups for all 30 days."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    
    import power_monitor.cli as pm_cli
    old_db = pm_cli.DB_PATH
    pm_cli.DB_PATH = db_path
    
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    rows_written = 0
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "date", "avg_power_w", "peak_power_w", "idle_power_w",
            "energy_kwh", "energy_kwh_psys", "duration_h", "samples", "est_cost_gbp",
        ])
        
        for d in range(days, 0, -1):
            day_start = today_start - timedelta(days=d)
            day_end = day_start + timedelta(days=1)
            date_str = day_start.strftime("%Y-%m-%d")
            
            s = pm_cli.daily_summary(day_start.timestamp(), day_end.timestamp())
            if s is None:
                continue
                
            writer.writerow([
                date_str,
                f"{s['avg_w']:.3f}" if s['avg_w'] else "",
                f"{s['max_w']:.3f}" if s['max_w'] else "",
                f"{s['min_w']:.3f}" if s['min_w'] else "",
                f"{s['energy_kwh']:.6f}",
                f"{s.get('energy_kwh_psys', 0):.6f}",
                f"{s['duration_h']:.2f}",
                s["samples"],
                f"{s['cost_gbp']:.4f}",
            ])
            rows_written += 1

    pm_cli.DB_PATH = old_db
    print(f"  Wrote {rows_written} daily records to {csv_path}")


def render_profile_plots(profile_dir: Path, hostname: str):
    """Invoke power_monitor.cli graphing commands via subprocess with custom env."""
    plots_dir = profile_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)
    
    env = os.environ.copy()
    env["POWER_MONITOR_DATA_DIR"] = str(profile_dir)
    env["POWER_MONITOR_DB"] = str(profile_dir / "power.db")
    env["POWER_MONITOR_CSV"] = str(profile_dir / "daily_summary.csv")
    env["POWER_MONITOR_HOSTNAME"] = hostname
    env["POWER_MONITOR_COST_PER_KWH"] = "0.34"
    
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    range_start = (today_start - timedelta(days=6)).strftime("%Y-%m-%d")
    range_end = (today_start - timedelta(days=2)).strftime("%Y-%m-%d")

    graph_views = [
        ("today", ["graph", "today"]),
        ("yesterday", ["graph", "yesterday"]),
        ("week", ["graph", "week"]),
        ("month", ["graph", "month"]),
        ("heatmap", ["graph", "heatmap"]),
        ("all", ["graph", "all"]),
        ("range", ["graph", "range", range_start, range_end]),
    ]
    
    print(f"  Rendering plots for {profile_dir.name} ({hostname}) ...")
    for name, args in graph_views:
        cmd = [sys.executable, "-m", "power_monitor"] + args
        res = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            print(f"    WARNING: Failed to render {name}: {res.stderr.strip()}", file=sys.stderr)
        else:
            # Find the saved line
            for line in res.stdout.splitlines():
                if line.startswith("Saved:"):
                    print(f"    ✓ {line.strip()}")


def main():
    parser = argparse.ArgumentParser(description="Generate synthetic power-monitor datasets and plots")
    parser.add_argument(
        "--profile",
        choices=["all", "nas_server", "ml_inference", "laptop_daily"],
        default="all",
        help="Profile to generate (default: all)",
    )
    parser.add_argument("--days", type=int, default=30, help="Number of days to generate (default: 30)")
    parser.add_argument("--render-plots", action="store_true", default=True, help="Render example PNG plots")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    args = parser.parse_args()

    targets = PROFILES if args.profile == "all" else [args.profile]
    
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_time = today_start - timedelta(days=args.days)
    end_time = now
    
    start_ts = start_time.timestamp()
    end_ts = end_time.timestamp()

    print(f"Generating synthetic datasets:")
    print(f"  Range: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')} → {end_time.strftime('%Y-%m-%d %H:%M:%S UTC')} ({args.days} days)")
    print(f"  Profiles: {', '.join(targets)}")
    print()

    examples_dir = REPO_ROOT / "examples"

    for p in targets:
        profile_dir = examples_dir / p
        db_path = profile_dir / "power.db"
        csv_path = profile_dir / "daily_summary.csv"
        hostname = HOSTNAMES[p]
        
        print(f"[{p}] Generating {p} profile (hostname: {hostname}) ...")
        if p == "nas_server":
            samples = generate_nas_samples(start_ts, end_ts, interval=10.0, seed=args.seed)
        elif p == "ml_inference":
            samples = generate_ml_samples(start_ts, end_ts, interval=10.0, seed=args.seed + 1)
        elif p == "laptop_daily":
            samples = generate_laptop_samples(start_ts, end_ts, interval=10.0, seed=args.seed + 2)
        else:
            continue
            
        populate_database(db_path, samples)
        generate_daily_csv(db_path, csv_path, days=args.days)
        
        if args.render_plots:
            render_profile_plots(profile_dir, hostname)
            
        print(f"[{p}] Completed successfully.\n")

    print("All example datasets and plots generated successfully!")


if __name__ == "__main__":
    main()
