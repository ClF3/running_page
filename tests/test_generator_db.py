from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from run_page.generator.db import (
        ACTIVITY_KEYS,
        Activity,
        init_db,
        update_or_create_activity,
    )
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(
        f"optional generator dependency is not installed: {exc.name}"
    ) from exc


def fake_activity(
    *,
    run_id: int = 1001,
    name: str = "Morning Run",
    distance: float = 5000,
    moving_seconds: int = 1500,
    activity_type: str = "Run",
    subtype: str = "generic",
    location_country: str = "道路, 大连市, 辽宁省, 中国",
    polyline: str = "abc",
    average_heartrate: float | None = 150,
    average_speed: float = 3.33,
    elevation_gain: float | None = 12,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        name=name,
        distance=distance,
        moving_time=dt.timedelta(seconds=moving_seconds),
        elapsed_time=dt.timedelta(seconds=moving_seconds + 60),
        type=activity_type,
        subtype=subtype,
        start_date="2026-01-01 00:00:00",
        start_date_local="2026-01-01 08:00:00",
        location_country=location_country,
        average_heartrate=average_heartrate,
        average_speed=average_speed,
        elevation_gain=elevation_gain,
        start_latlng=SimpleNamespace(lat=39.9, lon=116.3),
        map=SimpleNamespace(summary_polyline=polyline),
    )


class GeneratorDbTest(unittest.TestCase):
    def test_init_db_creates_expected_activity_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init_db(Path(tmp) / "data.db")

            column_names = {column.name for column in Activity.__table__.columns}

        self.assertTrue(set(ACTIVITY_KEYS).issubset(column_names))

    def test_update_or_create_activity_creates_row_and_serializes_to_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = init_db(Path(tmp) / "data.db")
            created = update_or_create_activity(session, fake_activity())
            session.commit()

            row = session.query(Activity).filter_by(run_id=1001).one()
            payload = row.to_dict()

        self.assertTrue(created)
        self.assertEqual(payload["run_id"], 1001)
        self.assertEqual(payload["name"], "Morning Run")
        self.assertEqual(payload["moving_time"], "0:25:00")
        self.assertEqual(payload["location_country"], "道路, 大连市, 辽宁省, 中国")
        self.assertEqual(payload["summary_polyline"], "abc")
        self.assertEqual(payload["elevation_gain"], 12.0)

    def test_update_or_create_activity_updates_existing_row_without_duplicate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = init_db(Path(tmp) / "data.db")
            self.assertTrue(update_or_create_activity(session, fake_activity()))
            session.commit()

            updated = update_or_create_activity(
                session,
                fake_activity(
                    name="Evening Run",
                    distance=10_000,
                    moving_seconds=3000,
                    polyline="xyz",
                    average_speed=3.5,
                    elevation_gain=25,
                ),
            )
            session.commit()

            rows = session.query(Activity).filter_by(run_id=1001).all()

        self.assertFalse(updated)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Evening Run")
        self.assertEqual(rows[0].distance, 10_000)
        self.assertEqual(rows[0].summary_polyline, "xyz")
        self.assertEqual(rows[0].elevation_gain, 25.0)


if __name__ == "__main__":
    unittest.main()
