from __future__ import annotations

import tempfile
import unittest
from io import StringIO
from datetime import datetime, timezone
from pathlib import Path

import app.dcv_logs as dcv_logs


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class DcvLogEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.log_dir = Path(self.temp_dir.name)
        self.original_log_dir = dcv_logs.DCV_LOG_DIR
        dcv_logs.DCV_LOG_DIR = self.log_dir
        self.addCleanup(setattr, dcv_logs, "DCV_LOG_DIR", self.original_log_dir)

        (self.log_dir / "agent.other_user.other_user.log").write_text(
            "2026-08-14 09:00:00.100 [agent-controller] INFO Other user event\n"
        )
        (self.log_dir / "agent.sample_user.sample_user.log").write_text(
            "2026-08-14 10:50:31.500 [agent-controller] INFO Server requested OS session lock\n"
            "Screen lock application: /usr/libexec/dcv/dcvscreenlock\n"
            "ERROR: Unknown command 'lock'\n"
            "OS session lock request forwarded\n"
            "2026-08-14 10:50:31.530 [agent-controller] WARN OS session lock request failed: 4\n"
        )
        (self.log_dir / "dcv-xsession.sample_user.sample_user_session.log").write_text(
            "ERROR: Unknown command 'lock'\n"
        )
        (self.log_dir / "Xdcv.legacy_user.legacy_session.log").write_text(
            "2026-08-14 10:49:00.250 [Xdcv] WARNING Legacy session event\n"
        )
        (self.log_dir / "agent.legacy_user.legacy_user.log").write_text(
            "2026-08-14 10:49:01.250 [agent] INFO Historical owner-name variant\n"
        )
        (self.log_dir / "agent.other.sample_user.log").write_text(
            "2026-08-14 10:50:31.510 ERROR FOREIGN-CONTEXT\n"
        )

    def collect_sample_user(self, include_unplaced: bool = False):
        return dcv_logs.collect_session_logs(
            "sample_user_session",
            "sample_user",
            utc(2026, 8, 13, 11, 0),
            utc(2026, 8, 14, 11, 0),
            include_unplaced,
        )

    def test_timezone_less_dcv_timestamp_is_explicitly_utc(self):
        event = dcv_logs.parse_log_line(
            "2026-08-14 10:50:31.530 INFO evidence",
            "agent.sample_user.sample_user.log",
            0,
        )
        self.assertEqual(
            event["_timestamp_epoch"],
            utc(2026, 8, 14, 10, 50, 31, 530000).timestamp(),
        )

    def test_owner_scoped_discovery_variants_are_independent(self):
        other_user, _ = dcv_logs.discover_session_files("other_user", "other_user")
        sample_user, _ = dcv_logs.discover_session_files(
            "sample_user", "sample_user_session"
        )
        legacy, _ = dcv_logs.discover_session_files(
            "legacy_user", "legacy_session"
        )

        self.assertEqual(other_user, ["agent.other_user.other_user.log"])
        self.assertEqual(sample_user, [
            "agent.sample_user.sample_user.log",
            "dcv-xsession.sample_user.sample_user_session.log",
        ])
        self.assertEqual(legacy, [
            "Xdcv.legacy_user.legacy_session.log",
            "agent.legacy_user.legacy_user.log",
        ])
        self.assertNotIn("agent.other.sample_user.log", sample_user)

    def test_search_reads_only_current_owner_session_files(self):
        result = dcv_logs.collect_session_logs(
            "sample_user_session", "sample_user", search_query="FOREIGN-CONTEXT"
        )

        self.assertEqual(result["events"], [])
        self.assertNotIn(
            "agent.other.sample_user.log", result["diagnostics"]["files_searched"]
        )

    def test_broad_window_returns_known_sample_user_lock_failure(self):
        result = self.collect_sample_user()
        lock_failure = next(
            event for event in result["events"]
            if "OS session lock request failed" in event["normalized_message"]
        )
        self.assertEqual(lock_failure["timestamp"], "2026-08-14T10:50:31.530")
        self.assertEqual(lock_failure["time_type"], "exact")
        self.assertEqual(result["earliest_timestamp"], "2026-08-14T10:50:31.500")
        self.assertEqual(result["latest_timestamp"], "2026-08-14T10:50:31.530")
        self.assertEqual(result["diagnostics"]["files_read"], [
            "agent.sample_user.sample_user.log",
            "dcv-xsession.sample_user.sample_user_session.log",
        ])
        self.assertEqual(result["diagnostics"]["lines_parsed"], 6)
        self.assertEqual(result["diagnostics"]["timestamped_lines"], 2)
        self.assertEqual(result["diagnostics"]["untimestamped_lines"], 4)
        self.assertEqual(
            result["diagnostics"]["timestamped_lines_inside_window"], 2
        )

    def test_narrow_1045_to_1055_window_returns_known_event(self):
        result = dcv_logs.collect_session_logs(
            "sample_user_session",
            "sample_user",
            utc(2026, 8, 14, 10, 45),
            utc(2026, 8, 14, 10, 55),
        )
        messages = [event["normalized_message"] for event in result["events"]]
        self.assertTrue(any("OS session lock request failed" in item for item in messages))

    def test_untimestamped_lock_error_is_bracketed_not_exact(self):
        result = self.collect_sample_user()
        lock_error = next(
            event for event in result["events"]
            if event["normalized_message"] == "Unknown command 'lock'"
        )
        self.assertEqual(lock_error["time_type"], "bracketed")
        self.assertEqual(lock_error["time_from"], "2026-08-14T10:50:31.500")
        self.assertEqual(lock_error["time_to"], "2026-08-14T10:50:31.530")
        self.assertIsNone(lock_error["timestamp"])

    def test_bracketed_event_is_included_when_bracket_overlaps_window(self):
        result = dcv_logs.collect_session_logs(
            "sample_user_session",
            "sample_user",
            utc(2026, 8, 14, 10, 50, 31, 515000),
            utc(2026, 8, 14, 10, 50, 31, 520000),
        )
        messages = [event["normalized_message"] for event in result["events"]]
        self.assertIn("Unknown command 'lock'", messages)
        self.assertFalse(any(event["time_type"] == "exact" for event in result["events"]))
        self.assertEqual(result["diagnostics"]["lines_inside_requested_window"], 3)

    def test_unknown_time_is_never_fabricated_and_can_be_included(self):
        excluded = self.collect_sample_user()
        self.assertEqual(excluded["unplaced_evidence_count"], 1)
        self.assertFalse(any(
            event["source_filename"].startswith("dcv-xsession")
            for event in excluded["events"]
        ))

        included = self.collect_sample_user(include_unplaced=True)
        unknown = next(
            event for event in included["events"]
            if event["source_filename"].startswith("dcv-xsession")
        )
        self.assertEqual(unknown["time_type"], "unknown")
        self.assertIsNone(unknown["timestamp"])
        self.assertIsNone(unknown["time_from"])
        self.assertIsNone(unknown["time_to"])
        self.assertIsNone(unknown["approximate_time"])

    def test_default_all_available_logs_includes_placed_and_unplaced_evidence(self):
        result = dcv_logs.collect_session_logs("sample_user_session", "sample_user")

        self.assertFalse(result["time_filter_active"])
        self.assertTrue(result["include_unplaced"])
        self.assertEqual(result["diagnostics"]["lines_inside_requested_window"], 6)
        self.assertTrue(any(event["time_type"] == "exact" for event in result["events"]))
        self.assertTrue(any(event["time_type"] == "bracketed" for event in result["events"]))
        self.assertTrue(any(event["time_type"] == "unknown" for event in result["events"]))

    def test_explicit_window_excludes_unknown_until_requested(self):
        result = self.collect_sample_user()

        self.assertTrue(result["time_filter_active"])
        self.assertFalse(result["include_unplaced"])
        self.assertFalse(any(event["time_type"] == "unknown" for event in result["events"]))

    def test_near_time_uses_one_close_source_neighbor(self):
        (self.log_dir / "agent.near.near.log").write_text(
            "2026-08-14 10:50:31.500 INFO preceding record\n"
            "ERROR one-sided contextual evidence\n"
        )
        result = dcv_logs.collect_session_logs(
            "near", "near", utc(2026, 8, 14, 10, 50), utc(2026, 8, 14, 10, 51)
        )
        near = next(event for event in result["events"] if event["severity"] == "ERROR")
        self.assertEqual(near["time_type"], "near")
        self.assertEqual(near["approximate_time"], "2026-08-14T10:50:31.500")
        self.assertIsNone(near["timestamp"])

    def test_context_is_limited_to_the_same_source_file(self):
        result = self.collect_sample_user()
        lock_error = next(
            event for event in result["events"]
            if event["normalized_message"] == "Unknown command 'lock'"
        )
        context_text = "\n".join(
            line["text"]
            for line in lock_error["context_before"] + lock_error["context_after"]
        )
        self.assertIn("Server requested OS session lock", context_text)
        self.assertIn("OS session lock request failed: 4", context_text)
        self.assertNotIn("FOREIGN-CONTEXT", context_text)

    def test_historical_range_is_scanned_before_return_cap(self):
        historical = [
            "2026-08-14 15:29:59,900000 [agent] INFO Before requested window",
            "2026-08-14 15:55:42,162397 WARN agent - Failed to receive message "
            "from client: Connection closed by the peer",
            "2026-08-14 15:55:42,162434 INFO agent - Display channel disconnected",
            "2026-08-14 15:55:42,166851 INFO agent - Last client connection '4821' has been closed",
            "2026-08-14 15:55:42,167148 INFO agent - Server requested OS session lock",
            "2026-08-14 15:55:42,199864 WARN agent - OS session lock request failed: 4",
            "2026-08-14 16:01:00,000000 [agent] DEBUG After requested window",
            "2026-08-14 16:01:01,000000 [agent] DEBUG Confirm chronological boundary",
        ]
        historical.extend(
            f"2026-08-14 18:{index // 60:02d}:{index % 60:02d},000000 "
            f"[agent] DEBUG New tail record {index}"
            for index in range(2_100)
        )
        (self.log_dir / "agent.sample_user.sample_user.log").write_text(
            "\n".join(historical) + "\n"
        )

        tail_result = dcv_logs.collect_session_logs(
            "sample_user_session", "sample_user"
        )
        self.assertFalse(any(
            "Connection closed by peer" in event["normalized_message"]
            for event in tail_result["events"]
        ))
        self.assertEqual(tail_result["diagnostics"]["read_mode"], "tail")

        result = dcv_logs.collect_session_logs(
            "sample_user_session",
            "sample_user",
            utc(2026, 8, 14, 15, 30),
            utc(2026, 8, 14, 16, 0),
        )
        messages = [event["normalized_message"] for event in result["events"]]
        self.assertIn("Client connection closed by peer", messages)
        self.assertIn("Display channel disconnected", messages)
        self.assertIn("Last client connection closed", messages)
        self.assertIn("OS session lock requested or forwarded", messages)
        self.assertIn("OS session lock request failed — exit code 4", messages)
        disconnect = next(
            event for event in result["events"]
            if event["normalized_message"] == "Client connection closed by peer"
        )
        self.assertEqual(disconnect["timestamp"], "2026-08-14T15:55:42.162")
        self.assertEqual(disconnect["source_line_number"], 2)
        self.assertIn("15:55:42,162397", disconnect["raw_log_line"])
        self.assertEqual(
            disconnect["source_filename"], "agent.sample_user.sample_user.log"
        )
        correlation = result["disconnect_correlations"][0]
        self.assertTrue(correlation["display_channel_closed"])
        self.assertTrue(correlation["session_closed"])
        self.assertEqual(correlation["lock_order"], "after")
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["read_mode"], "time_range_scan")
        self.assertEqual(diagnostics["files_scanned"], [
            "agent.sample_user.sample_user.log",
            "dcv-xsession.sample_user.sample_user_session.log",
        ])
        self.assertEqual(diagnostics["matching_timestamped_lines"], 5)
        self.assertEqual(diagnostics["returned_events"], 5)
        self.assertLess(diagnostics["scan_lines_read"], len(historical))

    def test_protected_range_file_uses_central_sudo_stream_fallback(self):
        filename = "agent.protected.protected.log"
        (self.log_dir / filename).write_text("protected fixture placeholder\n")
        source = (
            "2026-08-14 15:55:42,162397 WARN agent - Failed to receive "
            "message from client: Connection closed by the peer\n"
            "2026-08-14 15:55:42,199864 WARN agent - "
            "OS session lock request failed: 4\n"
        )
        original = dcv_logs._consume_log_command
        calls = []

        def protected_reader(command, consumer):
            calls.append(command)
            if command[0] != "sudo":
                return None, False, "[Errno 1] Operation not permitted"
            payload, _ = consumer(StringIO(source))
            return payload, True, ""

        dcv_logs._consume_log_command = protected_reader
        self.addCleanup(setattr, dcv_logs, "_consume_log_command", original)

        result = dcv_logs.collect_session_logs(
            "protected",
            "protected",
            utc(2026, 8, 14, 15, 30),
            utc(2026, 8, 14, 16, 0),
        )

        self.assertIn(filename, result["diagnostics"]["files_read"])
        self.assertIn(filename, result["diagnostics"]["files_scanned"])
        self.assertEqual(result["diagnostics"]["file_access"], [{
            "filename": filename,
            "read_method": "sudo",
            "status": "read",
            "error": None,
        }])
        self.assertFalse(result["log_errors"])
        self.assertTrue(any(command[0] == "sudo" for command in calls))
        self.assertTrue(any(
            command[:4] == ["sudo", "-n", "/usr/bin/tail", "-n"]
            and "+1" in command
            for command in calls
        ))
        self.assertTrue(any(
            event["normalized_message"] == "Client connection closed by peer"
            for event in result["events"]
        ))

        tail_result = dcv_logs.collect_session_logs("protected", "protected")
        self.assertEqual(tail_result["diagnostics"]["read_mode"], "tail")
        self.assertEqual(
            tail_result["diagnostics"]["file_access"][0]["read_method"],
            "sudo",
        )

    def test_protected_search_uses_central_sudo_stream_fallback(self):
        filename = "agent.protectedsearch.protectedsearch.log"
        (self.log_dir / filename).write_text("protected fixture placeholder\n")
        source = (
            "2026-08-14 15:55:42,162397 WARN agent - Connection closed by the peer\n"
        )
        original = dcv_logs._consume_log_command
        calls = []

        def protected_reader(command, consumer):
            calls.append(command)
            if command[0] != "sudo":
                return None, False, "[Errno 1] Operation not permitted"
            payload, _ = consumer(StringIO(source))
            return payload, True, ""

        dcv_logs._consume_log_command = protected_reader
        self.addCleanup(setattr, dcv_logs, "_consume_log_command", original)

        result = dcv_logs.collect_session_logs(
            "protectedsearch",
            "protectedsearch",
            search_query="connection closed by the peer",
        )

        self.assertEqual(result["diagnostics"]["files_searched"], [filename])
        self.assertEqual(
            result["diagnostics"]["file_access"][0]["read_method"], "sudo"
        )
        self.assertTrue(any(command[0] == "sudo" for command in calls))
        self.assertEqual(result["search_match_count"], 1)

    def test_time_range_caps_returned_matches_after_scanning(self):
        (self.log_dir / "agent.cap.cap.log").write_text("\n".join(
            f"2026-08-14 15:40:00,{index:06d} [agent] DEBUG matching {index}"
            for index in range(2_105)
        ) + "\n")

        result = dcv_logs.collect_session_logs(
            "cap",
            "cap",
            utc(2026, 8, 14, 15, 30),
            utc(2026, 8, 14, 16, 0),
        )

        self.assertEqual(result["diagnostics"]["matching_timestamped_lines"], 2_105)
        self.assertEqual(result["diagnostics"]["returned_events"], 2_000)
        self.assertEqual(len(result["events"]), 2_000)
        self.assertEqual(result["severity_counts"]["DEBUG"], 2_105)


if __name__ == "__main__":
    unittest.main()
