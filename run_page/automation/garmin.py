"""Strict Garmin activity synchronization into a disposable snapshot."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import math
import os
import re
from pathlib import Path
import sqlite3
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

from .common import atomic_write, read_json, write_json, digest_file
from .activity_files import ActivityFile


def parse_time(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def is_running(kind):
    if not isinstance(kind, dict):
        return False
    return (
        kind.get("typeId") == 1
        or kind.get("parentTypeId") == 1
        or bool(re.search(r"(?:^|_)run(?:ning)?(?:_|$)", str(kind.get("typeKey", ""))))
    )


def enrich_gpx(content, summary):
    root = ET.fromstring(content)
    if root.tag.split("}")[-1] != "gpx":
        raise RuntimeError("Garmin download is not GPX")
    if root.tag.startswith("{"):
        ET.register_namespace("", root.tag[1:].split("}")[0])
    dto = summary["summaryDTO"]
    start = parse_time(dto["startTimeGMT"])
    duration = number(dto, "duration", 0)
    moving = number(dto, "movingDuration", duration)
    values = {
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(seconds=duration)).isoformat(),
        "distance": dto["distance"],
        "moving_time": moving,
        "elapsed_time": number(dto, "elapsedDuration", duration),
        "average_speed": number(
            dto, "averageSpeed", float(dto["distance"]) / moving if moving else 0
        ),
        "average_hr": dto.get("averageHR"),
    }
    ext = next((x for x in root if x.tag.split("}")[-1] == "extensions"), None)
    if ext is None:
        ext = ET.Element("extensions")
        root.insert(0, ext)
    for child in list(ext):
        if child.tag.split("}")[-1] in values:
            ext.remove(child)
    for key, value in values.items():
        if value is not None:
            ET.SubElement(ext, key).text = str(value)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def number(dto, key, default):
    value = dto.get(key)
    result = float(default if value is None else value)
    if not math.isfinite(result):
        raise RuntimeError("Garmin statistics contain a non-finite number")
    return result


def gpx_start(path):
    """Read only the beginning of a GPX, without decoding every historic route."""
    stack, fallback = [], None
    for event, element in ET.iterparse(path, events=("start", "end")):
        tag = element.tag.split("}")[-1]
        if event == "start":
            stack.append(tag)
        else:
            if tag == "start_time" and element.text:
                fallback = parse_time(element.text)
            if tag == "time" and len(stack) > 1 and stack[-2] == "trkpt":
                return parse_time(element.text)
            stack.pop()
            element.clear()
    return fallback


def init_sources(database, raw_dir):
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS automation_sources (
            source_id TEXT PRIMARY KEY, run_id INTEGER UNIQUE NOT NULL,
            start_time TEXT NOT NULL, raw_hash TEXT NOT NULL DEFAULT '',
            ignored INTEGER NOT NULL DEFAULT 0)""")
        existing = {
            row[0]: row[1]
            for row in db.execute("SELECT run_id, start_date FROM activities")
        }
        mapped = {
            row[0] for row in db.execute("SELECT source_id FROM automation_sources")
        }
        for path in sorted(Path(raw_dir).glob("*.gpx")):
            if not path.stem.isdigit() or path.stem in mapped:
                continue
            try:
                start = gpx_start(path)
                run_id = int(start.timestamp() * 1000) if start else None
            except (ValueError, ET.ParseError):
                continue  # Unmatched files are validated if Garmin selects them.
            if run_id not in existing:
                continue
            try:
                db.execute(
                    "INSERT INTO automation_sources(source_id,run_id,start_time) VALUES(?,?,?)",
                    (path.stem, run_id, parse_time(existing[run_id]).isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError(
                    f"Ambiguous legacy Garmin mapping: {path.stem}"
                ) from exc


def source_rows(database):
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        return {
            row["source_id"]: dict(row)
            for row in db.execute(
                "SELECT s.*, a.run_id IS NOT NULL AS visible FROM automation_sources s "
                "LEFT JOIN activities a ON a.run_id=s.run_id"
            )
        }


def select_ids(remote, sources, cutoff, full=False):
    selected = set()
    for item in remote:
        source_id = str(item["activityId"])
        if not re.fullmatch(r"[0-9]{1,20}", source_id):
            raise RuntimeError("Invalid Garmin activity ID")
        old = sources.get(source_id)
        if old and old.get("ignored"):
            continue
        if (
            full
            or old is None
            or not old.get("visible", True)
            or parse_time(item["startTimeGMT"]) >= cutoff
        ):
            selected.add(source_id)
    # Also inspect IDs that moved out of the remote time/type query window.
    for source_id, old in sources.items():
        if (
            not old.get("ignored")
            and old.get("visible", True)
            and (full or parse_time(old["start_time"]) >= cutoff)
        ):
            selected.add(source_id)
    return sorted(selected)


class ActivityEndpoints:
    """Activity endpoints shared by Personal API and legacy session clients."""

    page_size = 100

    async def activities(self, *, all_types=False, start_date=None):
        result, seen = [], set()
        filters = {} if all_types else {"activityType": "running"}
        if start_date is not None:
            filters["startDate"] = start_date
        for start in range(0, 100000, self.page_size):
            query = urlencode({"start": start, "limit": self.page_size, **filters})
            page = await self.request(
                f"/activitylist-service/activities/search/activities?{query}"
            )
            if not isinstance(page, list):
                raise RuntimeError("Invalid Garmin activities response")
            if not page:
                return result
            ids = {str(item["activityId"]) for item in page}
            if seen & ids or len(ids) != len(page):
                raise RuntimeError("Garmin pagination repeated activity IDs")
            seen.update(ids)
            result.extend(page)
        raise RuntimeError("Garmin pagination safety limit reached")

    async def summary(self, source_id):
        return await self.request(f"/activity-service/activity/{source_id}")

    async def close(self):
        await self.http.aclose()


def authentication_status(config):
    if os.getenv("GARMIN_PERSONAL_API_KEY", "").strip():
        method = "personal_api"
    elif config.garmin_region == "cn" and (
        os.getenv("GARMIN_SECRET_STRING_CN", "").strip()
        or (config.state / "private/garmin-session.txt").is_file()
    ):
        method = "legacy_session"
    else:
        method = "not_configured"
    return {"method": method, "region": config.garmin_region}


def create_client(config):
    method = authentication_status(config)["method"]
    if method == "personal_api":
        from .personal_api import PersonalApiClient

        return PersonalApiClient(config.state, config.garmin_region)
    if method == "legacy_session":
        return GarminClient(config.state)
    raise RuntimeError(
        "Set GARMIN_PERSONAL_API_KEY before synchronizing Garmin activities"
    )


class GarminClient(ActivityEndpoints):
    def __init__(self, state):
        import garth
        import httpx

        self.garth = garth
        self.session_path = Path(state) / "private" / "garmin-session.txt"
        secret = (
            self.session_path.read_text().strip()
            if self.session_path.exists()
            else os.getenv("GARMIN_SECRET_STRING_CN", "").strip()
        )
        if not secret:
            raise RuntimeError(
                "GARMIN_SECRET_STRING_CN or an imported Garmin session is required"
            )
        os.environ["GARTH_TELEMETRY_ENABLED"] = "false"
        garth.configure(domain="garmin.cn", ssl_verify=True, telemetry_enabled=False)
        try:
            garth.client.loads(secret)
            if garth.client.oauth2_token.expired:
                garth.client.refresh_oauth2()
        except Exception as exc:
            raise RuntimeError(
                "Garmin session could not be loaded/refreshed; import a valid CN session"
            ) from exc
        self.persist()
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20))

    def persist(self):
        atomic_write(self.session_path, self.garth.client.dumps())

    async def request(self, path, as_json=True):
        import httpx

        for attempt in range(3):
            try:
                response = await self.http.get(
                    "https://connectapi.garmin.cn" + path,
                    headers={
                        "Authorization": str(self.garth.client.oauth2_token),
                        "User-Agent": "Mozilla/5.0",
                        "origin": "https://sso.garmin.cn",
                    },
                )
                if response.status_code == 401 and attempt < 2:
                    self.garth.client.refresh_oauth2()
                    self.persist()
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        delay = response.headers.get("Retry-After", "")
                        await asyncio.sleep(
                            min(120, int(delay))
                            if delay.isdigit()
                            else 2 ** (attempt + 1)
                        )
                        continue
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Garmin request failed (HTTP {response.status_code})"
                    )
                if len(response.content) > 64 * 1024 * 1024:
                    raise RuntimeError("Garmin response exceeds 64 MiB")
                return response.json() if as_json else response.content
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == 2:
                    raise RuntimeError(
                        "Garmin network request failed after 3 attempts"
                    ) from None
                await asyncio.sleep(2 ** (attempt + 1))
        raise RuntimeError("Garmin authentication refresh failed")

    async def download(self, source_id):
        return await self.request(
            f"/download-service/export/gpx/activity/{source_id}", False
        )


def import_activity(database, raw_path, summary, old=None):
    """Keep a stable local ID, using Garmin metadata for editable statistics."""
    from generator.db import Activity, init_db, g
    from gpxtrackposter.track import Track

    dto = summary.get("summaryDTO")
    if (
        not isinstance(dto, dict)
        or "distance" not in dto
        or not dto.get("startTimeGMT")
    ):
        raise RuntimeError("Incomplete Garmin activity summary")
    kind = summary.get("activityTypeDTO", {})
    activity_type = kind.get("typeKey", "running")
    if not is_running({**kind, "typeKey": activity_type}):
        raise RuntimeError(
            "Activity is no longer running; resolve it explicitly before retrying"
        )
    track = Track()
    track.load_gpx(str(raw_path))
    if (
        track.start_time is None
        or track.end_time is None
        or track.start_time_local is None
        or not {"distance", "moving_time", "elapsed_time", "average_speed"}.issubset(
            track.moving_dict
        )
    ):
        raise RuntimeError("GPX parsing did not produce a complete activity")
    parsed = track.to_namedtuple()
    start = parse_time(dto["startTimeGMT"]).astimezone(timezone.utc)
    local = dto.get("startTimeLocal")
    if local:
        local = datetime.fromisoformat(str(local).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    else:
        offset = track.start_time_local - track.start_time.replace(tzinfo=None)
        local = start.replace(tzinfo=None) + offset
    duration = number(
        dto, "duration", (track.end_time - track.start_time).total_seconds()
    )
    moving = number(dto, "movingDuration", duration)
    elapsed = number(dto, "elapsedDuration", duration)
    distance = number(dto, "distance", 0)
    if min(duration, moving, distance, elapsed) < 0 or moving > elapsed + 1:
        raise RuntimeError("Invalid Garmin distance or duration")
    session = init_db(str(database))
    try:
        run_id = int(old["run_id"]) if old else parsed.id
        row = session.get(Activity, run_id)
        if not old and row is not None:
            raise RuntimeError("New Garmin activity collides with an existing local ID")
        if row is None:
            row = Activity(run_id=run_id)
            session.add(row)
        location = row.location_country or ""
        route_changed = row.summary_polyline != parsed.map.summary_polyline
        if track.start_latlng and (not location or route_changed):
            try:
                location = str(
                    g.reverse(
                        f"{track.start_latlng.lat}, {track.start_latlng.lon}",
                        language="zh-CN",
                        timeout=15,
                    )
                    or location
                )
            except Exception:
                logging.warning(
                    "Reverse geocoding unavailable; retaining existing location"
                )
        values = dict(
            name=summary.get("activityName", parsed.name),
            distance=distance,
            moving_time=timedelta(seconds=moving),
            elapsed_time=timedelta(seconds=elapsed),
            type="Run",
            subtype=activity_type,
            start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            start_date_local=local.strftime("%Y-%m-%d %H:%M:%S"),
            location_country=location,
            summary_polyline=parsed.map.summary_polyline,
            average_heartrate=dto.get("averageHR", parsed.average_heartrate),
            average_speed=number(
                dto, "averageSpeed", distance / moving if moving else 0
            ),
            elevation_gain=number(dto, "elevationGain", parsed.elevation_gain or 0),
        )
        for key, value in values.items():
            setattr(row, key, value)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    with sqlite3.connect(database) as db:
        db.execute(
            """INSERT INTO automation_sources(source_id,run_id,start_time,raw_hash)
                   VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                   start_time=excluded.start_time,raw_hash=excluded.raw_hash""",
            (Path(raw_path).stem, run_id, start.isoformat(), digest_file(raw_path)),
        )


async def sync_snapshot(
    snapshot,
    client,
    days=30,
    full=False,
    now=None,
    queue_global=False,
    *,
    catalog=None,
    private_ids=frozenset(),
    queued=None,
):
    from generator import Generator
    from utils import write_activities_files

    snapshot = Path(snapshot)
    database, raw = snapshot / "run_page/data.db", snapshot / "GPX_OUT"
    sources = source_rows(database)
    remote = (
        await client.activities()
        if catalog is None
        else [item for item in catalog if is_running(item.get("activityType"))]
    )
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    selected = set(select_ids(remote, sources, cutoff, full))
    if catalog is not None:
        for item in catalog:
            source_id = str(item["activityId"])
            old = sources.get(source_id)
            kind = item.get("activityType") or {}
            if (
                old
                and old["visible"]
                and not old["ignored"]
                and kind.get("typeKey")
                and not is_running(kind)
            ):
                selected.add(source_id)
    downloaded_files = set()
    refreshed = 0
    logging.info("Refreshing %s Garmin activities", len(selected))
    for source_id in sorted(selected):
        logging.info("Refreshing Garmin activity %s", source_id)
        summary = await client.summary(source_id)
        if str(summary.get("activityId")) != source_id:
            raise RuntimeError("Garmin summary activity ID does not match request")
        if catalog is not None:
            kind = summary.get("activityTypeDTO") or {}
            if not kind.get("typeKey"):
                raise RuntimeError("Garmin summary is missing an activity type")
            if not is_running(kind):
                if source_id in sources:
                    with sqlite3.connect(database) as db:
                        db.execute(
                            "DELETE FROM activities WHERE run_id=?",
                            (sources[source_id]["run_id"],),
                        )
                continue
        content = await client.download(source_id)
        original = (
            content
            if isinstance(content, ActivityFile)
            else ActivityFile("gpx", content)
        )
        original_relative = f"ORIGINAL_OUT/{source_id}.gpx"
        if isinstance(content, ActivityFile):
            original = content
            content = original.gpx()
            if original.format in {"fit", "tcx"}:
                filename = f"{source_id}.{original.format}"
                original_relative = f"{original.format.upper()}_OUT/{filename}"
                atomic_write(
                    snapshot / f"{original.format.upper()}_OUT" / filename,
                    original.content,
                )
                downloaded_files.add(filename)
        if queue_global and original.format == "gpx":
            atomic_write(snapshot / original_relative, original.content)
        content = enrich_gpx(content, summary)
        path = raw / f"{source_id}.gpx"
        atomic_write(path, content)
        # Parsing/import failures abort this disposable snapshot, never current.
        import_activity(database, path, summary, sources.get(source_id))
        refreshed += 1
        downloaded_files.add(path.name)
        if queue_global and source_id not in private_ids:
            from .global_sync import enqueue_activity

            enqueue_activity(database, source_id, summary, original_relative)
            if queued is not None:
                queued.add(source_id)
    generator = Generator(str(database))
    try:
        activities = generator.load()
    finally:
        generator.session.close()
    write_activities_files(activities, snapshot / "src/static/activities.json")
    imported = sorted(
        {
            *read_json(snapshot / "imported.json", []),
            *downloaded_files,
        }
    )
    write_json(snapshot / "imported.json", imported)
    return {"refreshed": refreshed, "activities": len(activities)}
