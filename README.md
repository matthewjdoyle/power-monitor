# power-monitor

Real-time energy monitoring for Linux systems with Intel RAPL (Running Average Power Limit) hardware counters. Tracks CPU package, cores, uncore, DRAM, and platform total (PSYS) power at configurable intervals. Logs to SQLite, generates publication-quality graphs, and supports automated daily/weekly/monthly reporting.

## Requirements

- Linux system with Intel CPU (RAPL must be exposed at `/sys/class/powercap/intel-rapl/`)
- Python 3.10+
- Root access for the collector (RAPL sysfs files are root-only)
- `matplotlib`, `numpy` (install via `pip install -r requirements.txt`)

## Installation

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/power-monitor.git
cd power-monitor
pip install -r requirements.txt
```

### 2. Install the scripts

```bash
# Install as a package (recommended)
pip install .

# Or copy scripts manually
sudo cp power_monitor/collector.py /usr/local/bin/power-monitor-collector
sudo chmod +x /usr/local/bin/power-monitor-collector
cp power_monitor/cli.py /usr/local/bin/power-monitor
chmod +x /usr/local/bin/power-monitor
```

### 3. Install the systemd service

The collector must run as root to read RAPL counters.

```bash
sudo cp deploy/power-monitor-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now power-monitor-collector
```

### 4. Verify

```bash
# Check the collector is running
systemctl status power-monitor-collector

# Check current power draw
power-monitor status

# Check today's energy
power-monitor today
```

## Configuration

All settings are configured via environment variables. Defaults work for most systems.

| Variable | Default | Description |
|----------|---------|-------------|
| `POWER_MONITOR_DATA_DIR` | `~/.local/share/power-monitor` | Data directory for DB, CSV, and plots |
| `POWER_MONITOR_DB` | `<data_dir>/power.db` | SQLite database path |
| `POWER_MONITOR_CSV` | `<data_dir>/daily_summary.csv` | Daily summary CSV path |
| `POWER_MONITOR_SAMPLE_INTERVAL` | `10` | Seconds between samples (collector) |
| `POWER_MONITOR_COST_PER_KWH` | `0.34` | Electricity cost in GBP for estimates |
| `POWER_MONITOR_HOSTNAME` | `socket.gethostname()` | Hostname shown in weekly chart title |

Set `POWER_MONITOR_COST_PER_KWH` to your actual electricity tariff for accurate cost estimates. For example, on a UK system at 27p/kWh:

```bash
export POWER_MONITOR_COST_PER_KWH=0.27
```

## Usage

### CLI commands

```
power-monitor status          Show current power draw (last sample)
power-monitor today           Show today's energy summary
power-monitor range A [B]     Summary for date range (YYYY-MM-DD [YYYY-MM-DD])
power-monitor watch [N]       Live power display (default: 20 updates)
power-monitor export          Dump last 7 days as CSV
power-monitor daily-log       Append yesterday's summary to daily CSV

power-monitor graph [view]    Generate a PNG graph
  Views:
    today       Today's power time-series (default)
    yesterday   Yesterday's power time-series
    week        Bar chart of last 7 days daily energy
    month       30-day heatmap + daily energy bar chart
    heatmap     Hour-of-day x day heatmap (last 7 days)
    all         All data in the database (adaptive downsampling)
    range A B   Custom date range
```

All graphs are saved to `~/.local/share/power-monitor/plots/`.

### Graph types

#### Time-series (today, yesterday, range, all)

Two-panel figure:
- **Top panel**: Power (W) over time. PSYS (platform total) is the primary metric, shown as a bold rolling average with a faint raw line behind it. Package power is shown as a thin secondary trace. Y-axis is clipped at the 99th percentile to prevent outliers from squashing the scale.
- **Bottom panel**: Cumulative energy (Wh) integrated from PSYS, with total and estimated cost annotated.

Adaptive downsampling keeps approximately 1,500 points regardless of data span. Bin size auto-scales: 60s for sub-day, 5min for a week, 30min for a month.

#### Weekly bar chart

Daily energy (kWh) as bars with an overlaid average power line (secondary axis). Today's bar is highlighted with a hatched pattern and asterisk label to indicate it is partial.

#### Hour-of-day heatmap

Day (y-axis) x hour (UTC, x-axis) grid coloured by average PSYS power. Uses the YlOrBr (yellow-orange-brown) colormap. Cell values are annotated when showing 14 days or fewer. Reveals daily usage patterns at a glance.

#### Monthly chart

Dual-panel 30-day overview:
- **Top**: Hour-of-day x day heatmap (same as above, 30 days)
- **Bottom**: Daily energy bars with a 7-day rolling average overlay on a secondary axis

#### All data

Full dataset with adaptive downsampling. Time axis format adapts to the span: hourly for sub-day, daily for weeks, bi-daily for months, weekly for longer.

### Daily CSV

Running `power-monitor daily-log` appends yesterday's summary to a CSV file:

```csv
date,avg_power_w,peak_power_w,idle_power_w,energy_kwh,energy_kwh_psys,duration_h,samples,est_cost_gbp
2026-07-18,1.212,6.934,1.058,0.026399,0.101623,21.77,7835,0.0090
```

Energy is computed via trapezoidal integration of actual power samples. `energy_kwh` uses PSYS (platform total) when available, falling back to package power. `energy_kwh_psys` is the PSYS-specific energy.

### Automated reporting with cron

Example crontab entries:

```bash
# Daily CSV append at 8am (silent, no notification)
0 8 * * * /usr/local/bin/power-monitor daily-log

# Weekly report on Mondays at 9am (generate graphs)
0 9 * * 1 /usr/local/bin/power-monitor graph week && /usr/local/bin/power-monitor graph heatmap && /usr/local/bin/power-monitor graph all
```

## RAPL domains

Intel RAPL exposes several power domains via `/sys/class/powercap/intel-rapl/`:

| Domain | What it measures |
|--------|------------------|
| `package-0` | Whole CPU package (cores + uncore + GPU) |
| `core` | IA cores only |
| `uncore` | Graphics, ring bus, L3 cache |
| `dram` | DRAM power (accuracy varies) |
| `psys` | Platform total (closest to wall power) |

PSYS is the most useful metric for cost estimation. On low-power CPUs (e.g. i5-7200U), the core, DRAM, and uncore domains may report near-zero values and are not plotted. Only package and PSYS are visualised in the time-series graphs.

## What RAPL doesn't measure

RAPL covers CPU/SoC power accurately but misses:
- **Storage** (NVMe/SATA SSDs): typically 0.1-1W idle, 3-5W active
- **Fans**: negligible on NUC-class systems
- **PSU efficiency**: RAPL measures DC, not AC wall power (~85-90% efficient)
- **USB peripherals**: a few watts if drawing bus power

For precise wall-power tracking, pair with a smart plug (e.g. TP-Link KP115) and compare PSYS readings.

## Architecture

```
┌─────────────────────────────┐
│     Intel CPU (RAPL)        │
│  /sys/class/powercap/       │
│  (energy_uj counters)       │
└──────────┬──────────────────┘
           | reads every N seconds (root)
           v
┌─────────────────────────────┐
│  power-monitor-collector     │
│  (systemd, root)             │
│  Computes dE/dt -> watts     │
│  Writes to SQLite            │
└──────────┬──────────────────┘
           v
┌─────────────────────────────┐
│  SQLite (power.db)           │
│  WAL mode, ~5MB/month        │
└──────────┬──────────────────┘
           v
┌─────────────────────────────┐
│  power-monitor (CLI)         │
│  status / today / range      │
│  graph / export / daily-log  │
└─────────────────────────────┘
```

## Files

| Path | Purpose |
|------|---------|
| `power_monitor/collector.py` | Collector daemon (runs as root via systemd) |
| `power_monitor/cli.py` | CLI query and graph tool (runs as user) |
| `power_monitor/__main__.py` | Entry point for `python -m power_monitor` |
| `deploy/power-monitor-collector.service` | systemd unit file |
| `requirements.txt` | Python dependencies |

## License

MIT. See [LICENSE](LICENSE).