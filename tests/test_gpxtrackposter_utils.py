from __future__ import annotations

import unittest

from tests.helpers import require_modules

require_modules("colour", "s2sphere")

import s2sphere as s2

from run_page.gpxtrackposter.utils import compute_grid, lat2y, lng2x, project
from run_page.gpxtrackposter.xy import XY


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


if __name__ == "__main__":
    unittest.main()

