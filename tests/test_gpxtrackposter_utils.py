from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import svgwrite
    import s2sphere as s2

    from run_page.gpxtrackposter.grid_drawer import GridDrawer
    from run_page.gpxtrackposter.poster import Poster
    from run_page.gpxtrackposter.track import Track
    from run_page.gpxtrackposter.utils import (
        compute_grid,
        lat2y,
        lng2x,
        parse_datetime_to_local,
        project,
    )
    from run_page.gpxtrackposter.xy import XY
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(
        f"optional gpxtrackposter dependency is not installed: {exc.name}"
    ) from exc


class GpxTrackPosterUtilsTest(unittest.TestCase):
    def test_historical_local_time_uses_activity_date(self) -> None:
        cases = [
            (dt.datetime(2026, 2, 2, 6, 22, 42, tzinfo=dt.timezone.utc), 2, 7),
            (dt.datetime(2026, 7, 2, 6, 22, 42, tzinfo=dt.timezone.utc), 1, 8),
        ]
        for start, import_offset, expected_hour in cases:
            with (
                self.subTest(start=start),
                patch("run_page.gpxtrackposter.utils.datetime") as clock,
            ):
                clock.now.return_value = dt.datetime(
                    2026, 1, 1, tzinfo=dt.timezone(dt.timedelta(hours=import_offset))
                )
                local_start, local_end = parse_datetime_to_local(
                    start, start + dt.timedelta(minutes=30), (50.85, 4.35)
                )
                expected = start.replace(tzinfo=None, hour=expected_hour)
                self.assertEqual(local_start.replace(tzinfo=None), expected)
                self.assertEqual(
                    local_end.replace(tzinfo=None), expected + dt.timedelta(minutes=30)
                )

    def test_local_times_follow_a_dst_transition_within_the_activity(self) -> None:
        start = dt.datetime(2026, 3, 29, 0, 30, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(hours=1)
        local_start, local_end = parse_datetime_to_local(start, end, (48.86, 2.35))
        self.assertEqual(local_start, dt.datetime(2026, 3, 29, 1, 30))
        self.assertEqual(local_end, dt.datetime(2026, 3, 29, 3, 30))

    def test_non_utc_gpx_timestamp_is_converted_without_double_offset(self) -> None:
        start = dt.datetime(
            2026, 2, 2, 14, 22, 42, tzinfo=dt.timezone(dt.timedelta(hours=8))
        )
        local_start, local_end = parse_datetime_to_local(
            start, start + dt.timedelta(minutes=30), (50.85, 4.35)
        )
        self.assertEqual(local_start, dt.datetime(2026, 2, 2, 7, 22, 42))
        self.assertEqual(local_end, dt.datetime(2026, 2, 2, 7, 52, 42))
        self.assertEqual(start.hour, 14)

    def test_without_gps_uses_explicit_offset_or_default_for_utc(self) -> None:
        explicit = dt.datetime(
            2026, 2, 2, 7, 22, 42, tzinfo=dt.timezone(dt.timedelta(hours=1))
        )
        local_start, _ = parse_datetime_to_local(
            explicit, explicit + dt.timedelta(minutes=30), None
        )
        self.assertEqual(local_start, dt.datetime(2026, 2, 2, 7, 22, 42))
        for start in (
            dt.datetime(2026, 2, 2, 0),
            dt.datetime(2026, 2, 2, 0, tzinfo=dt.timezone.utc),
        ):
            local_start, _ = parse_datetime_to_local(
                start, start + dt.timedelta(minutes=30), None
            )
            self.assertEqual(local_start, dt.datetime(2026, 2, 2, 8))

    def test_compute_grid_packs_requested_count_inside_dimensions(self) -> None:
        size, counts = compute_grid(5, XY(100, 60))

        self.assertIsNotNone(size)
        self.assertIsNotNone(counts)
        self.assertGreaterEqual(counts[0] * counts[1], 5)
        self.assertLessEqual(size * counts[0], 100)
        self.assertLessEqual(size * counts[1], 60)

    def test_mercator_projection_helpers_return_stable_values(self) -> None:
        self.assertAlmostEqual(lng2x(0), 1.0)
        self.assertAlmostEqual(lng2x(180), 2.0)
        self.assertAlmostEqual(lat2y(0), 0.5)

    def test_project_maps_latlng_lines_into_target_box(self) -> None:
        points = [
            s2.LatLng.from_degrees(39.9, 116.3),
            s2.LatLng.from_degrees(39.91, 116.31),
            s2.LatLng.from_degrees(39.92, 116.32),
        ]
        bbox = s2.LatLngRect.empty()
        for point in points:
            bbox = bbox.union(s2.LatLngRect.from_point(point))

        lines = project(bbox, XY(100, 100), XY(0, 0), [points])

        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0]), 3)
        for x, y in lines[0]:
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x, 100)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(y, 100)

    def test_grid_drawer_adds_clickable_cell_hit_area(self) -> None:
        track = Track()
        track.run_id = 1001
        track.length = 12_000
        track.special = False
        track.start_time_local = dt.datetime(2026, 1, 1, 8, 0, 0)
        track.polylines = [
            [
                s2.LatLng.from_degrees(39.9, 116.3),
                s2.LatLng.from_degrees(39.91, 116.31),
            ]
        ]

        poster = Poster()
        poster.title = "Grid"
        poster.athlete = "Runner"
        poster.colors["track2"] = poster.colors["track"]
        poster.colors["special2"] = poster.colors["special"]
        poster.set_tracks([track])
        drawer = GridDrawer(poster)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "grid.svg"
            drawing = svgwrite.Drawing(str(output), ("200mm", "300mm"))
            drawing.viewbox(0, 0, 200, 300)
            drawer.draw(drawing, XY(180, 240), XY(10, 30))
            drawing.save()
            svg = output.read_text()

        self.assertIn('class="grid-hit-area"', svg)
        self.assertIn("<desc>1001</desc>", svg)
        self.assertIn("pointer-events: all", svg)


if __name__ == "__main__":
    unittest.main()
