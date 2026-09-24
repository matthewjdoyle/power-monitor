"""Unit tests for CLI plotting colors and styles."""

from __future__ import annotations

import unittest
import matplotlib.pyplot as plt

from power_monitor.cli import (
    COLOR_ACCENT,
    COLOR_CUMULATIVE,
    COLOR_PRIMARY,
    COLOR_SECONDARY,
    SEABORN_DEEP,
    _apply_style,
)


class TestCliColors(unittest.TestCase):
    def test_primary_and_secondary_colors(self):
        # Primary should be green, secondary should be purple
        self.assertEqual(COLOR_PRIMARY, "#55A868")
        self.assertEqual(COLOR_SECONDARY, "#8172B3")
        self.assertEqual(COLOR_ACCENT, "#DA8BC3")
        self.assertEqual(COLOR_CUMULATIVE, "#8C8C8C")

    def test_seaborn_deep_palette_order(self):
        # First two elements in palette must be primary (green) and secondary (purple)
        self.assertEqual(SEABORN_DEEP[0], COLOR_PRIMARY)
        self.assertEqual(SEABORN_DEEP[1], COLOR_SECONDARY)
        self.assertEqual(SEABORN_DEEP[6], COLOR_ACCENT)
        self.assertEqual(SEABORN_DEEP[7], COLOR_CUMULATIVE)

    def test_apply_style_prop_cycle(self):
        _apply_style()
        prop_cycle = plt.rcParams["axes.prop_cycle"]
        colors = [item["color"] for item in prop_cycle]
        self.assertEqual(colors[0], COLOR_PRIMARY)
        self.assertEqual(colors[1], COLOR_SECONDARY)


if __name__ == "__main__":
    unittest.main()
