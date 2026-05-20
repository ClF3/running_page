from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import polyline

    from utils import make_activities_file
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(
        f"optional GPX pipeline dependency is not installed: {exc.name}"
    ) from exc

from tests.helpers import sample_gpx


def _load_tracks_serial(file_names, load_func, activity_title_dict=None):
    activity_title_dict = activity_title_dict or {}
    return {
        file_name: load_func(file_name, activity_title_dict) for file_name in file_names
    }


class GpxPipelineIntegrationTest(unittest.TestCase):
    def test_gpx_file_generates_sqlite_row_and_frontend_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gpx_dir = root / "gpx"
            gpx_dir.mkdir()
            (gpx_dir / "unit_pipeline_sample.gpx").write_text(
                sample_gpx("Integration Run"),
                encoding="utf-8",
            )
            db_path = root / "data.db"
            json_path = root / "activities.json"
            chunks_dir = root / "activity_chunks"

            with mock.patch(
                "gpxtrackposter.track_loader.load_synced_file_list",
                return_value=[],
            ):
                with mock.patch("generator.save_synced_data_file_list"):
                    with mock.patch("generator.IGNORE_BEFORE_SAVING", True):
                        with mock.patch(
                            "gpxtrackposter.track_loader.TrackLoader._load_data_tracks",
                            side_effect=_load_tracks_serial,
                        ):
                            with contextlib.redirect_stdout(io.StringIO()):
                                make_activities_file(db_path, gpx_dir, json_path)

            activities = json.loads(json_path.read_text(encoding="utf-8"))
            manifest = json.loads(
                (chunks_dir / "manifest.json").read_text(encoding="utf-8")
            )
            chunk_exists = (chunks_dir / manifest["years"][0]["file"]).exists()
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "select run_id, name, distance, type, summary_polyline "
                    "from activities"
                ).fetchall()

        self.assertEqual(len(activities), 1)
        self.assertEqual(manifest["total_count"], 1)
        self.assertEqual(len(manifest["years"]), 1)
        self.assertEqual(manifest["years"][0]["count"], 1)
        self.assertTrue(chunk_exists)
        self.assertEqual(len(rows), 1)
        activity = activities[0]
        self.assertEqual(activity["name"], "Integration Run")
        self.assertEqual(activity["type"], "Run")
        self.assertGreater(activity["distance"], 100)
        self.assertEqual(activity["run_id"], rows[0][0])
        self.assertEqual(activity["summary_polyline"], rows[0][4])
        self.assertGreaterEqual(len(polyline.decode(activity["summary_polyline"])), 2)


if __name__ == "__main__":
    unittest.main()
