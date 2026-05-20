from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIVITIES_JSON = ROOT / "src" / "static" / "activities.json"


class GeneratedActivitiesContractTest(unittest.TestCase):
    def setUp(self) -> None:
        if not ACTIVITIES_JSON.exists():
            self.skipTest("generated activities.json is not present")
        self.activities = json.loads(ACTIVITIES_JSON.read_text(encoding="utf-8"))

    def test_activities_json_is_a_non_empty_list_with_unique_run_ids(self) -> None:
        self.assertIsInstance(self.activities, list)
        self.assertGreater(len(self.activities), 0)

        run_ids = [item["run_id"] for item in self.activities]
        self.assertEqual(len(run_ids), len(set(run_ids)))

    def test_activity_records_match_frontend_contract(self) -> None:
        required_keys = {
            "run_id",
            "name",
            "distance",
            "moving_time",
            "type",
            "subtype",
            "start_date",
            "start_date_local",
            "location_country",
            "summary_polyline",
            "average_heartrate",
            "average_speed",
            "elevation_gain",
            "streak",
        }

        for item in self.activities:
            self.assertTrue(required_keys.issubset(item.keys()), item)
            self.assertIsInstance(item["run_id"], int)
            self.assertIsInstance(item["name"], str)
            self.assertIsInstance(item["distance"], (int, float))
            self.assertGreaterEqual(item["distance"], 0)
            self.assertIsInstance(item["moving_time"], str)
            self.assertIsInstance(item["type"], str)
            self.assertIsInstance(item["start_date"], str)
            self.assertIsInstance(item["start_date_local"], str)
            self.assertIsInstance(item["average_speed"], (int, float))
            self.assertIsInstance(item["streak"], int)
            self.assertGreaterEqual(item["streak"], 1)
            self.assertTrue(
                item["summary_polyline"] is None
                or isinstance(item["summary_polyline"], str)
            )
            datetime.strptime(item["start_date_local"], "%Y-%m-%d %H:%M:%S")

    def test_records_are_sorted_by_local_start_time(self) -> None:
        start_times = [item["start_date_local"] for item in self.activities]
        self.assertEqual(start_times, sorted(start_times))


if __name__ == "__main__":
    unittest.main()
