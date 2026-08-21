from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main


def collected_server() -> dict:
    return {
        "name": "dcv-host",
        "hostname": "dcv-host",
        "sessions": [{
            "id": "session-1",
            "owner": "owner",
            "type": "virtual",
            "state": "running",
        }],
    }


class BackgroundStartupCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._cache = {
            "state": "initializing",
            "collection_state": "initializing",
            "servers": [],
            "collected_at": None,
            "collection_started_at": None,
            "last_collection_duration_ms": None,
            "collection_error": None,
        }
        main._collection_lock = asyncio.Lock()
        main._collection_started_monotonic = None
        main._startup_collection_task = None

    async def wait_for_thread(self, event: threading.Event) -> None:
        for _ in range(200):
            if event.is_set():
                return
            await asyncio.sleep(0.005)
        self.fail("collector thread did not start")

    async def test_startup_does_not_wait_for_simulated_60_second_collector(self):
        collector_started = threading.Event()
        release_collector = threading.Event()

        def slow_collector():
            collector_started.set()
            release_collector.wait(timeout=60)
            return collected_server()

        with (
            patch.object(main, "collect_local", side_effect=slow_collector),
            patch.object(
                main,
                "collect_recent_session_issues",
                return_value={"count": 0, "available": True, "scope": "Past 24h"},
            ),
        ):
            started = time.monotonic()
            await asyncio.wait_for(main.startup(), timeout=0.25)
            self.assertLess(time.monotonic() - started, 0.25)
            await self.wait_for_thread(collector_started)

            status_started = time.monotonic()
            payload = await main.status()
            self.assertLess(time.monotonic() - status_started, 0.1)
            self.assertEqual(payload["collection_state"], "collecting")
            self.assertEqual(payload["state"], "collecting")
            self.assertEqual(payload["servers"], [])
            self.assertIsNone(payload["collected_at"])
            self.assertIsNotNone(payload["collection_started_at"])
            self.assertIsNotNone(payload["collection_elapsed_ms"])

            release_collector.set()
            await asyncio.wait_for(main._startup_collection_task, timeout=1)

        payload = await main.status()
        self.assertEqual(payload["collection_state"], "ready")
        self.assertEqual(payload["servers"][0]["hostname"], "dcv-host")
        self.assertIsNotNone(payload["collected_at"])
        self.assertIsNotNone(payload["last_collection_duration_ms"])
        server, session = main.find_session("session-1")
        self.assertEqual(server["hostname"], "dcv-host")
        self.assertEqual(session["owner"], "owner")

    async def test_collector_exception_leaves_application_in_error_state(self):
        with patch.object(main, "collect_local", side_effect=RuntimeError("dcv unavailable")):
            payload = await main.refresh_all()

        self.assertEqual(payload["collection_state"], "error")
        self.assertEqual(payload["servers"], [])
        self.assertIn("RuntimeError", payload["collection_error"])
        self.assertIn("dcv unavailable", payload["collection_error"])
        self.assertEqual((await main.status())["collection_state"], "error")

    async def test_manual_refresh_retries_after_failure(self):
        calls = 0

        def fail_then_succeed():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary failure")
            return collected_server()

        with (
            patch.object(main, "collect_local", side_effect=fail_then_succeed),
            patch.object(main, "collect_recent_session_issues", return_value={}),
        ):
            failed = await main.refresh()
            retried = await main.refresh()

        self.assertEqual(failed["collection_state"], "error")
        self.assertEqual(retried["collection_state"], "ready")
        self.assertEqual(retried["servers"][0]["hostname"], "dcv-host")
        self.assertEqual(calls, 2)

    async def test_two_collection_jobs_cannot_run_concurrently(self):
        collector_started = threading.Event()
        release_collector = threading.Event()
        calls = 0

        def blocked_collector():
            nonlocal calls
            calls += 1
            collector_started.set()
            release_collector.wait(timeout=2)
            return collected_server()

        with (
            patch.object(main, "collect_local", side_effect=blocked_collector),
            patch.object(main, "collect_recent_session_issues", return_value={}),
        ):
            first = asyncio.create_task(main.refresh_all())
            await self.wait_for_thread(collector_started)
            second = await asyncio.wait_for(main.refresh(), timeout=0.1)
            self.assertEqual(second["collection_request"], "already_in_progress")
            self.assertEqual(second["collection_state"], "collecting")
            self.assertEqual(calls, 1)
            release_collector.set()
            await asyncio.wait_for(first, timeout=1)

        self.assertEqual(calls, 1)
        self.assertEqual((await main.status())["collection_state"], "ready")


class DashboardCollectionStateTests(unittest.TestCase):
    def test_dashboard_handles_initial_collection_without_zero_state(self):
        self.assertIn("Collecting initial DCV data...", main.DASHBOARD)
        self.assertIn("collectionState==='initializing'", main.DASHBOARD)
        self.assertIn("collectionState==='collecting'", main.DASHBOARD)
        self.assertIn("byId('sumSessions').textContent='—'", main.DASHBOARD)


class HttpAvailabilityDuringCollectionTests(unittest.TestCase):
    def test_status_route_is_http_200_while_startup_collector_is_blocked(self):
        collector_started = threading.Event()
        release_collector = threading.Event()

        def slow_collector():
            collector_started.set()
            release_collector.wait(timeout=60)
            return collected_server()

        main._cache = {
            "state": "initializing",
            "collection_state": "initializing",
            "servers": [],
            "collected_at": None,
            "collection_started_at": None,
            "last_collection_duration_ms": None,
            "collection_error": None,
        }
        main._collection_lock = asyncio.Lock()
        main._startup_collection_task = None
        with (
            patch.object(main, "collect_local", side_effect=slow_collector),
            patch.object(main, "collect_recent_session_issues", return_value={}),
            TestClient(main.app) as client,
        ):
            self.assertTrue(collector_started.wait(timeout=1))
            started = time.monotonic()
            response = client.get("/api/status")
            self.assertLess(time.monotonic() - started, 0.25)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["collection_state"], "collecting")
            release_collector.set()
            for _ in range(100):
                ready = client.get("/api/status").json()
                if ready["collection_state"] == "ready":
                    break
                time.sleep(0.01)
            else:
                self.fail("background collection did not complete")


if __name__ == "__main__":
    unittest.main()
