# Example Datasets and Demonstration Plots

This directory contains synthetic, physically modeled power telemetry datasets for testing, documentation, and demonstration of `power-monitor` across three distinct hardware archetypes.

All datasets span **30 full days** sampled at 10-second intervals (`POWER_MONITOR_SAMPLE_INTERVAL=10`), with realistic temporal correlation (Ornstein-Uhlenbeck random walk), physical power domain hierarchy, scheduled recurring workloads, and diurnal behavior.

---

## Hardware Profiles

### 1. `nas_server` (`truenas-core`)
- **Hardware Archetype:** Low-power home storage / mini-ITX server (e.g., Intel Celeron / Core i3 or AMD Ryzen Embedded).
- **Domains Monitored:** `package_w`, `cores_w`, `uncore_w`, `dram_w` (`psys_w` omitted, demonstrating package-only monitoring common on AMD/desktop boards).
- **Behavioral Characteristics:**
  - **Uptime:** Continuous 24/7 (0% downtime / no sleep gaps).
  - **Base Idle:** ~9.5 W – 11.5 W Package power.
  - **Diurnal Drift:** Ambient room temperature and fan speed fluctuations (+0.4 W).
  - **Nightly Backup & Snapshot Sync:** 02:30 – 03:45 UTC daily (hashing, disk I/O, compression; package power rises to 28 W – 36 W).
  - **Weekly RAID / ZFS Scrub:** Every Sunday 03:30 – 07:30 UTC (sustained disk verification at 32 W – 42 W).
  - **Media Streaming / Transcoding:** Evening sessions (20:00 – 23:15 UTC) with transcoder burst cycles (filling 60s buffer at ~32 W, idling at ~14 W).

### 2. `ml_inference` (`gpu-inference-01`)
- **Hardware Archetype:** Dedicated high-TDP multi-core workstation / server (e.g., Intel Xeon / Core i9 / AMD Threadripper).
- **Domains Monitored:** Full platform telemetry including `psys_w` (platform total), `package_w`, `cores_w`, `uncore_w`, and `dram_w`.
- **Behavioral Characteristics:**
  - **Uptime:** Continuous 24/7 (0% downtime).
  - **Base Idle:** High baseline idle at ~48 W Package, ~16 W DRAM, ~125 W Platform (`psys_w`).
  - **Business Hours API Traffic (09:00 – 18:30 UTC):** Poisson-distributed inference request bursts pushing Package power to 140 W – 230 W and Platform (`psys_w`) to 260 W – 370 W, with distinct weekday vs. weekend volume differences.
  - **Scheduled Nightly Batch Processing (01:00 – 04:15 UTC):** Long-running embedding re-indexing and validation run. Sustained 200 W+ package draw demonstrating thermal limit regulation (PL1/PL2 power droop as heatsinks heat up).

### 3. `laptop_daily` (`thinkpad-x1`)
- **Hardware Archetype:** Modern ultrabook / productivity laptop (e.g., Intel Core i7 / Ultra or AMD Ryzen Mobile).
- **Domains Monitored:** `psys_w`, `package_w`, `cores_w`, `uncore_w`, `dram_w` (where `psys_w` includes the display backlight, WiFi radio, and motherboard power).
- **Behavioral Characteristics:**
  - **Uptime & Suspend Gaps:** Genuine power-off / suspend periods (23:30 to 08:15 on weekdays, 01:15 to 09:45 on weekends, and lunch/commute gaps). The collector daemon stops during suspend, creating real time gaps (>60s) that `power-monitor` renders as clean breaks in time series.
  - **Desk Work Sessions:** 12 W – 22 W baseline with intermittent compiler runs (`cargo build`, `docker build`) exhibiting Turbo Boost (PL2 spike to 55 W – 65 W decaying to PL1 sustained power at ~35 W).
  - **Screen-Lock Idle:** Drops to ~3.2 W – 4.5 W package power during lunch break.
  - **Video Call:** 14:00 – 15:15 UTC continuous camera capture and video decoding at 22 W – 30 W package power.

---

## Directory Contents

Each profile folder contains:
- `power.db`: SQLite database populated with `power_samples` according to the official schema.
- `daily_summary.csv`: Precomputed daily aggregate table matching `power-monitor daily-log` schema.
- `plots/`: Rendered publication-quality PNG charts:
  - `power_all_<timestamp>.png`: All-time 30-day comprehensive dashboard (timeline, rolling avg, cumulative energy, analytics table).
  - `power_today_<date>.png`: Today's partial day power curve, cumulative energy, and LaTeX summary table.
  - `power_<date>.png`: Yesterday's full 24-hour power curve and summary table.
  - `power_<start>_to_<end>.png`: Multi-day custom range dashboard (e.g. 4-day workload or weekend window).
  - `power_week_<date>.png`: 7-day daily energy consumption dashboard and cost projection table.
  - `power_month_<date>.png`: 30-day hour-of-day heatmap and daily energy bar chart.
  - `power_heatmap_<date>.png`: 7-day hour-of-day power intensity heatmap.

---

## Inspecting and Plotting with the CLI

You can point `power-monitor` directly at any of these example datasets using the `POWER_MONITOR_DATA_DIR` environment variable:

```bash
# View current status / today's summary for the NAS server
POWER_MONITOR_DATA_DIR=examples/nas_server python -m power_monitor today

# View weekly summary for the ML inference server
POWER_MONITOR_DATA_DIR=examples/ml_inference python -m power_monitor graph week

# View the 30-day heatmap for the laptop
POWER_MONITOR_DATA_DIR=examples/laptop_daily python -m power_monitor graph month

# Inspect daily summaries
cat examples/laptop_daily/daily_summary.csv
```

---

## Regenerating Data

To regenerate all datasets or customize simulation parameters:

```bash
# Regenerate all 3 profiles (30 days of data + render all plots)
python examples/generate_example_data.py

# Generate only the laptop profile for 14 days
python examples/generate_example_data.py --profile laptop_daily --days 14

# Generate without rendering plots
python examples/generate_example_data.py --render-plots=false
```
