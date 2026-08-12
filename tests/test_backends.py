"""Unit tests for schema mapping, energy wrap math, and EMI helpers."""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

from power_monitor.backends.base import energy_delta
from power_monitor.backends.linux_powercap import LinuxPowercapBackend
from power_monitor.backends.windows_emi import (
    map_emi_channel_to_key,
    parse_channel_names,
    parse_measurements,
    pwh_to_joules,
)
from power_monitor.schema import (
    init_db,
    map_domain_name_to_key,
    primary_label,
    primary_power,
    primary_sql_expr,
)


class TestDomainMapping(unittest.TestCase):
    def test_intel_names(self):
        self.assertEqual(map_domain_name_to_key("package-0"), "package")
        self.assertEqual(map_domain_name_to_key("core"), "cores")
        self.assertEqual(map_domain_name_to_key("uncore"), "uncore")
        self.assertEqual(map_domain_name_to_key("dram"), "dram")
        self.assertEqual(map_domain_name_to_key("psys"), "psys")

    def test_emi_intel_channels(self):
        self.assertEqual(map_emi_channel_to_key("RAPL_Package0_PKG"), "package")
        self.assertEqual(map_emi_channel_to_key("RAPL_Package0_PP0"), "cores")
        self.assertEqual(map_emi_channel_to_key("RAPL_Package0_PP1"), "uncore")
        self.assertEqual(map_emi_channel_to_key("RAPL_Package0_DRAM"), "dram")

    def test_emi_amd_channels(self):
        self.assertEqual(map_emi_channel_to_key("Current Socket Energy"), "package")
        self.assertEqual(map_emi_channel_to_key("Apu Energy"), "package")
        self.assertEqual(map_emi_channel_to_key("VDDCR_VDD Energy"), "package")
        self.assertEqual(map_emi_channel_to_key("VDDCR_SOC Energy"), "uncore")

    def test_skips_per_core_rapl(self):
        self.assertIsNone(map_emi_channel_to_key("RAPL_Package0_Core3_CORE"))


class TestEnergyDelta(unittest.TestCase):
    def test_no_wrap(self):
        self.assertEqual(energy_delta(10.0, 15.0, 100.0), 5.0)

    def test_wrap_with_max(self):
        # prev=90, curr=5, max=100 → (100-90)+5 = 15
        self.assertEqual(energy_delta(90.0, 5.0, 100.0), 15.0)

    def test_wrap_without_max(self):
        self.assertEqual(energy_delta(90.0, 5.0, None), 5.0)


class TestPrimaryMetric(unittest.TestCase):
    def test_prefers_psys(self):
        self.assertEqual(primary_power({"psys_w": 12.0, "package_w": 8.0}), 12.0)

    def test_falls_back_to_package(self):
        self.assertEqual(primary_power({"psys_w": None, "package_w": 8.0}), 8.0)

    def test_labels(self):
        self.assertIn("PSYS", primary_label(True))
        self.assertEqual(primary_label(False), "Package")

    def test_sql_expr(self):
        self.assertEqual(primary_sql_expr(), "COALESCE(psys_w, package_w)")


class TestPwhConversion(unittest.TestCase):
    def test_known_value(self):
        # 1e12 picowatt-hours = 1 watt-hour = 3600 J
        self.assertAlmostEqual(pwh_to_joules(10**12), 3600.0, places=6)


class TestEmiParsing(unittest.TestCase):
    def test_parse_measurements(self):
        raw = struct.pack("<QQ", 1000, 2000) + struct.pack("<QQ", 3000, 4000)
        ms = parse_measurements(raw)
        self.assertEqual(ms, [(1000, 2000), (3000, 4000)])

    def test_parse_v2_channel_names(self):
        # Minimal V2 metadata: 64 bytes OEM+Model (16+16 WCHARs = 64),
        # then USHORT Revision + USHORT ChannelCount at offset 64/66,
        # then channels.
        # Layout: WCHAR[16] OEM (32), WCHAR[16] Model (32) = 64 bytes,
        # USHORT Revision (2) at 64, USHORT ChannelCount (2) at 66.
        header = b"\x00" * 64 + struct.pack("<HH", 1, 1)  # rev=1, count=1
        name = "RAPL_Package0_PKG"
        name_bytes = name.encode("utf-16-le") + b"\x00\x00"
        # EMI_CHANNEL_V2: int MeasurementUnit (4), USHORT ChannelNameSize (2), name
        channel = struct.pack("<iH", 0, len(name_bytes)) + name_bytes
        metadata = header + channel
        names = parse_channel_names(2, metadata)
        self.assertEqual(names, [name])


class TestLinuxPowercapFakeSysfs(unittest.TestCase):
    @unittest.skipIf(sys.platform.startswith("win"), "colon paths invalid on Windows")
    def test_discovers_package_and_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pkg = base / "intel-rapl:0"
            pkg.mkdir()
            (pkg / "name").write_text("package-0")
            (pkg / "energy_uj").write_text("1000000")
            (pkg / "max_energy_range_uj").write_text("100000000")

            core = pkg / "intel-rapl:0:0"
            core.mkdir()
            (core / "name").write_text("core")
            (core / "energy_uj").write_text("500000")

            domains = LinuxPowercapBackend(base=base).discover()
            keys = {d.key for d in domains}
            self.assertIn("package", keys)
            self.assertIn("cores", keys)

            package = next(d for d in domains if d.key == "package")
            self.assertEqual(package.read_joules(), 1.0)
            self.assertEqual(package.max_joules, 100.0)


class TestInitDb(unittest.TestCase):
    def test_creates_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "power.db"
            conn = init_db(db)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='power_samples'"
            ).fetchone()
            self.assertIsNotNone(row)
            conn.close()


if __name__ == "__main__":
    unittest.main()
