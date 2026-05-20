from __future__ import annotations

import unittest

try:
    import polyline

    from run_page.generator import Generator
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(
        f"optional generator dependency is not installed: {exc.name}"
    ) from exc


class GeneratorRouteTest(unittest.TestCase):
    def test_fix_indoor_locations_keeps_route_less_indoor_without_borrowing_route(
        self,
    ) -> None:
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
        self.assertIsNone(indoor["location_country"])
        self.assertEqual(indoor["summary_polyline"], "")

    def test_fix_indoor_locations_marks_no_route_activity_indoor_without_route(
        self,
    ) -> None:
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
        self.assertEqual(fixed[0]["subtype"], "indoor")

    def test_fix_indoor_locations_preserves_existing_indoor_route(self) -> None:
        route = [(39.9, 116.39), (39.90001, 116.39001), (39.90002, 116.39002)]
        encoded_route = polyline.encode(route)
        activities = [
            {
                "run_id": 1,
                "distance": 150,
                "subtype": "treadmill",
                "summary_polyline": encoded_route,
                "location_country": "北京市, 中国",
            }
        ]

        fixed = Generator._fix_indoor_locations(activities)

        self.assertEqual(fixed[0]["summary_polyline"], encoded_route)
        self.assertEqual(fixed[0]["subtype"], "indoor")
        self.assertEqual(fixed[0]["location_country"], "北京市, 中国")


if __name__ == "__main__":
    unittest.main()
