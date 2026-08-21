from __future__ import annotations

import re
import unittest
from pathlib import Path


SOURCE = (Path(__file__).parents[1] / "app" / "main.py").read_text()


class FrontendContractTests(unittest.TestCase):
    def test_session_page_defaults_to_all_available_logs(self):
        self.assertIn("Time range: All available logs", SOURCE)
        self.assertRegex(SOURCE, r"document\.querySelectorAll\('\.quick'\).*loadEvidence\(\);")
        self.assertNotIn("setQuick(1440,false)", SOURCE)

    def test_two_primary_text_fields_replace_the_range_calendar(self):
        for control in ("fromInput", "toInput"):
            self.assertIn(f"id='{control}'", SOURCE)
        for removed in ("rangeTrigger", "rangePopup", "calendarGrid"):
            self.assertNotIn(f"id='{removed}'", SOURCE)
        self.assertIn("id='fromCalendarButton'", SOURCE)
        self.assertIn("id='toCalendarButton'", SOURCE)
        self.assertIn("id='clearButton'", SOURCE)
        self.assertIn("function clearTimeFilter()", SOURCE)

    def test_picker_uses_text_friendly_24_hour_utc_values(self):
        self.assertIn("id='fromInput' class='datetime-input' type='text'", SOURCE)
        self.assertIn("id='toInput' class='datetime-input' type='text'", SOURCE)
        self.assertNotIn("type='datetime-local'", SOURCE)
        self.assertIn("placeholder='YYYY-MM-DD HH:MM:SS'", SOURCE)
        self.assertIn("Log timezone: UTC", SOURCE)
        self.assertIn("function validTime(value)", SOURCE)
        self.assertIn("function parseLogDateTime(value)", SOURCE)
        self.assertIn("T${match[2]}.000Z", SOURCE)

    def test_validation_is_specific_to_each_field(self):
        for message in (
            "From is required",
            "To is required",
            "Use YYYY-MM-DD HH:MM:SS",
            "Invalid date",
            "Invalid time",
            "To must be at or after From",
        ):
            self.assertIn(message, SOURCE)

    def test_page_contains_only_factual_evidence_language(self):
        for removed in (
            "Session Health Assessment",
            "Confidence",
            "Suspected issue",
            "No recognized signature",
            "Raw DCV Logs",
        ):
            self.assertNotIn(removed, SOURCE)
        self.assertIn("No matching evidence is currently displayed.", SOURCE)

    def test_default_filters_exclude_only_debug(self):
        self.assertIn(
            "const enabled=new Set(filterKeys.filter(key=>key!=='DEBUG'))",
            SOURCE,
        )
        self.assertIn(
            "if(event.severity==='DEBUG'&&!enabled.has('DEBUG'))return false",
            SOURCE,
        )

    def test_evidence_and_dashboard_sort_defaults(self):
        self.assertIn("sessionSortKey='id',sessionSortDirection='asc'", SOURCE)
        self.assertIn("evidenceSortKey='time',evidenceSortDirection='desc'", SOURCE)
        self.assertIn("if(av===null)return 1;if(bv===null)return-1", SOURCE)
        self.assertEqual(len(re.findall(r"sortHeader\('", SOURCE)), 7)
        self.assertEqual(len(re.findall(r"evidenceHeader\('", SOURCE)), 5)

    def test_refresh_does_not_reset_view_state(self):
        refresh_assignment = re.search(
            r"byId\('refreshButton'\)\.onclick=([^;]+);", SOURCE
        )
        self.assertIsNotNone(refresh_assignment)
        self.assertEqual(refresh_assignment.group(1), "loadEvidence")
        self.assertEqual(SOURCE.count("fetch('/api/refresh',{method:'POST'})"), 1)

    def test_log_search_is_compact_and_preserves_existing_view_state(self):
        self.assertIn("id='searchInput' class='search-input' type='text'", SOURCE)
        self.assertIn("placeholder='Search log text...'", SOURCE)
        self.assertIn("id='searchButton'", SOURCE)
        self.assertIn("id='clearSearchButton'", SOURCE)
        self.assertIn("if(appliedSearch)params.set('q',appliedSearch)", SOURCE)
        self.assertIn("if(event.key==='Enter'){event.preventDefault();runSearch()}", SOURCE)
        self.assertIn("function clearSearch(){appliedSearch='';", SOURCE)
        self.assertIn("No matching log evidence for this search.", SOURCE)
        self.assertNotIn("appliedRange=null;byId('searchInput')", SOURCE)


if __name__ == "__main__":
    unittest.main()
