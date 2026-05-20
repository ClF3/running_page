from __future__ import annotations

import unittest
from unittest import mock

from tests.helpers import require_modules

require_modules("polyline", "haversine")

import polyline

from run_page import polyline_processor


class PolylineProcessorTest(unittest.TestCase):
    def test_start_end_hiding_removes_configured_distance_from_both_ends(self) -> None:
        points = [(0.0, 0.0), (0.0, 0.001), (0.0, 0.002), (0.0, 0.003)]

        hidden = polyline_processor.start_end_hiding(points, 0.1)

        self.assertEqual(hidden, [(0.0, 0.001), (0.0, 0.002)])

    def test_start_end_hiding_returns_empty_when_route_is_fully_hidden(self) -> None:
        points = [(0.0, 0.0), (0.0, 0.001), (0.0, 0.002)]

        self.assertEqual(polyline_processor.start_end_hiding(points, 10), [])

    def test_start_end_hiding_keeps_route_when_distance_is_zero(self) -> None:
        points = [(0.0, 0.0), (0.0, 0.001), (0.0, 0.002)]

        self.assertEqual(polyline_processor.start_end_hiding(points, 0), points)

    def test_range_hiding_removes_points_near_sensitive_locations(self) -> None:
        points = [(0.0, 0.0), (0.0, 0.001), (0.0, 0.003)]

        hidden = polyline_processor.range_hiding(points, [(0.0, 0.001)], 0.05)

        self.assertEqual(hidden, [(0.0, 0.0), (0.0, 0.003)])

    def test_filter_out_applies_start_end_and_sensitive_point_filters(self) -> None:
        points = [(0.0, 0.0), (0.0, 0.001), (0.0, 0.002), (0.0, 0.003)]
        encoded = polyline.encode(points)

        with mock.patch.object(polyline_processor, "IGNORE_START_END_RANGE", 0.1):
            with mock.patch.object(
                polyline_processor, "IGNORE_POLYLINE", [(0.0, 0.001)]
            ):
                with mock.patch.object(polyline_processor, "IGNORE_RANGE", 0.05):
                    filtered = polyline_processor.filter_out(encoded)

        self.assertEqual(polyline.decode(filtered), [(0.0, 0.002)])

    def test_filter_out_keeps_empty_values_empty(self) -> None:
        self.assertIsNone(polyline_processor.filter_out(""))
        self.assertIsNone(polyline_processor.filter_out(None))


if __name__ == "__main__":
    unittest.main()
