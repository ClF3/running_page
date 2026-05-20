from __future__ import annotations

import unittest

try:
    import polyline

    from run_page.generator import (
        Generator,
        _build_route_for_distance,
        _is_loop,
        _route_length_m,
    )
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(
        f"optional generator dependency is not installed: {exc.name}"
    ) from exc


class GeneratorRouteTest(unittest.TestCase):
    def test_build_route_for_distance_truncates_reference_route(self) -> None:
        reference = [(0.0, 0.0), (0.0, 0.001), (0.0, 0.002)]

        route = _build_route_for_distance(reference, 80)

        self.assertEqual(route[0], reference[0])
        self.assertGreaterEqual(len(route), 2)
        self.assertAlmostEqual(_route_length_m(route), 80, delta=3)

    def test_build_route_for_distance_ping_pongs_traverse_route(self) -> None:
        reference = [(0.0, 0.0), (0.0, 0.001)]

        route = _build_route_for_distance(reference, 250)

        self.assertGreater(len(route), 3)
        self.assertAlmostEqual(_route_length_m(route), 250, delta=5)

    def test_build_route_for_distance_repeats_loop_route(self) -> None:
        reference = [(0.0, 0.0), (0.0, 0.001), (0.001, 0.001), (0.0, 0.0)]

        self.assertTrue(_is_loop(reference))
        route = _build_route_for_distance(reference, 500)

        self.assertGreater(len(route), len(reference))
        self.assertAlmostEqual(_route_length_m(route), 500, delta=8)

    def test_fix_indoor_locations_uses_previous_outdoor_route(self) -> None:
        outdoor_route = [(39.9, 116.39), (39.901, 116.391), (39.902, 116.392)]
        activities = [
            {
                "run_id": 1,
                "distance": 300,
                "subtype": "generic",
                "summary_polyline": polyline.encode(outdoor_route),
                "location_country": "道路, 大连市, 辽宁省, 中国",
            },
            {
                "run_id": 2,
                "distance": 150,
                "subtype": "treadmill",
                "summary_polyline": "",
                "location_country": None,
            },
        ]

        fixed = Generator._fix_indoor_locations(activities)
        indoor = fixed[1]

        self.assertEqual(indoor["subtype"], "indoor")
        self.assertEqual(indoor["location_country"], "道路, 大连市, 辽宁省, 中国")
        self.assertGreaterEqual(len(polyline.decode(indoor["summary_polyline"])), 2)

    def test_fix_indoor_locations_does_not_invent_route_without_reference(self) -> None:
        activities = [
            {
                "run_id": 1,
                "distance": 150,
                "subtype": "",
                "summary_polyline": "",
                "location_country": None,
            }
        ]

        fixed = Generator._fix_indoor_locations(activities)

        self.assertEqual(fixed[0]["summary_polyline"], "")
        self.assertEqual(fixed[0]["subtype"], "")


if __name__ == "__main__":
    unittest.main()
