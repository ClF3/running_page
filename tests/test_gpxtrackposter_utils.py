from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

try:
    import svgwrite
    import s2sphere as s2

    from run_page.gpxtrackposter.grid_drawer import GridDrawer
    from run_page.gpxtrackposter.poster import Poster
    from run_page.gpxtrackposter.track import Track
    from run_page.gpxtrackposter.utils import compute_grid, lat2y, lng2x, project
    from run_page.gpxtrackposter.xy import XY
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(
        f"optional gpxtrackposter dependency is not installed: {exc.name}"
    ) from exc


class GpxTrackPosterUtilsTest(unittest.TestCase):
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
