from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import polyline

from generator import Generator
from gpxtrackposter.github_drawer import GithubDrawer
from gpxtrackposter.track_loader import TrackLoader
from tests.test_generator_privacy import fake_activity, fetch_route_and_subtype


class GeneratorRouteTest(unittest.TestCase):
    def test_web_and_svg_use_source_type_independently_of_route(self):
        tiny_route = polyline.encode([(39.9, 116.39), (39.90001, 116.39001)])
        cases = [
            ("running", "", False),
            ("running", tiny_route, False),
            ("indoor_running", "", True),
            ("indoor_running", tiny_route, True),
            ("treadmill_running", "", True),
            ("treadmill_running", tiny_route, True),
            ("treadmill", "", True),
            ("VirtualRun", "", True),
            ("", "", False),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            generator = Generator(db_path)
            for run_id, (subtype, route, _) in enumerate(cases, 1):
                generator.sync_from_app(
                    [
                        fake_activity(
                            run_id=run_id, subtype=subtype, summary_polyline=route
                        )
                    ]
                )
            payload = {a["run_id"]: a for a in generator.load()}
            tracks = {t.run_id: t for t in TrackLoader().load_tracks_from_db(db_path)}
            drawer = GithubDrawer(None)
            for run_id, (subtype, route, indoor) in enumerate(cases, 1):
                with self.subTest(subtype=subtype, route=bool(route)):
                    self.assertEqual(payload[run_id]["subtype"], subtype)
                    self.assertEqual(payload[run_id]["is_indoor"], indoor)
                    self.assertEqual(drawer._has_indoor_track([tracks[run_id]]), indoor)
                    self.assertEqual(payload[run_id]["has_route"], bool(route))
                    self.assertEqual(tracks[run_id].has_route, bool(route))
                    self.assertEqual(payload[run_id]["summary_polyline"], route)
                    self.assertEqual(
                        fetch_route_and_subtype(db_path, run_id), (route, subtype)
                    )
            grid_ids = {
                t.run_id
                for t in TrackLoader().load_tracks_from_db(db_path, is_grid=True)
            }
            self.assertEqual(grid_ids, {2, 4, 6})
            generator.session.close()

    def test_unusable_routes_stay_separate_from_indoor_and_do_not_break_svg(self):
        cases = [
            ("", "missing"),
            ("_", "invalid"),
            (polyline.encode([(91, 10), (91, 11)]), "invalid"),
            (polyline.encode([(39.9, 116.39)]), "insufficient"),
            (polyline.encode([(39.9, 116.39)] * 2), "insufficient"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            generator = Generator(db_path)
            generator.sync_from_app(
                [
                    fake_activity(run_id=i, subtype="running", summary_polyline=route)
                    for i, (route, _) in enumerate(cases, 1)
                ]
            )
            payload = generator.load()
            tracks = TrackLoader().load_tracks_from_db(db_path)
            self.assertEqual(len(tracks), len(cases))
            for activity, (_, expected_status) in zip(payload, cases):
                self.assertFalse(activity["is_indoor"])
                self.assertFalse(activity["has_route"])
                self.assertEqual(activity["route_status"], expected_status)
            self.assertEqual(
                TrackLoader().load_tracks_from_db(db_path, is_grid=True), []
            )
            generator.session.close()


if __name__ == "__main__":
    unittest.main()
