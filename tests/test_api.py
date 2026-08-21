from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app.dcv_logs as dcv_logs
import app.main as main


class SessionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.original_log_dir = dcv_logs.DCV_LOG_DIR
        dcv_logs.DCV_LOG_DIR = Path(self.temp_dir.name)
        self.addCleanup(setattr, dcv_logs, "DCV_LOG_DIR", self.original_log_dir)
        (dcv_logs.DCV_LOG_DIR / "agent.sample_user.sample_user.log").write_text(
            "2026-08-14 10:50:31.500 [agent-controller] INFO Lock requested\n"
            "ERROR: Unknown command 'lock'\n"
            "2026-08-14 10:50:31.530 [agent-controller] WARN "
            "OS session lock request failed: 4\n"
        )
        (dcv_logs.DCV_LOG_DIR / "dcv-xsession.sample_user.sample_user_session.log").write_text(
            "ERROR: unplaced session evidence\n"
        )
        main._cache = {
            "collected_at": "2026-08-15T12:00:00+00:00",
            "servers": [{
                "name": "dcv-host",
                "hostname": "dcv-host",
                "sessions": [{
                    "id": "sample_user",
                    "owner": "sample_user",
                    "type": "virtual",
                    "state": "running",
                    "age_seconds": 3600,
                    "dcv_process_cpu_pct": 1.2,
                    "dcv_process_mem_pct": 0.8,
                }, {
                    "id": "legacy_session",
                    "owner": "legacy_user",
                    "type": "virtual",
                    "state": "running",
                    "age_seconds": 1800,
                    "dcv_process_cpu_pct": 0.5,
                    "dcv_process_mem_pct": 0.4,
                }],
            }],
        }
        self.client = TestClient(main.app)

    def write_historical_sample_user(self):
        path = dcv_logs.DCV_LOG_DIR / "agent.sample_user.sample_user.log"
        historical = [
            "2026-08-14 15:55:42,162397 WARN agent - Failed to receive message "
            "from client: Connection closed by the peer",
            "2026-08-14 15:55:42,162434 INFO agent - Display channel disconnected",
            "2026-08-14 15:55:42,166851 INFO agent - Last client connection '4821' has been closed",
            "2026-08-14 15:55:42,167148 INFO agent - Server requested OS session lock",
            "ERROR: Unknown command 'lock'",
            "2026-08-14 15:55:42,199864 WARN agent - OS session lock request failed: 4",
        ]
        historical.extend(
            f"2026-08-14 18:{index // 60:02d}:{index % 60:02d},000000 "
            f"[agent] DEBUG New tail record {index}"
            for index in range(2_100)
        )
        path.write_text("\n".join(historical) + "\n")

    def test_status_and_dashboard_routes_still_work(self):
        status = self.client.get("/api/status")
        dashboard = self.client.get("/")

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["servers"][0]["hostname"], "dcv-host")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("DCV Observability", dashboard.text)

    def test_default_session_api_returns_all_bounded_evidence(self):
        response = self.client.get("/api/sessions/sample_user")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["time_filter_active"])
        self.assertTrue(payload["include_unplaced"])
        self.assertIsNone(payload["evidence_window"]["from"])
        self.assertTrue(any(
            event["time_type"] == "unknown"
            for event in payload["evidence_timeline"]
        ))
        self.assertTrue(any(
            "OS session lock request failed" in event["normalized_message"]
            for event in payload["evidence_timeline"]
        ))

    def test_filtered_api_uses_explicit_timezone_and_bracket_overlap(self):
        response = self.client.get(
            "/api/sessions/sample_user",
            params={
                "from": "2026-08-14T10:50:31.510Z",
                "to": "2026-08-14T10:50:31.520Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["time_filter_active"])
        self.assertTrue(any(
            event["time_type"] == "bracketed"
            for event in payload["evidence_timeline"]
        ))
        self.assertFalse(any(
            event["time_type"] == "unknown"
            for event in payload["evidence_timeline"]
        ))

    def test_invalid_session_is_404_and_naive_time_uses_log_timezone(self):
        missing = self.client.get("/api/sessions/not-a-session")
        naive = self.client.get(
            "/api/sessions/sample_user?from=2026-08-14T10:50:00"
        )

        self.assertEqual(missing.status_code, 404)
        self.assertEqual(naive.status_code, 200)
        self.assertEqual(
            naive.json()["time_interpretation"]["interpreted_log_from"],
            "2026-08-14T10:50:00.000+00:00",
        )

    def test_historical_sample_user_api_range_is_not_limited_by_tail(self):
        self.write_historical_sample_user()

        response = self.client.get(
            "/api/sessions/sample_user",
            params={
                "from": "2026-08-14T15:30:00",
                "to": "2026-08-14T16:00:00",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        messages = [
            event["normalized_message"]
            for event in payload["evidence_timeline"]
        ]
        self.assertIn("Client connection closed by peer", messages)
        self.assertIn("OS session lock request failed — exit code 4", messages)
        raw_lines = [event["raw_log_line"] for event in payload["evidence_timeline"]]
        for expected in (
            "Connection closed by the peer",
            "Display channel disconnected",
            "Last client connection '4821' has been closed",
            "Server requested OS session lock",
            "OS session lock request failed: 4",
        ):
            self.assertTrue(any(expected in line for line in raw_lines), expected)
        self.assertEqual(payload["diagnostics"]["read_mode"], "time_range_scan")

    def test_literal_search_scans_history_and_is_case_insensitive(self):
        self.write_historical_sample_user()

        lower = self.client.get(
            "/api/sessions/sample_user",
            params={"q": "connection closed by the peer"},
        )
        mixed = self.client.get(
            "/api/sessions/sample_user",
            params={"q": "Connection Closed By The Peer"},
        )

        self.assertEqual(lower.status_code, 200)
        self.assertEqual(mixed.status_code, 200)
        lower_rows = [
            (event["source_filename"], event["source_line_number"])
            for event in lower.json()["evidence_timeline"]
        ]
        mixed_rows = [
            (event["source_filename"], event["source_line_number"])
            for event in mixed.json()["evidence_timeline"]
        ]
        self.assertEqual(lower_rows, mixed_rows)
        self.assertTrue(any(
            "Connection closed by the peer" in event["raw_log_line"]
            for event in lower.json()["evidence_timeline"]
        ))
        diagnostics = lower.json()["diagnostics"]
        self.assertEqual(diagnostics["read_mode"], "search_scan")
        self.assertEqual(diagnostics["search_query"], "connection closed by the peer")
        self.assertIn("agent.sample_user.sample_user.log", diagnostics["files_searched"])
        self.assertGreaterEqual(diagnostics["matching_lines"], 1)

    def test_search_finds_connection_id_and_contextual_lock_evidence(self):
        self.write_historical_sample_user()

        connection = self.client.get(
            "/api/sessions/sample_user", params={"q": "4821"}
        ).json()
        lock = self.client.get(
            "/api/sessions/sample_user", params={"q": "lock"}
        ).json()

        self.assertTrue(any(
            "4821" in event["raw_log_line"]
            for event in connection["evidence_timeline"]
        ))
        unknown = next(
            event for event in lock["evidence_timeline"]
            if "Unknown command 'lock'" in event["raw_log_line"]
        )
        self.assertEqual(unknown["time_type"], "bracketed")
        self.assertEqual(unknown["source_filename"], "agent.sample_user.sample_user.log")
        self.assertTrue(unknown["context_before"])
        self.assertTrue(unknown["context_after"])

    def test_search_and_time_range_compose_and_no_match_is_clean(self):
        self.write_historical_sample_user()
        params = {
            "from": "2026-08-14T15:30:00",
            "to": "2026-08-14T16:00:00",
            "q": "Connection closed by the peer",
        }

        response = self.client.get("/api/sessions/sample_user", params=params)
        missing = self.client.get(
            "/api/sessions/sample_user",
            params={"q": "THIS_STRING_DOES_NOT_EXIST"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["evidence_timeline"]), 1)
        self.assertEqual(
            payload["evidence_timeline"][0]["timestamp"],
            "2026-08-14T15:55:42.162",
        )
        self.assertEqual(payload["search_match_count"], 1)
        self.assertEqual(payload["diagnostics"]["returned_events"], 1)
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(missing.json()["evidence_timeline"], [])
        self.assertEqual(missing.json()["search_match_count"], 0)

    def test_session_page_has_no_default_time_query(self):
        response = self.client.get("/sessions/legacy_session")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Time range: All available logs", response.text)
        self.assertIn("id='fromInput'", response.text)
        self.assertIn("id='toInput'", response.text)
        self.assertIn("loadEvidence();", response.text)
        self.assertNotIn("setQuick(1440,false)", response.text)

    def test_same_day_and_cross_day_utc_ranges_are_preserved(self):
        ranges = (
            ("2026-08-14T15:50:00.000Z", "2026-08-14T16:00:00.000Z"),
            ("2026-08-14T00:00:00.000Z", "2026-08-14T23:59:59.000Z"),
            ("2026-08-13T23:59:00.000Z", "2026-08-14T00:00:00.000Z"),
            ("2026-08-13T11:00:00.000Z", "2026-08-14T11:00:00.000Z"),
        )
        for start, end in ranges:
            with self.subTest(start=start, end=end):
                response = self.client.get(
                    "/api/sessions/sample_user",
                    params={"from": start, "to": end},
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(
                    payload["time_interpretation"]["interpreted_log_from"],
                    start.replace("Z", "+00:00"),
                )
                self.assertEqual(
                    payload["time_interpretation"]["interpreted_log_to"],
                    end.replace("Z", "+00:00"),
                )
                self.assertEqual(payload["time_interpretation"]["log_timezone"], "UTC")


if __name__ == "__main__":
    unittest.main()
