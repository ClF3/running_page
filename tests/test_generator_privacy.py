from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import polyline
    import polyline_processor

    import generator as generator_module
    from generator import Generator
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(
        f"optional generator dependency is not installed: {exc.name}"
    ) from exc


class FakeActivity(SimpleNamespace):
    def __contains__(self, key):
        return hasattr(self, key)


def fake_activity(
    *,
    run_id: int = 1001,
    distance: float = 500,
    subtype: str = "",
    summary_polyline: str = "",
) -> FakeActivity:
    return FakeActivity(
        id=run_id,
        name="Privacy Test Run",
        distance=distance,
        moving_time=dt.timedelta(seconds=180),
        elapsed_time=dt.timedelta(seconds=200),
        type="Run",
        subtype=subtype,
        start_date="2026-01-01 00:00:00",
        start_date_local="2026-01-01 08:00:00",
        location_country="北京市, 中国",
        average_heartrate=None,
        average_speed=3.0,
        elevation_gain=0,
        start_latlng=SimpleNamespace(lat=39.9, lon=116.3),
        map=SimpleNamespace(summary_polyline=summary_polyline),
    )


def fetch_route_and_subtype(db_path: Path, run_id: int = 1001) -> tuple[str, str]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "select coalesce(summary_polyline, ''), coalesce(subtype, '') "
            "from activities where run_id = ?",
            (run_id,),
        ).fetchone()


class GeneratorPrivacyTest(unittest.TestCase):
    def test_load_does_not_crop_or_persist_summary_polyline(self) -> None:
        route = polyline.encode([(0.0, 0.0), (0.0, 0.001), (0.0, 0.002), (0.0, 0.003)])

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            generator = Generator(db_path)
            generator.sync_from_app([fake_activity(summary_polyline=route)])

            with mock.patch.object(polyline_processor, "IGNORE_START_END_RANGE", 0.1):
                activities = generator.load()

            stored_route, _ = fetch_route_and_subtype(db_path)

        self.assertEqual(stored_route, route)
        self.assertEqual(activities[0]["summary_polyline"], route)

    def test_load_marks_no_route_indoor_only_in_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data.db"
            generator = Generator(db_path)
            generator.sync_from_app([fake_activity(distance=150, summary_polyline="")])

            activities = generator.load()
            stored_route, stored_subtype = fetch_route_and_subtype(db_path)

        self.assertEqual(stored_route, "")
        self.assertEqual(stored_subtype, "")
        self.assertEqual(activities[0]["subtype"], "indoor")

    def test_privacy_filter_runs_once_before_saving(self) -> None:
        route = polyline.encode(
            [
                (0.0, 0.0),
                (0.0, 0.001),
                (0.0, 0.002),
                (0.0, 0.003),
                (0.0, 0.004),
            ]
        )

        with mock.patch.object(polyline_processor, "IGNORE_START_END_RANGE", 0.1):
            with mock.patch.object(polyline_processor, "IGNORE_POLYLINE", []):
                with mock.patch.object(polyline_processor, "IGNORE_RANGE", 0):
                    expected_once = polyline_processor.filter_out(route)

                    with tempfile.TemporaryDirectory() as tmp:
                        db_path = Path(tmp) / "data.db"
                        generator = Generator(db_path)
                        with mock.patch.object(
                            generator_module, "IGNORE_BEFORE_SAVING", True
                        ):
                            generator.sync_from_app(
                                [fake_activity(summary_polyline=route)]
                            )
                            stored_after_import, _ = fetch_route_and_subtype(db_path)
                            activities = generator.load()
                            stored_after_load, _ = fetch_route_and_subtype(db_path)

        self.assertNotEqual(expected_once, route)
        self.assertEqual(stored_after_import, expected_once)
        self.assertEqual(stored_after_load, expected_once)
        self.assertEqual(activities[0]["summary_polyline"], expected_once)


if __name__ == "__main__":
    unittest.main()
