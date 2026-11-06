from __future__ import annotations

import importlib
import unittest


def require_modules(*module_names: str) -> None:
    """Skip a test module when optional project dependencies are absent."""

    for module_name in module_names:
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            missing = exc.name or module_name
            raise unittest.SkipTest(
                f"optional test dependency is not installed: {missing}"
            ) from exc


def sample_gpx(name: str = "Morning Run") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="running_page tests"
     xmlns="http://www.topografix.com/GPX/1/1">
  <metadata>
    <time>2026-01-01T00:00:00Z</time>
  </metadata>
  <trk>
    <name>{name}</name>
    <type>running</type>
    <trkseg>
      <trkpt lat="39.900000" lon="116.390000">
        <ele>10.0</ele>
        <time>2026-01-01T00:00:00Z</time>
      </trkpt>
      <trkpt lat="39.901000" lon="116.391000">
        <ele>12.0</ele>
        <time>2026-01-01T00:00:10Z</time>
      </trkpt>
      <trkpt lat="39.902000" lon="116.392000">
        <ele>15.0</ele>
        <time>2026-01-01T00:00:20Z</time>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""
