from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from run_page.tui.data import (
    Activity,
    aggregate_activities,
    build_contribution_grid,
    filter_activities,
    load_activities,
    make_city_filter,
    make_period_filter,
    make_search_filter,
    make_type_filter,
    make_year_filter,
)


def activity(
    run_id: int,
    *,
    distance: float = 5000.0,
    moving_time: str = "00:25:00",
    activity_type: str = "Run",
    subtype: str | None = None,
    start_date_local: str = "2026-01-01 08:00:00",
    location_country: str | None = "高能街, 大连市, 辽宁省, 116026, 中国",
    average_heartrate: float | None = 150,
    elevation_gain: float | None = 10,
    average_speed: float = 3.33,
    streak: int = 1,
) -> Activity:
    return Activity(
        run_id=run_id,
        name=f"Run {run_id}",
        distance=distance,
        moving_time=moving_time,
        type=activity_type,
        subtype=subtype,
        start_date=start_date_local,
        start_date_local=start_date_local,
        location_country=location_country,
        summary_polyline="abc",
        average_heartrate=average_heartrate,
        elevation_gain=elevation_gain,
        average_speed=average_speed,
        streak=streak,
    )


class ActivityDataTest(unittest.TestCase):
    def test_activity_properties_parse_time_location_and_race_labels(self) -> None:
        a = activity(
            1,
            distance=21_500,
            moving_time="1 days, 01:02:03",
            start_date_local="2026-05-02 19:15:00",
            location_country="道路, 西湖区, 杭州市, 浙江省, 310000, 中国",
        )

        self.assertEqual(a.distance_km, 21.5)
        self.assertEqual(a.year, "2026")
        self.assertEqual(a.date_local, "2026-05-02")
        self.assertEqual(a.period, "Evening")
        self.assertEqual(a.period_label, "傍晚跑步")
        self.assertEqual(a.moving_seconds, 90_123)
        self.assertEqual(a.formatted_time, "25h2m")
        self.assertEqual(a.city, "杭州市")
        self.assertEqual(a.province, "浙江省")
        self.assertEqual(a.country, "中国")
        self.assertEqual(a.race_label, "半程马拉松")

    def test_filters_can_be_composed(self) -> None:
        activities = [
            activity(1, start_date_local="2026-01-01 08:00:00"),
            activity(
                2,
                activity_type="Ride",
                start_date_local="2025-01-01 12:00:00",
                location_country="Pier, San Francisco, California, United States",
            ),
        ]

        filtered = filter_activities(
            activities,
            [
                make_year_filter("2026"),
                make_type_filter("run"),
                make_city_filter("大连"),
                make_period_filter("清晨跑步"),
                make_search_filter("run 1"),
            ],
        )

        self.assertEqual([a.run_id for a in filtered], [1])

    def test_aggregate_activities_computes_totals_breakdowns_and_ranges(self) -> None:
        activities = [
            activity(1, distance=5000, moving_time="00:25:00", streak=1),
            activity(
                2,
                distance=10_000,
                moving_time="00:50:00",
                start_date_local="2026-01-02 18:00:00",
                average_heartrate=160,
                elevation_gain=20,
                streak=2,
            ),
            activity(
                3,
                distance=12_000,
                moving_time="01:00:00",
                activity_type="Ride",
                start_date_local="2025-12-31 07:00:00",
                location_country="Pier, San Francisco, California, United States",
                average_heartrate=None,
                elevation_gain=None,
            ),
        ]

        data = aggregate_activities(activities)

        self.assertEqual(data.years, ["2026", "2025"])
        self.assertEqual(data.total_count, 3)
        self.assertAlmostEqual(data.total_distance, 27.0)
        self.assertAlmostEqual(data.total_time_sec, 8100)
        self.assertEqual(data.first_date, "2025-12-31")
        self.assertEqual(data.last_date, "2026-01-02")
        self.assertEqual(data.type_counts["Run"], 2)
        self.assertEqual(data.type_counts["Ride"], 1)
        self.assertEqual(data.year_stats["2026"].count, 2)
        self.assertEqual(data.year_stats["2026"].streak, 2)
        self.assertAlmostEqual(
            data.year_stats["2026"].daily_distances["2026-01-02"], 10
        )
        self.assertAlmostEqual(data.city_details["大连市"].total_distance, 15.0)
        self.assertEqual(data.races["10K"], 2)

    def test_contribution_grid_levels_and_month_labels(self) -> None:
        activities = [
            activity(1, distance=5000, start_date_local="2026-01-01 08:00:00"),
            activity(2, distance=10_000, start_date_local="2026-01-01 18:00:00"),
            activity(3, distance=20_000, start_date_local="2026-02-03 08:00:00"),
        ]

        grid = build_contribution_grid(activities, "2026")
        cells = {
            cell.date_str: cell
            for week in grid.weeks
            for cell in week
            if cell is not None
        }

        self.assertGreaterEqual(len(grid.weeks), 52)
        self.assertAlmostEqual(cells["2026-01-01"].distance_km, 15.0)
        self.assertEqual(cells["2026-01-01"].level, 3)
        self.assertEqual(cells["2026-02-03"].level, 4)
        self.assertIn((0, 1, "1月"), grid.month_labels)
        self.assertTrue(any(label == "2月" for _, _, label in grid.month_labels))

    def test_load_activities_round_trips_generated_json_shape(self) -> None:
        payload = [activity(1).__dict__]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "activities.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = load_activities(path)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].run_id, 1)


if __name__ == "__main__":
    unittest.main()
