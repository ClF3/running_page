"""Discover Garmin activities and reuse the website's completed source scan."""

from datetime import datetime, timedelta, timezone
import json
import logging
import re
from zoneinfo import ZoneInfo

from .common import (
    atomic_write,
    digest_file,
    digest_json,
    read_json,
    utcnow,
    write_json,
)
from .garmin import is_running, parse_time
from .global_sync import (
    EDITABLE_STATS,
    _connect,
    _fields,
    _path,
    private_database,
    private_source_ids,
)

CATALOG_FILE = "private/garmin-activity-catalog.json"
CATALOG_MAX_AGE = timedelta(minutes=45)
CATALOG_FIELDS = (
    "activityId",
    "activityType",
    "activityName",
    "parentActivityId",
    *EDITABLE_STATS,
)


def remember_catalog(config, scope, activities, queued=(), *, now=None):
    if not scope:
        return
    try:
        write_json(
            config.state / CATALOG_FILE,
            {
                "scope": scope,
                "all_types": config.global_sync_all_activities,
                "saved_at": (now or datetime.now(timezone.utc)).isoformat(),
                "activities": [_catalog_item(row) for row in activities],
                "queued": sorted(queued),
            },
        )
    except OSError:
        logging.warning("Unable to retain the completed Garmin activity catalog")


def _catalog_item(row):
    return {key: row[key] for key in CATALOG_FIELDS if key in row}


async def independent_catalog(config, client, *, full=False, now=None):
    """Use the fresh date-filtered list and a recent, account-bound full catalog."""
    now = now or datetime.now(timezone.utc)
    scope = getattr(client, "catalog_scope", None)
    all_types = config.global_sync_all_activities
    cached = {}
    if scope and not full:
        try:
            value = read_json(config.state / CATALOG_FILE, {})
            if (
                value.get("scope") == scope
                and value.get("all_types") == all_types
                and timedelta()
                <= now - parse_time(value["saved_at"])
                <= CATALOG_MAX_AGE
            ):
                cached = value
                previous = {str(row["activityId"]): row for row in cached["activities"]}
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            cached = {}
    if not cached:
        activities = await client.activities(all_types=all_types)
        remember_catalog(config, scope, activities, now=now)
        return activities, set(), "full"

    start_date = now.astimezone(ZoneInfo("Asia/Shanghai")).date() - timedelta(
        days=config.days
    )
    recent = await client.activities(
        all_types=all_types, start_date=start_date.isoformat()
    )
    merged = {
        **previous,
        **{str(row["activityId"]): _catalog_item(row) for row in recent},
    }
    reusable = {
        source_id
        for source_id in cached.get("queued", [])
        if source_id in previous and merged[source_id] == previous[source_id]
    }
    return list(merged.values()), reusable, "recent_with_history"


def discover_activities(
    config,
    catalog,
    *,
    full=False,
    now=None,
    include_running=False,
    reusable=frozenset(),
):
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=config.days)
    rows = {}
    for item in catalog:
        source_id = str(item.get("activityId"))
        kind = item.get("activityType") or {}
        if (
            not re.fullmatch(r"[0-9]{1,20}", source_id)
            or int(source_id) <= 0
            or source_id in rows
            or not kind.get("typeKey")
        ):
            raise RuntimeError("Invalid activity in the complete Garmin catalog")
        rows[source_id] = (item, kind)

    running_db = config.current / "run_page/data.db"
    private_db = private_database(config)
    root = private_db.parent.parent
    if config.global_sync_all_activities:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_db.parent.mkdir(mode=0o700, exist_ok=True)
        with _connect(private_db):
            private_db.chmod(0o600)

    with _connect(running_db) as db:
        public = {
            row["source_id"]: row
            for row in db.execute("SELECT * FROM main.automation_global_sync")
        }
        private = {}
        if config.global_sync_all_activities:
            db.execute("ATTACH DATABASE ? AS private_queue", (str(private_db),))
            private = {
                row["source_id"]: row
                for row in db.execute(
                    "SELECT * FROM private_queue.automation_global_sync"
                )
            }
        private_ids = (
            private.keys()
            if config.global_sync_all_activities
            else private_source_ids(config)
        )
        if public.keys() & private_ids:
            raise RuntimeError("A source activity exists in both delivery queues")
        ignored = {
            row[0]
            for row in db.execute(
                "SELECT source_id FROM automation_sources WHERE ignored"
            )
        }

        for source_id, (item, kind) in rows.items():
            if str(item.get("parentActivityId")) in rows and source_id not in private:
                continue  # Garmin imports known multisport legs with the parent FIT.
            to_private = source_id in private_ids or not is_running(kind)
            if to_private:
                if not config.global_sync_all_activities:
                    continue
                schema, old = "private_queue", private.get(source_id)
            else:
                if not include_running or source_id in ignored:
                    continue
                schema, old = "main", public.get(source_id)

            if to_private and old is None and source_id in public:
                old = public[source_id]
                version = json.loads(old["desired"])
                if version["file"]:
                    try:
                        file = _path(config.current, version["file"])
                    except RuntimeError:
                        file = None
                    if file is not None and digest_file(file) == version["file_hash"]:
                        atomic_write(root / version["file"], file.read_bytes())
                # Move the mapping and any outstanding receipt together before
                # fetching the newly classified activity into the private cache.
                db.execute(
                    f"INSERT INTO {schema}.automation_global_sync "
                    "SELECT * FROM main.automation_global_sync WHERE source_id=?",
                    (source_id,),
                )
                db.execute(
                    "DELETE FROM main.automation_global_sync WHERE source_id=?",
                    (source_id,),
                )

            if old is None:
                fields = _fields(
                    {
                        "activityName": item.get("activityName"),
                        "activityTypeDTO": kind,
                        "summaryDTO": item,
                    }
                )
                version = {
                    "fields": fields,
                    "file": None,
                    "file_hash": None,
                    "original": False,
                    "metadata_verified": False,
                    "origin": fields["summaryDTO"],
                }
                version["fingerprint"] = digest_json(version)
                db.execute(
                    f"INSERT INTO {schema}.automation_global_sync "
                    "(source_id,desired,updated_at) VALUES(?,?,?)",
                    (source_id, json.dumps(version, ensure_ascii=False), utcnow()),
                )
            elif (
                full
                or (
                    source_id not in reusable
                    and parse_time(item["startTimeGMT"]) >= cutoff
                )
                or json.loads(old["desired"])["fields"]["activityTypeDTO"]["typeKey"]
                != kind["typeKey"]
            ):
                db.execute(
                    f"UPDATE {schema}.automation_global_sync "
                    "SET refresh_requested=1 WHERE source_id=?",
                    (source_id,),
                )
