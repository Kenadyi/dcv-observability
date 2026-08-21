from __future__ import annotations

import unittest
from unittest.mock import patch

import app.collector as collector


class HostnameDetectionTests(unittest.TestCase):
    def test_uses_detected_local_hostname(self):
        with patch.object(
            collector, "run", return_value=(0, "dcv-host.example.com", "")
        ):
            self.assertEqual(collector.get_hostname(), "dcv-host.example.com")

    def test_uses_generic_fallback_when_detection_fails(self):
        with patch.object(collector, "run", return_value=(1, "", "unavailable")):
            self.assertEqual(collector.get_hostname(), "dcv-host")


if __name__ == "__main__":
    unittest.main()
