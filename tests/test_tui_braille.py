from __future__ import annotations

import unittest

from tests.helpers import require_modules

require_modules("polyline")

import polyline

from run_page.tui.braille import BrailleCanvas, render_polyline


class BrailleCanvasTest(unittest.TestCase):
    def test_canvas_draws_line_into_braille_characters(self) -> None:
        canvas = BrailleCanvas(width_chars=4, height_chars=2)

        canvas.draw_line(0, 0, 7, 7)
        lines = canvas.to_lines()

        self.assertEqual(len(lines), 2)
        self.assertTrue(any(char != "\u2800" for line in lines for char in line))

    def test_render_polyline_returns_message_for_degenerate_routes(self) -> None:
        self.assertEqual(
            render_polyline(polyline.encode([(39.9, 116.3)]), 10, 4),
            ["  (route has 1 point(s))"],
        )

    def test_render_polyline_scales_route_to_requested_dimensions(self) -> None:
        encoded = polyline.encode(
            [(39.9, 116.3), (39.901, 116.301), (39.902, 116.302)]
        )

        lines = render_polyline(encoded, 12, 5)

        self.assertEqual(len(lines), 5)
        self.assertTrue(all(len(line) == 12 for line in lines))
        self.assertTrue(any(char != "\u2800" for line in lines for char in line))


if __name__ == "__main__":
    unittest.main()

