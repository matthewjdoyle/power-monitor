# power-monitor

Real-time CPU energy monitoring for **Linux** (Intel and AMD) and **Windows 11** (Intel and AMD). Tracks package (and available subdomain) power at configurable intervals, logs to SQLite, and generates publication-quality graphs with daily/weekly/monthly reporting.

## Capability matrix

| Platform | CPU | Measurement source | Elevated privileges? | Typical domains |
|----------|-----|--------------------|----------------------|-----------------|
| Linux | Intel | `/sys/class/powercap/intel-rapl/` | Yes (root) | package, cores, uncore, dram, psys |
| Linux | AMD Zen+ | Same powercap tree (despite the `intel-rapl` name), or `amd_energy` hwmon fallback | Yes (root) | package, cores |
| Windows 11 | Intel / AMD | Energy Meter Interface (EMI) — no third-party driver | No | package (+ cores/dram if exposed) |

**Primary metric:** platform total (`psys`) when available, otherwise CPU package power. AMD systems usually have package only.

**Single-socket assumption:** multi-socket package domains currently map to one `package_w` column (first socket wins).

## Requirements

- Python 3.10+
- `matplotlib`, `numpy` (`pip install -r requirements.txt`)
- **Linux:** RAPL exposed under `/sys/class/powercap/intel-rapl/` (Intel and modern AMD), or the `amd_energy` hwmon driver
- **Windows 11:** bare-metal machine with EMI energy meters (usually present; often absent in VMs)

## Installation

### Common: clone and install the package

```bash
git clone https://github.com/YOUR_USERNAME/power-monitor.git
cd power-monitor
pip install -r requirements.txt
pip install .
```

### Probe hardware before starting the collector

```bash
power-monitor probe
# or:
python -m power_monitor probe
python -m power_monitor.collector --probe
```

You should see at least one backend with a selected `package` domain. On Windows, raw EMI channel names are also listed.

---

### Linux (Intel or AMD)

1. Install the package (above). Ensure the collector entry point is on PATH (or use `/usr/local/bin` after a system install).

2. Create the shared data directory and point both the service and your user CLI at it:

```bash
sudo mkdir -p /var/lib/power-monitor
sudo chmod 755 /var/lib/power-monitor
# Optional: allow your user to read the DB without root
sudo chgrp "$(id -gn)" /var/lib/power-monitor
sudo chmod 775 /var/lib/power-monitor
```

3. Install the systemd unit (uses `/var/lib/power-monitor` so root and user share one DB):

```bash
sudo cp deploy/power-monitor-collector.service /etc/systemd/system/
# If the console script is not at /usr/local/bin, edit ExecStart in the unit.
sudo systemctl daemon-reload
sudo systemctl enable --now power-monitor-collector
```

4. Configure your shell to use the same data dir:

```bash
export POWER_MONITOR_DATA_DIR=/var/lib/power-monitor
# Optional: persist in ~/.bashrc / ~/.profile
```

5. Verify:

```bash
systemctl status power-monitor-collector
power-monitor status
power-monitor today
```

**Note for AMD on Linux:** modern kernels expose Zen RAPL counters under `/sys/class/powercap/intel-rapl/` even though the path says “intel”. If that tree is empty, the collector falls back to the `amd_energy` hwmon driver when loaded.

---

### Windows 11 (Intel or AMD)

1. Install Python 3.10+ from [python.org](https://www.python.org/downloads/) (check “Add python.exe to PATH”).

2. In PowerShell from the repo root:

```powershell
pip install -r requirements.txt
pip install .
```

If pip warns that scripts are not on PATH, either add  
`%APPDATA%\Python\Python313\Scripts` (adjust version) to your **user** PATH and open a new terminal, or always use module form:

```powershell
python -m power_monitor probe
```

If probe reports no EMI devices, collection will not work on this machine (common in VMs). Do not install WinRing0 or similar vulnerable drivers.

3. Register a **background** Task Scheduler job (starts at logon, no admin, **no console window**; data under `%LOCALAPPDATA%\power-monitor`):

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\windows\Register-CollectorTask.ps1
```

This uses `pythonw.exe` so the collector runs hidden. It does **not** appear under Settings → Apps → Startup; manage it in **Task Scheduler** (`Win+R` → `taskschd.msc` → task `PowerMonitorCollector`). Logs append to `%LOCALAPPDATA%\power-monitor\collector.log`.

To run the collector in a visible terminal for debugging instead:

```powershell
python -m power_monitor.collector
```

4. Wait ~20 seconds, then:

```powershell
python -m power_monitor status
python -m power_monitor today
```

5. To remove the collector task:

```powershell
.\deploy\windows\Unregister-CollectorTask.ps1
```

Graphs are written to `%LOCALAPPDATA%\power-monitor\plots\`.

## Configuration

All settings are environment variables. Defaults work for most systems.

| Variable | Default | Description |
|----------|---------|-------------|
| `POWER_MONITOR_DATA_DIR` | Linux: `~/.local/share/power-monitor`<br>Windows: `%LOCALAPPDATA%\power-monitor`<br>Linux systemd unit: `/var/lib/power-monitor` | Data directory for DB, CSV, and plots |
| `POWER_MONITOR_DB` | `<data_dir>/power.db` | SQLite database path |
| `POWER_MONITOR_CSV` | `<data_dir>/daily_summary.csv` | Daily summary CSV path |
| `POWER_MONITOR_SAMPLE_INTERVAL` | `10` | Seconds between samples (collector) |
| `POWER_MONITOR_COST_PER_KWH` | `0.34` | Electricity cost in GBP for estimates |
| `POWER_MONITOR_HOSTNAME` | `socket.gethostname()` | Hostname shown in weekly chart title |

Example (UK tariff at 27p/kWh):

```bash
export POWER_MONITOR_COST_PER_KWH=0.27
```

PowerShell:

```powershell
[System.Environment]::SetEnvironmentVariable("POWER_MONITOR_COST_PER_KWH", "0.27", "User")
```

## Usage

### CLI commands

```
power-monitor probe           List energy backends and domains
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

### Graph types

#### Time-series (today, yesterday, range, all)

Two-panel figure:
- **Top panel:** Power (W) over time. Primary metric (PSYS if present, else package) as a bold rolling average with a faint raw line. When PSYS is primary, package is shown as a thin secondary trace.
- **Bottom panel:** Cumulative energy (Wh) integrated from the primary metric, with total and estimated cost.

#### Weekly / monthly / heatmap

Use the same primary metric (`COALESCE(psys_w, package_w)`), so AMD systems without PSYS still graph correctly.

### Daily CSV

`power-monitor daily-log` appends yesterday's summary:

```csv
date,avg_power_w,peak_power_w,idle_power_w,energy_kwh,energy_kwh_psys,duration_h,samples,est_cost_gbp
2026-07-18,1.212,6.934,1.058,0.026399,0.101623,21.77,7835,0.0090
```

Energy uses trapezoidal integration. `energy_kwh` uses the primary metric; `energy_kwh_psys` is PSYS-specific (0 when absent).

### Automated reporting

**Linux (cron):**

```bash
0 8 * * * POWER_MONITOR_DATA_DIR=/var/lib/power-monitor /usr/local/bin/power-monitor daily-log
0 9 * * 1 POWER_MONITOR_DATA_DIR=/var/lib/power-monitor /usr/local/bin/power-monitor graph week
```

**Windows:** Task Scheduler can run `power-monitor daily-log` on a daily trigger the same way as the collector.

## Power domains

| Domain | What it measures |
|--------|------------------|
| `package` | Whole CPU package (cores + uncore + GPU where applicable) |
| `cores` | CPU cores / PP0 |
| `uncore` | Uncore / iGPU / SoC rails when exposed |
| `dram` | DRAM power (accuracy varies) |
| `psys` | Platform total (closest to wall power; Intel-centric) |

## What hardware counters don't measure

CPU energy APIs cover package/SoC power but miss:
- **Storage** (NVMe/SATA): typically 0.1–1 W idle, 3–5 W active
- **Discrete GPU** (unless included in package/APU readings)
- **Fans / PSU AC losses** (~85–90% efficient)
- **USB peripherals**

For wall-power tracking, pair with a smart plug (e.g. TP-Link KP115) and compare against package/PSYS.

## Architecture

```
┌──────────────────────────────────┐
│  Linux powercap / amd_energy     │
│  or Windows EMI                  │
└──────────────┬───────────────────┘
               │ sample every N seconds
               v
┌──────────────────────────────────┐
│  power-monitor-collector         │
│  (systemd / Task Scheduler)      │
│  Computes dE/dt → watts          │
│  Writes to SQLite                │
└──────────────┬───────────────────┘
               v
┌──────────────────────────────────┐
│  SQLite (power.db)               │
│  WAL mode, ~5 MB/month           │
└──────────────┬───────────────────┘
               v
┌──────────────────────────────────┐
│  power-monitor (CLI)             │
│  status / today / range / probe  │
│  graph / export / daily-log      │
└──────────────────────────────────┘
```

## Files

| Path | Purpose |
|------|---------|
| `power_monitor/collector.py` | Collector daemon |
| `power_monitor/cli.py` | CLI query and graph tool |
| `power_monitor/backends/` | Linux powercap, amd_energy, Windows EMI |
| `power_monitor/config.py` | Cross-platform paths and settings |
| `power_monitor/schema.py` | SQLite schema and primary-metric helpers |
| `deploy/power-monitor-collector.service` | Linux systemd unit |
| `deploy/windows/Register-CollectorTask.ps1` | Windows Task Scheduler installer |
| `requirements.txt` | Python dependencies |

## License

MIT. See [LICENSE](LICENSE).
