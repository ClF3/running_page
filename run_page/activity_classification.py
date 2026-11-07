"""Source activity types and route availability are independent facts.

Used by JSON exports and SVG tracks. Never infer indoor running from missing
GPS, distance or the size of a route, and never overwrite the source subtype.
"""

import math

import polyline

INDOOR_SUBTYPES = frozenset(
    {
        "indoor_running",  # Garmin Connect activity types
        "treadmill_running",
        "indoor",  # Legacy exports / FIT sub_sport
        "treadmill",
        "virtualrun",  # Strava
        "virtual_run",
    }
)


def is_indoor_activity(subtype):
    return (subtype or "").strip().lower() in INDOOR_SUBTYPES


def classify_route(summary_polyline):
    """Return (status, coordinates); unusable routes have no drawable points."""
    if not summary_polyline or not summary_polyline.strip():
        return "missing", []
    try:
        points = polyline.decode(summary_polyline)
        if any(
            not math.isfinite(lat)
            or not math.isfinite(lon)
            or not -90 <= lat <= 90
            or not -180 <= lon <= 180
            for lat, lon in points
        ):
            return "invalid", []
    except (IndexError, TypeError, ValueError, OverflowError):
        return "invalid", []
    if len(points) < 2 or all(point == points[0] for point in points[1:]):
        return "insufficient", []
    return "available", points


def activity_flags(subtype, summary_polyline):
    status, _ = classify_route(summary_polyline)
    return {
        "is_indoor": is_indoor_activity(subtype),
        "has_route": status == "available",
        "route_status": status,
    }
