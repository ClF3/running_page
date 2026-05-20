from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

try:
    from utils import write_activities_files
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(
        f"optional activity chunk dependency is not installed: {exc.name}"
    ) from exc


def _activity(run_id: int, start_date_local: str) -> dict:
    return {
        "run_id": run_id,
        "name": f"Run {run_id}",
        "distance": 1000,
        "moving_time": "00:05:00",
        "type": "Run",
        "subtype": "generic",
        "start_date": start_date_local,
        "start_date_local": start_date_local,
        "location_country": "中国",
        "summary_polyline": "abc",
        "average_heartrate": None,
        "average_speed": 3.33,
        "elevation_gain": 10,
        "streak": 1,
    }


class ActivityChunkWriterTest(unittest.TestCase):
    def test_write_activities_files_writes_full_json_manifest_and_year_chunks(
        self,
    ) -> None:
        activities = [
            _activity(1, "2025-01-01 08:00:00"),
            _activity(2, "2025-12-31 18:00:00"),
            _activity(3, "2026-01-01 07:00:00"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "activities.json"
            chunks_dir = Path(tmp) / "activity_chunks"
            chunks_dir.mkdir()
            stale_chunk = chunks_dir / "year_2024.json"
            stale_chunk.write_text("[]", encoding="utf-8")

            write_activities_files(activities, json_path)

            manifest = json.loads(
                (chunks_dir / "manifest.json").read_text(encoding="utf-8")
            )
            year_2025 = json.loads(
                (chunks_dir / "year_2025.json").read_text(encoding="utf-8")
            )
            year_2026 = json.loads(
                (chunks_dir / "year_2026.json").read_text(encoding="utf-8")
            )
            full_json = json.loads(json_path.read_text(encoding="utf-8"))
            stale_chunk_exists = stale_chunk.exists()

        self.assertEqual(full_json, activities)
        self.assertFalse(stale_chunk_exists)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["total_count"], 3)
        self.assertEqual([item["year"] for item in manifest["years"]], ["2026", "2025"])
        self.assertEqual(year_2026, [activities[2]])
        self.assertEqual(year_2025, activities[:2])

        year_2025_manifest = manifest["years"][1]
        self.assertEqual(year_2025_manifest["file"], "year_2025.json")
        self.assertEqual(year_2025_manifest["count"], 2)
        self.assertEqual(year_2025_manifest["run_ids"], [1, 2])
        self.assertEqual(
            year_2025_manifest["first_start_date_local"], "2025-01-01 08:00:00"
        )
        self.assertEqual(
            year_2025_manifest["last_start_date_local"], "2025-12-31 18:00:00"
        )


if __name__ == "__main__":
    unittest.main()
