#!/usr/bin/env python3
"""
power-monitor — CLI for querying the energy monitoring database.

Usage:
  power-monitor status          Show current power draw
  power-monitor today           Show today's energy summary
  power-monitor range A [B]     Summary for date range (YYYY-MM-DD [YYYY-MM-DD])
  power-monitor watch [N]       Live power display (default: 20 updates)
  power-monitor export          Dump last 7 days as CSV
  power-monitor daily-log       Append yesterday's summary to daily CSV
  power-monitor graph [view]    Generate power graph (today|yesterday|week|range A B)
  power-monitor probe           List available energy backends/domains
"""
from power_monitor import __version__

import argparse
import csv
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

# ── Agg backend before importing pyplot (headless server) ─────────────────
import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import numpy as np

from power_monitor.config import (
    COST_PER_KWH,
    CSV_PATH,
    DB_PATH,
    HOSTNAME,
    PLOTS_DIR,
    SAMPLE_INTERVAL,
)
from power_monitor.schema import primary_label, primary_power, primary_sql_expr
from power_monitor.collector import cmd_probe as run_probe

DAILY_CSV = CSV_PATH

# Seaborn deep muted palette — clean modern look (blues, greens, warm accents)
SEABORN_DEEP = [
    "#4C72B0",  # muted blue (primary: PSYS)
    "#DD8452",  # warm orange (secondary: Package, overlays)
    "#55A868",  # muted green
    "#C44E52",  # muted red
    "#8172B3",  # muted purple
    "#937860",  # muted brown
    "#DA8BC3",  # muted pink (accents: today/partial)
    "#8C8C8C",  # grey (energy fill)
    "#CCB974",  # muted gold
    "#64B5CD",  # muted cyan
]
# Heatmap colormap — deep blues to warm gold (seaborn-style)
HEATMAP_CMAP = "YlOrBr"  # yellow-orange-brown, perceptually uniform, readable


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def get_conn():
    """Open a SQLite connection to the power-monitor database."""
    if not DB_PATH.exists():
        print("ERROR: No data yet. Is power-monitor-collector running?", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def fmt_w(w):
    """Format a wattage value for display."""
    if w is None:
        return "  N/A "
    return f"{w:6.2f}W"


def fmt_kwh(j):
    """Format an energy value (joules) for display as Wh or kWh."""
    if j is None:
        return "  N/A"
    kwh = j / 3_600_000
    if kwh < 0.01:
        return f"{kwh * 1000:.2f} Wh"
    return f"{kwh:.3f} kWh"


def parse_date_range(start_str, end_str=None):
    """Parse YYYY-MM-DD [YYYY-MM-DD] → (ts_start, ts_end, label)."""
    try:
        start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_str:
            end = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        else:
            end = start + timedelta(days=1)
    except ValueError:
        print("ERROR: dates must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    label = start_str if not end_str else f"{start_str} → {end_str}"
    return start.timestamp(), end.timestamp(), label


def _coalesce_primary(package_w: np.ndarray, psys_w: np.ndarray) -> tuple[np.ndarray, bool]:
    """Return (primary_w, has_psys) where primary is PSYS when available else package."""
    has_psys = not np.all(np.isnan(psys_w))
    if has_psys:
        primary = np.where(np.isnan(psys_w), package_w, psys_w)
        return primary, True
    return package_w.copy(), False


def daily_summary(ts_start, ts_end):
    """Return a dict with aggregated stats for the time window.

    Energy is computed via trapezoidal integration over actual sample rows,
    separately for package_w and psys_w.  The primary metric (used for
    energy_kwh and cost_gbp) is PSYS when PSYS data is available, otherwise
    it falls back to package energy.
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT timestamp, package_w, psys_w
           FROM power_samples
           WHERE timestamp >= ? AND timestamp < ?
           ORDER BY timestamp""",
        (ts_start, ts_end),
    ).fetchall()
    conn.close()

    if not rows:
        return None

    timestamps = np.array([r["timestamp"] for r in rows], dtype=float)
    package_w = np.array(
        [r["package_w"] if r["package_w"] is not None else np.nan for r in rows],
        dtype=float,
    )
    psys_w = np.array(
        [r["psys_w"] if r["psys_w"] is not None else np.nan for r in rows],
        dtype=float,
    )
    primary_w, has_psys = _coalesce_primary(package_w, psys_w)

    dts = np.diff(timestamps)  # seconds between consecutive samples

    def _trapz(power):
        """Trapezoidal integration, skipping pairs where either value is NaN."""
        total_j = 0.0
        for i in range(len(dts)):
            p0, p1 = power[i], power[i + 1]
            if np.isnan(p0) or np.isnan(p1):
                continue
            total_j += (p0 + p1) / 2.0 * dts[i]
        return total_j

    energy_j_pkg = _trapz(package_w)
    energy_kwh_pkg = energy_j_pkg / 3_600_000
    energy_j_psys = _trapz(psys_w)
    energy_kwh_psys = energy_j_psys / 3_600_000
    energy_j_primary = _trapz(primary_w)
    energy_kwh = energy_j_primary / 3_600_000
    cost = energy_kwh * COST_PER_KWH

    # Summary stats on the primary metric (PSYS or package)
    valid = primary_w[~np.isnan(primary_w)]
    avg_w = float(np.nanmean(primary_w)) if len(valid) > 0 else 0.0
    max_w = float(np.nanmax(primary_w)) if len(valid) > 0 else 0.0
    min_w = float(np.nanmin(primary_w)) if len(valid) > 0 else 0.0

    duration_s = timestamps[-1] - timestamps[0]
    duration_h = duration_s / 3600

    return {
        "samples": len(rows),
        "first_ts": float(timestamps[0]),
        "last_ts": float(timestamps[-1]),
        "duration_h": duration_h,
        "avg_w": avg_w,
        "max_w": max_w,
        "min_w": min_w,
        "energy_kwh": energy_kwh,
        "energy_kwh_pkg": energy_kwh_pkg,
        "energy_kwh_psys": energy_kwh_psys,
        "cost_gbp": cost,
        "primary_label": primary_label(has_psys),
        "has_psys": has_psys,
    }


def print_summary(s, label):
    """Pretty-print a daily_summary dict."""
    metric = s.get("primary_label", "package")
    print(f"Power summary: {label}")
    print(f"  Metric:      {metric}")
    print(f"  Samples:     {s['samples']}")
    print(f"  Duration:    {s['duration_h']:.1f} hours")
    print(f"  Average:     {s['avg_w']:6.2f}W")
    print(f"  Peak:        {s['max_w']:6.2f}W")
    print(f"  Idle:        {s['min_w']:6.2f}W")
    print(f"  Energy:      {fmt_kwh(s['energy_kwh'] * 3_600_000)}")
    print(f"  Est. cost:   £{s['cost_gbp']:.3f}")


def _apply_style():
    """Apply default plot styling."""
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["DejaVu Sans"],
        "mathtext.fontset": "dejavusans",
        "axes.spines.top": True,
        "axes.spines.right": True,
        "xtick.direction": "in",
        "xtick.top": True,
        "ytick.direction": "in",
        "ytick.right": True,
        "axes.linewidth": 1.2,
        "axes.grid": False,
        "legend.frameon": False,
        "legend.fontsize": 9,
        "legend.handlelength": 1.2,
        "lines.linewidth": 1.5,
        "lines.markersize": 6,
        "axes.labelsize": 11,
        "axes.titlesize": 12,
        "xtick.labelsize": 9.5,
        "ytick.labelsize": 9.5,
        "xtick.major.size": 4,
        "xtick.major.width": 0.7,
        "ytick.major.size": 4,
        "ytick.major.width": 0.7,
        "figure.dpi": 150,
        "savefig.dpi": 200,
        "savefig.bbox": "tight",
        "axes.prop_cycle": plt.cycler(color=SEABORN_DEEP),
    })


# ═══════════════════════════════════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════════════════════════════════

def cmd_status():
    """Show the most recent power sample."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM power_samples ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if not row:
        print("No data yet.")
        return

    ts = datetime.fromtimestamp(row["timestamp"], tz=timezone.utc).astimezone()
    print(f"Power @ {ts.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Package:  {fmt_w(row['package_w'])}")
    print(f"  Cores:    {fmt_w(row['cores_w'])}")
    print(f"  Uncore:   {fmt_w(row['uncore_w'])}")
    print(f"  DRAM:     {fmt_w(row['dram_w'])}")
    if row["psys_w"] is not None:
        print(f"  PSYS:     {fmt_w(row['psys_w'])}")
    total = primary_power(row)
    total_label = primary_label(row["psys_w"] is not None)
    print(f"  ─────────────────")
    if total is not None:
        print(f"  Total:    {total:6.2f}W  ({total_label})")
    else:
        print(f"  Total:       N/A  ({total_label})")


def cmd_today():
    """Show today's energy summary."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    s = daily_summary(today_start.timestamp(), tomorrow_start.timestamp())
    if s is None:
        print("No data for today.")
        return
    print_summary(s, "Today")


def cmd_range(start_str, end_str=None):
    """Show energy summary for a date range."""
    ts_start, ts_end, label = parse_date_range(start_str, end_str)
    s = daily_summary(ts_start, ts_end)
    if s is None:
        print(f"No data for {label}")
        return
    print_summary(s, label)


def cmd_watch(n=20):
    """Display live power readings for N updates."""
    conn = get_conn()
    try:
        for i in range(n):
            row = conn.execute(
                "SELECT * FROM power_samples ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                ts = datetime.fromtimestamp(row["timestamp"], tz=timezone.utc).astimezone()
                pkg = row["package_w"] or 0
                cores = row["cores_w"] or 0
                dram = row["dram_w"] or 0
                bar = "\u2588" * int(pkg / 2) if pkg < 40 else "\u2588" * 20
                print(f"\r{ts.strftime('%H:%M:%S')}  Package:{pkg:5.1f}W  Cores:{cores:5.1f}W  DRAM:{dram:4.1f}W  {bar}",
                      end="", flush=True)
            if i < n - 1:
                time.sleep(2)
        print()
    except KeyboardInterrupt:
        print()
    finally:
        conn.close()


def cmd_export():
    """Export the last 7 days of raw samples as CSV to stdout."""
    conn = get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
    rows = conn.execute(
        """SELECT timestamp, package_w, cores_w, uncore_w, dram_w, psys_w
           FROM power_samples WHERE timestamp >= ? ORDER BY timestamp""",
        (cutoff,),
    ).fetchall()
    conn.close()

    print("timestamp,package_w,cores_w,uncore_w,dram_w,psys_w")
    for r in rows:
        iso = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc).isoformat()
        vals = [iso] + [
            f"{r[c]:.3f}" if r[c] is not None else ""
            for c in ("package_w", "cores_w", "uncore_w", "dram_w", "psys_w")
        ]
        print(",".join(vals))


def cmd_daily_log():
    """Append yesterday's summary to the daily CSV."""
    yesterday = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    tomorrow = yesterday + timedelta(days=1)
    s = daily_summary(yesterday.timestamp(), tomorrow.timestamp())

    if s is None:
        print(f"No data for {yesterday.strftime('%Y-%m-%d')} — skipping CSV append.", file=sys.stderr)
        sys.exit(0)

    date_str = yesterday.strftime("%Y-%m-%d")

    # Create CSV with header if it doesn't exist
    write_header = not DAILY_CSV.exists()
    DAILY_CSV.parent.mkdir(parents=True, exist_ok=True)

    with open(DAILY_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "date", "avg_power_w", "peak_power_w", "idle_power_w",
                "energy_kwh", "energy_kwh_psys", "duration_h", "samples", "est_cost_gbp",
            ])
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

    print(f"Appended {date_str} to {DAILY_CSV}")
    print_summary(s, date_str)


# ═══════════════════════════════════════════════════════════════════════════
# Graph commands
# ═══════════════════════════════════════════════════════════════════════════

def _fetch_timeseries(ts_start=None, ts_end=None):
    """Fetch power data for a time range (or all data if ts_start/ts_end are None).

    Returns (timestamps, package_w, psys_w) with NaN gaps inserted where the
    collector was down (>60s between consecutive samples).
    """
    conn = get_conn()
    if ts_start is not None and ts_end is not None:
        rows = conn.execute(
            """SELECT timestamp, package_w, psys_w
               FROM power_samples
               WHERE timestamp >= ? AND timestamp < ?
               ORDER BY timestamp""",
            (ts_start, ts_end),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT timestamp, package_w, psys_w
               FROM power_samples ORDER BY timestamp"""
        ).fetchall()
    conn.close()

    if not rows:
        return None, None, None

    ts = np.array([r["timestamp"] for r in rows])
    pkg = np.array([r["package_w"] if r["package_w"] is not None else np.nan for r in rows])
    psys = np.array([r["psys_w"] if r["psys_w"] is not None else np.nan for r in rows])

    # Insert NaN gaps where collector was down (>60s between samples)
    GAP_THRESHOLD = 60  # seconds
    gap_mask = np.diff(ts) > GAP_THRESHOLD
    if gap_mask.any():
        gap_indices = np.where(gap_mask)[0]
        for arr in (pkg, psys):
            arr[gap_indices] = np.nan

    return ts, pkg, psys


def _downsample(ts, *arrays, period_s=60):
    """Downsample to one point per `period_s` using binned means (vectorised)."""
    if ts is None or len(ts) == 0:
        return (None,) * (1 + len(arrays))

    start = ts[0] - (ts[0] % period_s)
    end = ts[-1] + period_s
    bin_edges = np.arange(start, end, period_s)
    if len(bin_edges) < 2:
        return (ts,) + arrays

    bin_centers = bin_edges[:-1] + period_s / 2
    bin_idx = np.digitize(ts, bin_edges) - 1
    bin_idx = np.clip(bin_idx, 0, len(bin_centers) - 1)

    result = [bin_centers]
    for arr in arrays:
        if arr is None:
            result.append(None)
            continue
        binned = np.full(len(bin_centers), np.nan)
        valid = np.isfinite(arr)
        if valid.any():
            binned_sum = np.zeros(len(bin_centers))
            binned_count = np.zeros(len(bin_centers))
            np.add.at(binned_sum, bin_idx[valid], arr[valid])
            np.add.at(binned_count, bin_idx[valid], 1)
            mask = binned_count > 0
            binned[mask] = binned_sum[mask] / binned_count[mask]
        result.append(binned)

    return tuple(result)


def _rolling_mean(arr, window):
    """Rolling mean that respects NaN (NaN values excluded from average)."""
    if arr is None or len(arr) < window:
        return arr
    mask = ~np.isnan(arr)
    filled = np.where(mask, arr, 0.0)
    cumsum = np.cumsum(filled)
    cumcount = np.cumsum(mask.astype(float))

    roll_sum = cumsum[window - 1:] - np.concatenate(([0.0], cumsum[:-window]))
    roll_count = cumcount[window - 1:] - np.concatenate(([0.0], cumcount[:-window]))

    result = np.full(len(arr), np.nan)
    centre = window // 2
    n_available = len(roll_sum)
    result[centre:centre + n_available] = np.where(
        roll_count > 0, roll_sum / np.maximum(roll_count, 1), np.nan
    )
    return result


def cmd_graph(view="today", start_str=None, end_str=None):
    """Generate a power graph and save to PLOTS_DIR."""
    now = datetime.now(timezone.utc)

    if view == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
        title = "Power — Today"
        filename = f"power_today_{now.strftime('%Y%m%d')}.png"
        ts_start, ts_end = start.timestamp(), end.timestamp()
        ts, pkg, psys = _fetch_timeseries(ts_start, ts_end)
    elif view == "yesterday":
        yesterday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        start = yesterday
        end = yesterday + timedelta(days=1)
        title = f"Power — {yesterday.strftime('%Y-%m-%d')}"
        filename = f"power_{yesterday.strftime('%Y%m%d')}.png"
        ts_start, ts_end = start.timestamp(), end.timestamp()
        ts, pkg, psys = _fetch_timeseries(ts_start, ts_end)
    elif view == "week":
        return cmd_graph_week()
    elif view == "month":
        return cmd_graph_month()
    elif view == "heatmap":
        return cmd_graph_heatmap(days=7)
    elif view == "range":
        if not start_str:
            print("ERROR: 'graph range' requires start and end dates (YYYY-MM-DD)", file=sys.stderr)
            sys.exit(1)
        ts_start, ts_end, label = parse_date_range(start_str, end_str)
        start = datetime.fromtimestamp(ts_start, tz=timezone.utc)
        end = datetime.fromtimestamp(ts_end, tz=timezone.utc)
        title = f"Power — {label}"
        safe = label.replace(" → ", "_to_")
        filename = f"power_{safe}.png"
        ts, pkg, psys = _fetch_timeseries(ts_start, ts_end)
    elif view == "all":
        ts, pkg, psys = _fetch_timeseries()  # no WHERE clause
        if ts is None or len(ts) == 0:
            print("No data in database.", file=sys.stderr)
            sys.exit(1)
        start_ts = datetime.fromtimestamp(ts[0], tz=timezone.utc)
        end_ts = datetime.fromtimestamp(ts[-1], tz=timezone.utc)
        title = f"Power — All Data ({start_ts.strftime('%Y-%m-%d')} → {end_ts.strftime('%Y-%m-%d')})"
        filename = f"power_all_{now.strftime('%Y%m%d_%H%M%S')}.png"
    else:
        print(f"ERROR: unknown view '{view}'. Use: today|yesterday|week|month|range|all|heatmap", file=sys.stderr)
        sys.exit(1)

    if ts is None or len(ts) == 0:
        print(f"No data for {title}", file=sys.stderr)
        sys.exit(1)

    # ── Adaptive downsampling ───────────────────────────────────────────
    # Pick bin size so we get ~1500 points for clean rendering
    total_span_s = ts[-1] - ts[0]
    TARGET_POINTS = 1500
    bin_s = max(60, int(total_span_s / TARGET_POINTS))  # min 60s, auto-scale up
    # Round to nice numbers: 60, 120, 300 (5min), 600 (10min), 900 (15min), 1800 (30min), 3600 (1h)
    nice_bins = [60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 86400]
    for nb in nice_bins:
        if nb >= bin_s:
            bin_s = nb
            break
    else:
        bin_s = 86400  # 1 day max

    ts_ds, pkg_ds, psys_ds = _downsample(ts, pkg, psys, period_s=bin_s)
    primary_ds, has_psys = _coalesce_primary(pkg_ds, psys_ds)
    primary_name = "PSYS" if has_psys else "Package"

    _apply_style()

    # ── Figure ─────────────────────────────────────────────────────────
    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(14, 6.5),
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08},
        sharex=True,
    )

    dt = [datetime.fromtimestamp(t, tz=timezone.utc) for t in ts_ds]

    # Top panel: power lines — primary bold, package secondary when PSYS exists
    ax_top.plot(dt, primary_ds, color=SEABORN_DEEP[0], lw=0.4, alpha=0.25, label="_nolegend_")
    if has_psys:
        ax_top.plot(dt, pkg_ds, color=SEABORN_DEEP[1], lw=0.5, alpha=0.4, label="Package")

    window = max(5, int(900 / bin_s))
    if len(primary_ds) > window:
        roll = _rolling_mean(primary_ds, window)
        roll_label_mins = bin_s * window // 60
        ax_top.plot(
            dt, roll, color=SEABORN_DEEP[0], lw=1.8,
            label=f"{primary_name} rolling avg ({roll_label_mins}min)",
        )
    else:
        ax_top.plot(dt, primary_ds, color=SEABORN_DEEP[0], lw=1.2, label=primary_name)

    ax_top.set_ylabel("Power (W)")
    ax_top.set_title(title, loc="left", fontweight="bold")
    ax_top.legend(loc="upper right", ncol=2)
    ax_top.set_ylim(bottom=0)

    # Clip y-axis to p99 to prevent single spikes from squashing everything
    primary_finite = primary_ds[np.isfinite(primary_ds)] if primary_ds is not None else np.array([])
    pkg_finite = pkg_ds[np.isfinite(pkg_ds)] if pkg_ds is not None else np.array([])
    all_vals = np.concatenate([primary_finite, pkg_finite]) if has_psys else primary_finite
    all_vals = all_vals[all_vals > 0]  # exclude zeros
    if len(all_vals) > 10:
        clip_max = np.percentile(all_vals, 99)
        actual_max = np.max(all_vals)
        if clip_max < actual_max * 0.9:
            ax_top.set_ylim(top=clip_max * 1.1)
        else:
            ax_top.set_ylim(top=actual_max * 1.1)

    # Bottom panel: cumulative energy from primary metric
    energy_source = primary_ds
    energy_wh = np.array([0.0])
    if len(ts_ds) > 1:
        dt_s = np.diff(ts_ds)
        energy_j = np.cumsum(np.nan_to_num(energy_source[:-1], nan=0) * dt_s)
        energy_wh = energy_j / 3600
        energy_wh = np.insert(energy_wh, 0, 0)
        ax_bot.fill_between(dt, energy_wh, color=SEABORN_DEEP[7], alpha=0.15)
        ax_bot.plot(dt, energy_wh, color=SEABORN_DEEP[7], lw=1.2)
        final = energy_wh[-1]
        final_kwh = final / 1000
        cost = final_kwh * COST_PER_KWH
        if final < 1:
            elabel = f"{final * 1000:.1f} Wh"
        else:
            elabel = f"{final_kwh:.2f} kWh"
        ax_bot.annotate(
            f"{elabel} ({primary_name})\nEst. cost: £{cost:.2f}",
            xy=(dt[-1], final), xytext=(10, 5),
            textcoords="offset points", fontsize=9, ha="left", va="bottom",
            color=SEABORN_DEEP[0],
        )

    ax_bot.set_ylabel("Energy (Wh)")
    ax_bot.set_xlabel("Time")
    ax_bot.yaxis.set_major_locator(mticker.MaxNLocator(nbins=6))

    # Time axis formatting — adapt to time range
    span_h = (ts_ds[-1] - ts_ds[0]) / 3600
    if span_h <= 6:
        ax_bot.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax_bot.xaxis.set_major_locator(mdates.HourLocator(interval=1))
        ax_bot.xaxis.set_minor_locator(mdates.MinuteLocator(interval=30))
    elif span_h <= 24:
        ax_bot.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax_bot.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        ax_bot.xaxis.set_minor_locator(mdates.HourLocator(interval=1))
    elif span_h <= 48:
        ax_bot.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax_bot.xaxis.set_major_locator(mdates.HourLocator(interval=4))
    elif span_h <= 168:
        ax_bot.xaxis.set_major_formatter(mdates.DateFormatter("%a %d"))
        ax_bot.xaxis.set_major_locator(mdates.DayLocator())
        ax_bot.xaxis.set_minor_locator(mdates.HourLocator(interval=6))
    elif span_h <= 720:
        ax_bot.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax_bot.xaxis.set_major_locator(mdates.DayLocator(interval=2))
    else:
        ax_bot.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax_bot.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
    fig.autofmt_xdate(rotation=30, ha="right")

    # Save
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PLOTS_DIR / filename
    fig.savefig(out_path, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    # Summary
    print(f"Saved: {out_path}")
    print(f"  {title}")
    if len(ts_ds) > 1:
        total_wh = energy_wh[-1] if len(energy_wh) > 0 else 0
        print(f"  Data points: {len(ts)} raw → {len(ts_ds)} binned ({bin_s}s bins)")
        print(f"  Total energy: {total_wh:.2f} Wh  ({total_wh / 1000:.3f} kWh)")
        print(f"  Est. cost: £{total_wh / 1000 * COST_PER_KWH:.3f}")


def cmd_graph_heatmap(days=7):
    """Hour-of-day x day heatmap of average primary power (PSYS or package)."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_start - timedelta(days=days - 1)
    primary = primary_sql_expr()

    conn = get_conn()
    rows = conn.execute(f"""
        SELECT
            date(datetime(timestamp, 'unixepoch')) as day,
            CAST(strftime('%H', datetime(timestamp, 'unixepoch')) AS INTEGER) as hour,
            AVG({primary}) as avg_primary
        FROM power_samples
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY day, hour
        ORDER BY day, hour
    """, (start.timestamp(), (today_start + timedelta(days=1)).timestamp())).fetchall()
    conn.close()

    if not rows:
        print("No data for heatmap.", file=sys.stderr)
        sys.exit(1)

    day_labels = []
    for i in range(days):
        d = start + timedelta(days=i)
        day_labels.append(d.strftime("%a %d"))

    grid = np.full((days, 24), np.nan)
    for r in rows:
        day_str = r["day"]
        day_dt = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        day_idx = (day_dt - start).days
        if 0 <= day_idx < days:
            grid[day_idx, r["hour"]] = r["avg_primary"]

    _apply_style()
    fig, ax = plt.subplots(figsize=(14, max(4, days * 0.35)))

    im = ax.imshow(grid, aspect="auto", cmap=HEATMAP_CMAP,
                   interpolation="nearest", vmin=0)

    ax.set_xticks(range(24))
    ax.set_xticklabels([f"{h:02d}" for h in range(24)], fontsize=8)
    ax.set_yticks(range(days))
    ax.set_yticklabels(day_labels, fontsize=9)

    ax.set_xlabel("Hour of Day (UTC)")
    ax.set_title(f"Power Heatmap — Last {days} Days", loc="left", fontweight="bold")

    cbar = fig.colorbar(im, ax=ax, pad=0.02)
    cbar.set_label("Average Power (W)", fontsize=10)

    if days <= 14:
        for i in range(days):
            for j in range(24):
                if not np.isnan(grid[i, j]):
                    val = grid[i, j]
                    color = "white" if val > np.nanpercentile(grid, 50) else "black"
                    ax.text(j, i, f"{val:.1f}", ha="center", va="center",
                            fontsize=6, color=color)

    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PLOTS_DIR / f"power_heatmap_{today_start.strftime('%Y%m%d')}.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    print(f"Saved: {out_path}")
    print(f"  {days}-day heatmap of average power (PSYS or package)")


def cmd_graph_month():
    """Monthly view: 30-day heatmap + daily energy bar chart."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_start - timedelta(days=29)
    primary = primary_sql_expr()

    conn = get_conn()
    rows = conn.execute(f"""
        SELECT
            date(datetime(timestamp, 'unixepoch')) as day,
            AVG({primary}) as avg_primary,
            MAX({primary}) as max_primary,
            MIN({primary}) as min_primary,
            COUNT(*) as samples
        FROM power_samples
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY day
        ORDER BY day
    """, (start.timestamp(), (today_start + timedelta(days=1)).timestamp())).fetchall()

    heat_rows = conn.execute(f"""
        SELECT
            date(datetime(timestamp, 'unixepoch')) as day,
            CAST(strftime('%H', datetime(timestamp, 'unixepoch')) AS INTEGER) as hour,
            AVG({primary}) as avg_primary
        FROM power_samples
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY day, hour
        ORDER BY day, hour
    """, (start.timestamp(), (today_start + timedelta(days=1)).timestamp())).fetchall()
    conn.close()

    days = 30
    day_labels = []
    bar_energies = []
    day_map = {r["day"]: r for r in rows}

    for i in range(days):
        d = start + timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        day_labels.append(d.strftime("%d"))
        if d_str in day_map:
            r = day_map[d_str]
            hours_collected = r["samples"] * SAMPLE_INTERVAL / 3600
            bar_energies.append((r["avg_primary"] or 0) * hours_collected / 1000)
        else:
            bar_energies.append(0)

    _apply_style()
    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(14, 8),
        gridspec_kw={"height_ratios": [2, 1], "hspace": 0.25},
    )

    grid = np.full((days, 24), np.nan)
    for r in heat_rows:
        day_dt = datetime.strptime(r["day"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        day_idx = (day_dt - start).days
        if 0 <= day_idx < days:
            grid[day_idx, r["hour"]] = r["avg_primary"]

    im = ax_top.imshow(grid, aspect="auto", cmap=HEATMAP_CMAP, interpolation="nearest", vmin=0)
    ax_top.set_xticks(range(24))
    ax_top.set_xticklabels([f"{h:02d}" for h in range(24)], fontsize=7)
    ax_top.set_yticks(range(0, days, 2))
    ax_top.set_yticklabels([day_labels[i] for i in range(0, days, 2)], fontsize=8)
    ax_top.set_xlabel("Hour of Day (UTC)")
    ax_top.set_ylabel("Day")
    ax_top.set_title("Hourly Power (W)", loc="left", fontweight="bold")
    fig.colorbar(im, ax=ax_top, pad=0.02, label="Avg Power (W)")

    x = np.arange(days)
    bars = ax_bot.bar(x, bar_energies, color=SEABORN_DEEP[0], edgecolor="none", width=0.7)
    bars[-1].set_color(SEABORN_DEEP[6])

    roll = _rolling_mean(np.array(bar_energies), 7)
    ax2 = ax_bot.twinx()
    ax2.plot(x, roll, color=SEABORN_DEEP[1], lw=1.8, label="7-day rolling avg")
    ax2.set_ylabel("Rolling Avg Energy (kWh)", color=SEABORN_DEEP[1], fontsize=9)
    ax2.tick_params(axis="y", colors=SEABORN_DEEP[1])
    ax2.set_ylim(bottom=0)

    ax_bot.set_xticks(range(0, days, 2))
    ax_bot.set_xticklabels([day_labels[i] for i in range(0, days, 2)], fontsize=8)
    ax_bot.set_ylabel("Energy (kWh)")
    ax_bot.set_title("Daily Energy + 7-day Rolling Average", loc="left", fontweight="bold")
    ax_bot.set_ylim(bottom=0)

    total_kwh = sum(bar_energies)
    total_cost = total_kwh * COST_PER_KWH
    ax_bot.text(0.99, 0.95,
                f"30-day total: {total_kwh:.2f} kWh\nEst. cost: £{total_cost:.2f}",
                transform=ax_bot.transAxes, ha="right", va="top", fontsize=10,
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="gray", alpha=0.8))

    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PLOTS_DIR / f"power_month_{today_start.strftime('%Y%m%d')}.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    print(f"Saved: {out_path}")
    print(f"  30-day heatmap + daily energy chart")
    print(f"  30-day total: {total_kwh:.2f} kWh  (£{total_cost:.2f})")


def cmd_graph_week():
    """Bar chart of daily energy for the last 7 days."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today_start - timedelta(days=6)  # 7 days including today
    primary = primary_sql_expr()

    conn = get_conn()
    sql_rows = conn.execute(
        f"""
        SELECT
            date(datetime(timestamp, 'unixepoch')) as day,
            AVG({primary}) as avg_primary,
            AVG(package_w) as avg_pkg,
            COUNT(*) as samples
        FROM power_samples
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY date(datetime(timestamp, 'unixepoch'))
        ORDER BY day
        """,
        (week_ago.timestamp(), (today_start + timedelta(days=1)).timestamp()),
    ).fetchall()
    conn.close()

    by_day = {}
    for r in sql_rows:
        by_day[r["day"]] = dict(r)

    days = []
    labels = []
    energies = []
    avg_powers = []

    for i in range(6, -1, -1):
        day_start = today_start - timedelta(days=i)
        day_str = day_start.strftime("%Y-%m-%d")
        days.append(day_start)
        labels.append(day_start.strftime("%a\n%d"))

        d = by_day.get(day_str)
        if d and d["samples"] > 0:
            samples = d["samples"]
            hours_collected = samples * SAMPLE_INTERVAL / 3600
            avg_primary = d["avg_primary"] if d["avg_primary"] is not None else (d["avg_pkg"] or 0)
            energy_kwh = (avg_primary or 0) * hours_collected / 1000
            energies.append(energy_kwh)
            avg_powers.append(avg_primary if avg_primary is not None else 0)
        else:
            energies.append(0)
            avg_powers.append(0)

    _apply_style()

    fig, ax = plt.subplots(figsize=(10, 5))

    x = np.arange(len(days))
    bars = ax.bar(x, energies, color=SEABORN_DEEP[0], edgecolor="white", lw=0.5, width=0.65)

    y_max_data = max(energies) if energies else 0.001
    for i, (bar, e) in enumerate(zip(bars, energies)):
        if e > 0:
            label_text = f"{e:.3f}"
            if i == len(bars) - 1:
                d_str = days[i].strftime("%Y-%m-%d")
                if d_str in by_day:
                    hours_collected = by_day[d_str]["samples"] * SAMPLE_INTERVAL / 3600
                    if hours_collected < 23:
                        label_text = f"{e:.3f}*"
                        bar.set_hatch("//")
                        bar.set_edgecolor("white")
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + y_max_data * 0.02,
                    label_text, ha="center", va="bottom", fontsize=9, fontweight="bold")

    ax2 = ax.twinx()
    ax2.plot(x, avg_powers, color=SEABORN_DEEP[1], marker="o", ms=6, lw=2.0, label="Avg power (W)")
    ax2.set_ylabel("Average Power (W)", color=SEABORN_DEEP[1])
    ax2.tick_params(axis="y", colors=SEABORN_DEEP[1])
    ax2.set_ylim(bottom=0)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylabel("Energy (kWh)")
    ax.set_title(f"Weekly Energy Consumption — {HOSTNAME}", loc="left", fontweight="bold")
    ax.set_ylim(0, max(energies) * 1.20 if max(energies) > 0 else 1)

    bars[-1].set_color(SEABORN_DEEP[6])

    legend_elements = [
        Patch(facecolor=SEABORN_DEEP[0], label="Energy (kWh)"),
        Patch(facecolor=SEABORN_DEEP[6], label="Today (partial)"),
        Line2D([0], [0], color=SEABORN_DEEP[1], marker="o", lw=2.0, label="Avg power (W)"),
    ]
    ax.legend(handles=legend_elements, loc="upper left")

    total_kwh = sum(energies)
    total_cost = total_kwh * COST_PER_KWH
    ax.text(0.99, 0.95, f"7-day total: {total_kwh:.3f} kWh\nEst. cost: £{total_cost:.2f}",
            transform=ax.transAxes, ha="right", va="top", fontsize=10,
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="gray", alpha=0.8))

    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PLOTS_DIR / f"power_week_{today_start.strftime('%Y%m%d')}.png"
    fig.savefig(out_path, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    print(f"Saved: {out_path}")
    print(f"  7-day total: {total_kwh:.3f} kWh  (£{total_cost:.2f})")


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def cmd_probe():
    """List available energy backends and domains."""
    sys.exit(run_probe())


def main():
    """Parse arguments and dispatch to the appropriate command."""
    parser = argparse.ArgumentParser(
        description=f"Power monitoring CLI (v{__version__})"
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="Show current power draw")
    sub.add_parser("today", help="Show today's energy summary")
    sub.add_parser("probe", help="List available energy backends/domains")

    r = sub.add_parser("range", help="Summary for date range")
    r.add_argument("start", help="Start date (YYYY-MM-DD)")
    r.add_argument("end", nargs="?", default=None, help="End date (YYYY-MM-DD, optional)")

    w = sub.add_parser("watch", help="Live power display")
    w.add_argument("n", nargs="?", type=int, default=20, help="Number of updates (default: 20)")

    sub.add_parser("export", help="Export last 7 days as CSV (raw samples)")
    sub.add_parser("daily-log", help="Append yesterday's summary to daily CSV")

    g = sub.add_parser("graph", help="Generate power graph")
    g.add_argument("view", nargs="?", default="today",
                   help="View: today|yesterday|week|month|range|all|heatmap (default: today)")
    g.add_argument("start", nargs="?", default=None, help="Start date for 'range' (YYYY-MM-DD)")
    g.add_argument("end", nargs="?", default=None, help="End date for 'range' (YYYY-MM-DD)")

    args = parser.parse_args()

    if args.cmd == "status":
        cmd_status()
    elif args.cmd == "today":
        cmd_today()
    elif args.cmd == "probe":
        cmd_probe()
    elif args.cmd == "range":
        cmd_range(args.start, args.end)
    elif args.cmd == "watch":
        cmd_watch(args.n)
    elif args.cmd == "export":
        cmd_export()
    elif args.cmd == "daily-log":
        cmd_daily_log()
    elif args.cmd == "graph":
        cmd_graph(args.view, getattr(args, "start", None), getattr(args, "end", None))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()