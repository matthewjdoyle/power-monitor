"""Shared configuration for collector and CLI."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path


def default_data_dir() -> Path:
    """Return the platform-appropriate default data directory.

    Linux/macOS: ~/.local/share/power-monitor
    Windows:     %LOCALAPPDATA%\\power-monitor
    Override with POWER_MONITOR_DATA_DIR.
    """
    env = os.environ.get("POWER_MONITOR_DATA_DIR")
    if env:
        return Path(env)

    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "power-monitor"
        return Path.home() / "AppData" / "Local" / "power-monitor"

    return Path.home() / ".local" / "share" / "power-monitor"


DATA_DIR = default_data_dir()
DB_PATH = Path(os.environ.get("POWER_MONITOR_DB", str(DATA_DIR / "power.db")))
CSV_PATH = Path(os.environ.get("POWER_MONITOR_CSV", str(DATA_DIR / "daily_summary.csv")))
PLOTS_DIR = DATA_DIR / "plots"

SAMPLE_INTERVAL = int(os.environ.get("POWER_MONITOR_SAMPLE_INTERVAL", "10"))
COST_PER_KWH = float(os.environ.get("POWER_MONITOR_COST_PER_KWH", "0.34"))
HOSTNAME = os.environ.get("POWER_MONITOR_HOSTNAME", socket.gethostname())

# Shared Linux systemd data dir (collector as root + CLI as user)
LINUX_SYSTEM_DATA_DIR = Path("/var/lib/power-monitor")
