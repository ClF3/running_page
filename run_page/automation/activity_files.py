"""Validate original activity archives and adapt FIT/TCX tracks to GPX."""

from dataclasses import dataclass
from datetime import datetime, timezone
import io
import math
from pathlib import PurePosixPath
import stat
import xml.etree.ElementTree as ET
import zipfile

MAX_ACTIVITY_BYTES = 64 * 1024 * 1024
GPX_NS = "http://www.topografix.com/GPX/1/1"
HR_NS = "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"


@dataclass(frozen=True)
class ActivityFile:
    format: str
    content: bytes

    def gpx(self):
        if self.format == "gpx":
            return self.content
        if self.format == "fit":
            return points_to_gpx(fit_points(self.content))
        if self.format == "tcx":
            return points_to_gpx(tcx_points(self.content))
        raise RuntimeError("Unsupported original activity format")


def unpack_activity(content):
    """Read one original file in memory; never extract server-supplied paths."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 100:
                raise RuntimeError("Activity archive has too many entries")
            supported = []
            for entry in entries:
                path = PurePosixPath(entry.filename.replace("\\", "/"))
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise RuntimeError("Unsafe activity archive entry")
                if not entry.is_dir() and path.suffix.lower() in {
                    ".fit",
                    ".gpx",
                    ".tcx",
                }:
                    supported.append(entry)
            if len(supported) != 1:
                raise RuntimeError(
                    "Activity archive must contain exactly one FIT, GPX or TCX file"
                )
            entry = supported[0]
            if not 0 < entry.file_size <= MAX_ACTIVITY_BYTES:
                raise RuntimeError("Original activity is empty or exceeds 64 MiB")
            with archive.open(entry) as stream:
                raw = stream.read(MAX_ACTIVITY_BYTES + 1)
            if not raw or len(raw) > MAX_ACTIVITY_BYTES:
                raise RuntimeError("Original activity is empty or exceeds 64 MiB")
            return ActivityFile(PurePosixPath(entry.filename).suffix.lower()[1:], raw)
    except (zipfile.BadZipFile, EOFError, OSError, NotImplementedError) as exc:
        raise RuntimeError("Original activity ZIP is invalid or incomplete") from exc


def fit_points(content):
    from garmin_fit_sdk import Decoder, Stream
    from garmin_fit_sdk.util import FIT_EPOCH_S

    try:
        decoder = Decoder(Stream.from_byte_array(bytearray(content)))
        if not decoder.is_fit():
            raise RuntimeError("Original activity is not FIT")
        messages, errors = decoder.read(convert_datetimes_to_dates=False)
        if errors or not messages.get("session_mesgs"):
            raise RuntimeError("FIT activity is corrupt or lacks a session")
        points = []
        for item in messages.get("record_mesgs", []):
            lat, lon = item.get("position_lat"), item.get("position_long")
            if lat is None or lon is None:
                continue  # Indoor records and the period before GPS acquisition.
            timestamp = item.get("timestamp")
            if timestamp is None:
                raise RuntimeError("FIT GPS record has no timestamp")
            altitude = item.get("enhanced_altitude")
            if altitude is None:
                altitude = item.get("altitude")
            points.append(
                (
                    float(lat) * 180 / 2**31,
                    float(lon) * 180 / 2**31,
                    datetime.fromtimestamp(
                        float(timestamp) + FIT_EPOCH_S, timezone.utc
                    ),
                    altitude,
                    item.get("heart_rate"),
                )
            )
        return points
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("FIT activity could not be decoded completely") from exc


def tcx_points(content):
    try:
        root = ET.fromstring(content)
        if root.tag.split("}")[-1] != "TrainingCenterDatabase":
            raise RuntimeError("Original activity is not TCX")
        if not root.findall(".//{*}Activity"):
            raise RuntimeError("TCX has no activity")
        points = []
        for item in root.findall(".//{*}Trackpoint"):
            lat = item.findtext("{*}Position/{*}LatitudeDegrees")
            lon = item.findtext("{*}Position/{*}LongitudeDegrees")
            if lat is None or lon is None:
                continue
            timestamp = item.findtext("{*}Time")
            if timestamp is None:
                raise RuntimeError("TCX GPS record has no timestamp")
            instant = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise RuntimeError("TCX GPS record has no timezone")
            points.append(
                (
                    float(lat),
                    float(lon),
                    instant,
                    item.findtext("{*}AltitudeMeters"),
                    item.findtext("{*}HeartRateBpm/{*}Value"),
                )
            )
        return points
    except (ET.ParseError, ValueError, TypeError) as exc:
        raise RuntimeError("TCX activity could not be decoded completely") from exc


def points_to_gpx(points):
    ET.register_namespace("", GPX_NS)
    ET.register_namespace("gpxtpx", HR_NS)
    root = ET.Element(f"{{{GPX_NS}}}gpx", version="1.1", creator="Running Page")
    segment = ET.SubElement(
        ET.SubElement(root, f"{{{GPX_NS}}}trk"), f"{{{GPX_NS}}}trkseg"
    )
    for lat, lon, timestamp, altitude, heart_rate in points:
        if (
            not math.isfinite(lat)
            or not math.isfinite(lon)
            or abs(lat) > 90
            or abs(lon) > 180
        ):
            raise RuntimeError("Original activity has invalid GPS coordinates")
        point = ET.SubElement(segment, f"{{{GPX_NS}}}trkpt", lat=str(lat), lon=str(lon))
        ET.SubElement(point, f"{{{GPX_NS}}}time").text = timestamp.astimezone(
            timezone.utc
        ).isoformat()
        if altitude is not None:
            value = float(altitude)
            if not math.isfinite(value):
                raise RuntimeError("Original activity has invalid elevation")
            ET.SubElement(point, f"{{{GPX_NS}}}ele").text = str(value)
        if heart_rate is not None and 0 < float(heart_rate) < 255:
            extensions = ET.SubElement(point, f"{{{GPX_NS}}}extensions")
            sensor = ET.SubElement(extensions, f"{{{HR_NS}}}TrackPointExtension")
            ET.SubElement(sensor, f"{{{HR_NS}}}hr").text = str(int(float(heart_rate)))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
