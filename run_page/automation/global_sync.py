"""Durable CN → international activity delivery, independent of page builds.

The queue and receipts live in the backed-up SQLite database. Every remote write
is preceded by a committed phase, so interrupted uploads are reconciled before
another POST. Existing activities are adopted, never treated as tool-owned.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import re
from pathlib import Path, PurePosixPath
import sqlite3
import time
import xml.etree.ElementTree as ET

from .activity_files import ActivityFile, MAX_ACTIVITY_BYTES
from .common import atomic_write, digest_file, digest_json, utcnow
from .garmin import parse_time

EDITABLE_STATS = (
    "distance",
    "duration",
    "movingDuration",
    "elapsedDuration",
    "averageSpeed",
    "averageHR",
    "maxHR",
    "elevationGain",
    "elevationLoss",
    "calories",
    "startTimeGMT",
    "startTimeLocal",
)


@contextmanager
def _connect(database):
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS automation_global_sync (
        source_id TEXT PRIMARY KEY, desired TEXT NOT NULL,
        confirmed TEXT, target_id TEXT, owned INTEGER NOT NULL DEFAULT 0,
        phase TEXT NOT NULL DEFAULT 'pending', attempt TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL, last_attempt TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS automation_global_account (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), account TEXT NOT NULL
    )""")
    db.commit()
    try:
        with db:
            yield db
    finally:
        db.close()


def _fields(summary):
    """Only activity fields belong here: never copy privacy, gear or ownership."""
    dto = summary.get("summaryDTO", {})
    kind = summary.get("activityTypeDTO", {})
    if not isinstance(kind.get("typeKey"), str) or "run" not in kind["typeKey"].lower():
        raise RuntimeError("International delivery requires a running activity type")
    if not dto.get("startTimeGMT") or dto.get("distance") is None:
        raise RuntimeError("Incomplete international activity metadata")
    result = {
        "activityName": str(summary.get("activityName") or ""),
        "description": summary.get("description") or "",
        "activityTypeDTO": {
            k: kind[k] for k in ("typeId", "typeKey", "parentTypeId") if k in kind
        },
        "summaryDTO": {k: dto[k] for k in EDITABLE_STATS if dto.get(k) is not None},
    }
    # Reject non-finite statistics before persisting a version or making writes.
    json.dumps(result, allow_nan=False)
    return result


def _path(snapshot, relative):
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 2:
        raise RuntimeError("Invalid original activity path")
    if rel.parts[0] not in {"FIT_OUT", "TCX_OUT", "GPX_OUT", "ORIGINAL_OUT"}:
        raise RuntimeError("Invalid original activity directory")
    path = Path(snapshot) / str(rel)
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError("Original activities must not be symlinks")
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_ACTIVITY_BYTES:
        raise RuntimeError("Original activity is missing or too large")
    return path


def _origin(path, fallback):
    """Original start survives manual edits to Garmin summary timestamps."""
    result = dict(fallback)
    if path.suffix.lower() == ".fit":
        from garmin_fit_sdk import Decoder, Stream
        from garmin_fit_sdk.util import FIT_EPOCH_S

        decoder = Decoder(Stream.from_file(str(path)))
        messages, errors = decoder.read(convert_datetimes_to_dates=False)
        sessions = messages.get("session_mesgs", [])
        if errors or len(sessions) != 1:
            raise RuntimeError("Original FIT must contain one complete session")
        session = sessions[0]
        if session.get("start_time") is not None:
            result["startTimeGMT"] = datetime.fromtimestamp(
                float(session["start_time"]) + FIT_EPOCH_S, timezone.utc
            ).isoformat()
        for source, target in (
            ("total_elapsed_time", "duration"),
            ("total_distance", "distance"),
        ):
            if session.get(source) is not None:
                result[target] = float(session[source])
    else:
        root = ET.fromstring(path.read_bytes())
        if path.suffix.lower() == ".tcx":
            start = root.findtext(".//{*}Activity/{*}Id")
        else:
            start = (
                root.findtext(".//{*}trkpt/{*}time")
                or root.findtext("{*}extensions/{*}start_time")
                or root.findtext("{*}metadata/{*}time")
            )
        if start:
            result["startTimeGMT"] = parse_time(start).isoformat()
    return {
        k: result[k] for k in ("startTimeGMT", "duration", "distance") if k in result
    }


def _version(snapshot, fields, relative, original, verified=True, *, trimmed=False):
    path = _path(snapshot, relative)
    value = {
        "fields": fields,
        "file": relative,
        "file_hash": digest_file(path),
        "original": bool(original),
        "metadata_verified": bool(verified),
        "trimmed": trimmed,
        "origin": _origin(path, fields["summaryDTO"]),
    }
    value["fingerprint"] = digest_json(value)
    return value


def enqueue_activity(
    database, source_id, summary, relative, *, original=True, verified=True
):
    """Stage one actual source version inside the disposable source snapshot."""
    if not re.fullmatch(r"[0-9]{1,20}", str(source_id)) or int(source_id) <= 0:
        raise RuntimeError("Invalid Garmin source activity ID")
    database = Path(database)
    desired = _version(
        database.parent.parent,
        _fields(summary),
        relative,
        original,
        verified,
        trimmed=(summary.get("metadataDTO") or {}).get("trimmed") is True,
    )
    with _connect(database) as db:
        old = db.execute(
            "SELECT * FROM automation_global_sync WHERE source_id=?", (str(source_id),)
        ).fetchone()
        if old and json.loads(old["desired"])["fingerprint"] == desired["fingerprint"]:
            return False
        encoded = json.dumps(desired, ensure_ascii=False, sort_keys=True)
        if old:
            phase = (
                old["phase"]
                if old["phase"] in {"uploading", "updating", "retiring"}
                else "pending"
            )
            db.execute(
                "UPDATE automation_global_sync SET desired=?,phase=?,error=?,updated_at=? WHERE source_id=?",
                (encoded, phase, "", utcnow(), str(source_id)),
            )
        else:
            db.execute(
                "INSERT INTO automation_global_sync(source_id,desired,updated_at) VALUES(?,?,?)",
                (str(source_id), encoded, utcnow()),
            )
    return True


def seed_history(config):
    """Queue every unmapped source, including files downloaded by older tools."""
    from generator.db import Activity, init_db
    from .garmin import source_rows

    snapshot = config.current
    database = snapshot / "run_page/data.db"
    with _connect(database) as db:
        known = {
            r[0] for r in db.execute("SELECT source_id FROM automation_global_sync")
        }
    session = init_db(str(database))
    try:
        activities = {str(row.run_id): row for row in session.query(Activity)}
        count = 0
        for source_id, source in source_rows(database).items():
            if source_id in known or source["ignored"]:
                continue
            row = activities.get(str(source["run_id"]))
            if row is None:
                continue
            relative, original = f"GPX_OUT/{source_id}.gpx", False
            for folder, extension in (
                ("FIT_OUT", "fit"),
                ("TCX_OUT", "tcx"),
                ("ORIGINAL_OUT", "gpx"),
            ):
                candidate = f"{folder}/{source_id}.{extension}"
                if (snapshot / candidate).is_file():
                    relative, original = candidate, True
                    break
            dto = {
                "startTimeGMT": parse_time(row.start_date).isoformat(),
                "startTimeLocal": row.start_date_local,
                "distance": row.distance,
                "duration": row.moving_time.total_seconds(),
                "movingDuration": row.moving_time.total_seconds(),
                "elapsedDuration": row.elapsed_time.total_seconds(),
                "averageSpeed": row.average_speed,
                "averageHR": row.average_heartrate,
                "elevationGain": row.elevation_gain,
            }
            # All deployed historic rows have been backfilled with source types.
            summary = {
                "activityName": row.name,
                "activityTypeDTO": {
                    "typeKey": (
                        row.subtype
                        if row.subtype and "run" in row.subtype.lower()
                        else "running"
                    )
                },
                "summaryDTO": dto,
            }
            enqueue_activity(
                database,
                source_id,
                summary,
                relative,
                original=original,
                verified=False,
            )
            count += 1
            if count % 50 == 0:
                logging.info("Queued %s historical Garmin activities", count)
        return count
    finally:
        session.close()


def sync_status(config):
    database = config.current / "run_page/data.db"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_global_sync'"
        ).fetchone():
            return {"enabled": config.global_sync, "counts": {}, "entries": []}
        db.row_factory = sqlite3.Row
        counts = dict(
            db.execute(
                "SELECT phase,count(*) FROM automation_global_sync GROUP BY phase"
            )
        )
        entries = [
            dict(r)
            for r in db.execute(
                "SELECT source_id,target_id,owned,phase,error,attempts FROM automation_global_sync "
                "ORDER BY CASE WHEN phase='blocked' THEN 0 WHEN error!='' THEN 1 "
                "WHEN phase='done' THEN 3 ELSE 2 END,source_id DESC LIMIT 100"
            )
        ]
        return {"enabled": config.global_sync, "counts": counts, "entries": entries}


def _write(database, source_id, **values):
    allowed = {
        "desired",
        "confirmed",
        "target_id",
        "owned",
        "phase",
        "attempt",
        "error",
        "attempts",
        "last_attempt",
    }
    if not values.keys() <= allowed:
        raise ValueError("Unknown delivery state field")
    values["updated_at"] = utcnow()
    with _connect(database) as db:
        db.execute(
            "UPDATE automation_global_sync SET "
            + ",".join(k + "=?" for k in values)
            + " WHERE source_id=?",
            (*values.values(), str(source_id)),
        )


class Blocked(RuntimeError):
    """A conflict requiring explicit selection, never a reason to drop a row."""


def _same_time(left, right, *, local=False):
    try:
        a, b = parse_time(left), parse_time(right)
        if local:
            a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
        return abs((a - b).total_seconds()) <= 1
    except (ValueError, TypeError):
        return False


def _matches(row, identity):
    kind = row.get("activityType") or {}
    if (
        not isinstance(kind, dict)
        or "run" not in str(kind.get("typeKey", "running")).lower()
    ):
        return False
    if not _same_time(row.get("startTimeGMT"), identity.get("startTimeGMT")):
        return False
    durations = [
        identity[k]
        for k in ("duration", "movingDuration", "elapsedDuration")
        if identity.get(k) is not None
    ]
    if row.get("duration") is None or not any(
        abs(float(row["duration"]) - float(d)) <= 2 for d in durations
    ):
        return False
    for key in ("distance",):
        if identity.get(key) is None or row.get(key) is None:
            return False
        tolerance = (
            2 if key == "duration" else max(5, abs(float(identity[key])) * 0.001)
        )
        if abs(float(row[key]) - float(identity[key])) > tolerance:
            return False
    return True


def _field_updates(actual, expected):
    changes = {}
    for key in ("activityName", "description"):
        if key in expected and (actual.get(key) or "") != expected[key]:
            changes[key] = expected[key]
    kind = expected.get("activityTypeDTO")
    if (
        kind is not None
        and (actual.get("activityTypeDTO") or {}).get("typeKey") != kind["typeKey"]
    ):
        changes["activityTypeDTO"] = kind
    dto = actual.get("summaryDTO") or {}
    stats = {}
    for key, value in expected.get("summaryDTO", {}).items():
        if key.startswith("startTime"):
            if not _same_time(dto.get(key), value, local=key == "startTimeLocal"):
                stats[key] = value
        else:
            try:
                tolerance = 0.02 if key == "averageSpeed" else 0.25
                if (
                    not math.isfinite(float(dto.get(key)))
                    or abs(float(dto[key]) - float(value)) > tolerance
                ):
                    stats[key] = value
            except (TypeError, ValueError):
                stats[key] = value
    if stats:
        changes["summaryDTO"] = stats
    return changes


def _matches_fields(actual, expected):
    return isinstance(actual, dict) and not _field_updates(actual, expected)


async def _patch(client, target_id, desired, confirmed=None):
    current = await client.summary(target_id)
    if current is None:
        raise RuntimeError(
            "International activity is not yet visible; retained for retry"
        )
    updates = _field_updates(current, desired["fields"])
    source_trimmed = desired.get("trimmed", False)
    target_trimmed = (current.get("metadataDTO") or {}).get("trimmed") is True
    boundary_changed = {"startTimeGMT", "elapsedDuration"}.intersection(
        updates.get("summaryDTO", {})
    )
    needs_effective_file = source_trimmed != target_trimmed or (
        source_trimmed and boundary_changed
    )
    target_file_hash = (confirmed or {}).get("target_file_hash")
    if target_file_hash:
        previous = confirmed["fields"]["summaryDTO"]
        bounds = desired["fields"]["summaryDTO"]
        unchanged_crop = (
            source_trimmed
            and not target_trimmed
            and desired["file_hash"] == confirmed["file_hash"]
            and bounds["startTimeGMT"] == previous["startTimeGMT"]
            and previous.get("elapsedDuration") is not None
            and bounds.get("elapsedDuration") == previous["elapsedDuration"]
            and not boundary_changed
        )
        needs_effective_file = True
        if unchanged_crop:
            # Garmin imports a physically clipped FIT without setting trimmed.
            original = await client.download(target_id)
            needs_effective_file = (
                hashlib.sha256(original.content).hexdigest() != target_file_hash
            )
    if needs_effective_file:
        text = {
            key: updates[key]
            for key in ("activityName", "description")
            if key in updates
        }
        if text:
            await client.update(target_id, text)
            if not _matches_fields(await client.summary(target_id), text):
                raise RuntimeError(
                    "International activity read-back verification failed"
                )
        raise Blocked("trimmed_activity_requires_effective_file")
    if updates:
        await client.update(target_id, updates)
        current = await client.summary(target_id)
    if not _matches_fields(current, desired["fields"]):
        raise RuntimeError("International activity read-back verification failed")
    return current


def _confirm(database, source_id, desired, target_id, owned):
    _write(
        database,
        source_id,
        confirmed=json.dumps(desired, sort_keys=True),
        target_id=str(target_id),
        owned=int(owned),
        phase="done",
        attempt="{}",
        error="",
    )


async def _retire(database, source_id, attempt, client, index):
    # Only the persisted ownership receipt can authorize retiring an old record.
    if not attempt.get("old_owned") or attempt["old_id"] == attempt["new_id"]:
        raise Blocked("replacement_ownership_not_proven")
    desired = attempt["version"]
    current = await _patch(client, attempt["new_id"], desired)
    original = await client.download(attempt["new_id"])
    if hashlib.sha256(original.content).hexdigest() != desired["file_hash"]:
        raise Blocked("replacement_original_verification_failed")
    if await client.summary(attempt["old_id"]) is not None:
        old_original = await client.download(attempt["old_id"])
        if hashlib.sha256(old_original.content).hexdigest() != attempt["old_file_hash"]:
            raise Blocked("previous_owned_activity_was_replaced_externally")
        await client.delete(attempt["old_id"])
    if await client.summary(attempt["old_id"]) is not None:
        raise RuntimeError("Previous activity deletion is not verified")
    _confirm(database, source_id, desired, attempt["new_id"], attempt["new_owned"])
    index[:] = [
        r
        for r in index
        if str(r["activityId"]) not in {str(attempt["old_id"]), str(attempt["new_id"])}
    ]
    index.append(
        dict(
            current["summaryDTO"],
            activityId=attempt["new_id"],
            activityType=current["activityTypeDTO"],
        )
    )
    _requeue_newer_version(database, source_id, desired)


def _requeue_newer_version(database, source_id, desired):
    # Newer source versions may have arrived while recovering an interrupted job.
    with _connect(database) as db:
        latest = json.loads(
            db.execute(
                "SELECT desired FROM automation_global_sync WHERE source_id=?",
                (source_id,),
            ).fetchone()[0]
        )
    if latest["fingerprint"] != desired["fingerprint"]:
        _write(database, source_id, phase="pending")


async def _confirm_update(
    database, source_id, target_id, owned, desired, client, index, confirmed=None
):
    current = await _patch(client, target_id, desired, confirmed)
    target_file_hash = (confirmed or {}).get("target_file_hash")
    if target_file_hash:
        desired = {**desired, "target_file_hash": target_file_hash}
    _confirm(database, source_id, desired, target_id, owned)
    index[:] = [r for r in index if str(r["activityId"]) != target_id]
    index.append(
        dict(
            current["summaryDTO"],
            activityId=target_id,
            activityType=current["activityTypeDTO"],
        )
    )


async def _confirm_upload(database, source_id, attempt, client, index):
    await _confirm_update(
        database,
        source_id,
        attempt["new_id"],
        attempt["new_owned"],
        attempt["version"],
        client,
        index,
    )
    _requeue_newer_version(database, source_id, attempt["version"])


async def _finish_upload(database, source_id, attempt, result, client, index):
    if not result.get("id"):
        raise RuntimeError("Upload needs reconciliation before another attempt")
    new_id = str(result["id"])
    preexisting = (
        attempt.get("receipt_preexisting", False)
        if "receipt" in attempt
        else any(str(r["activityId"]) == new_id for r in index)
    )
    new_owned = bool(result.get("created")) and not preexisting
    attempt.update(new_id=new_id, new_owned=new_owned)
    old_id, old_hash = attempt["old_id"], attempt["old_file_hash"]
    if old_id and old_hash and old_hash != attempt["version"]["file_hash"]:
        if new_id == old_id or not new_owned:
            raise Blocked("server_refused_non_destructive_replacement")
        _write(
            database,
            source_id,
            phase="retiring",
            attempt=json.dumps(attempt, sort_keys=True),
        )
        await _retire(database, source_id, attempt, client, index)
        return
    _write(
        database,
        source_id,
        target_id=new_id,
        owned=int(new_owned),
        phase="updating",
        attempt=json.dumps(attempt, sort_keys=True),
    )
    await _confirm_upload(database, source_id, attempt, client, index)


async def _poll_upload(database, source_id, attempt, client, index):
    for delay in (0, 1, 2, 4):
        if delay:
            await asyncio.sleep(delay)
        result = await client.upload_status(attempt["receipt"])
        if result is not None:
            await _finish_upload(database, source_id, attempt, result, client, index)
            return


async def _ensure_original(config, row, desired, source_client):
    if (
        desired["original"]
        and desired.get("metadata_verified", True)
        and "trimmed" in desired
    ):
        return desired
    if source_client is None:
        raise RuntimeError(
            "Original Garmin file requires an authenticated source client"
        )
    source_id = row["source_id"]
    fields = desired["fields"]
    trimmed = desired.get("trimmed", False)
    if hasattr(source_client, "summary"):
        summary = await source_client.summary(source_id)
        if str(summary.get("activityId")) != source_id:
            raise RuntimeError("Original activity summary identity mismatch")
        fields = _fields(summary)
        trimmed = (summary.get("metadataDTO") or {}).get("trimmed") is True
    if desired["original"]:
        desired = _version(
            config.current, fields, desired["file"], True, trimmed=trimmed
        )
        _write(
            config.current / "run_page/data.db",
            source_id,
            desired=json.dumps(desired, sort_keys=True),
        )
        return desired
    original = await source_client.download(source_id)
    if not isinstance(original, ActivityFile):
        original = ActivityFile("gpx", original)
    original.gpx()  # Fully validate before writing or uploading the cache.
    folder = {"fit": "FIT_OUT", "tcx": "TCX_OUT", "gpx": "ORIGINAL_OUT"}[
        original.format
    ]
    relative = f"{folder}/{source_id}.{original.format}"
    atomic_write(config.current / relative, original.content)
    desired = _version(config.current, fields, relative, True, trimmed=trimmed)
    _write(
        config.current / "run_page/data.db",
        source_id,
        desired=json.dumps(desired, sort_keys=True),
    )
    return desired


async def _process(config, database, row, client, index, source_client):
    source_id = row["source_id"]
    desired = json.loads(row["desired"])
    confirmed = json.loads(row["confirmed"]) if row["confirmed"] else None
    attempt = json.loads(row["attempt"])
    if row["phase"] == "retiring":
        await _retire(database, source_id, attempt, client, index)
        return
    if row["phase"] == "updating" and attempt.get("new_id"):
        await _confirm_upload(database, source_id, attempt, client, index)
        return
    if row["phase"] == "uploading" and attempt.get("receipt"):
        await _poll_upload(database, source_id, attempt, client, index)
        return
    desired = await _ensure_original(config, row, desired, source_client)
    target_id, owned = row["target_id"], bool(row["owned"])
    replacement = bool(
        target_id and confirmed and confirmed["file_hash"] != desired["file_hash"]
    )
    if replacement:
        # Metadata changes can still be applied to the retained activity.
        await _patch(client, target_id, desired, confirmed)
        if not owned:
            raise Blocked("original_changed_on_adopted_activity")
    if not target_id or replacement or row["phase"] == "uploading":
        identities = [desired["origin"], desired["fields"]["summaryDTO"]]
        if attempt.get("version"):
            identities.extend(
                (
                    attempt["version"]["origin"],
                    attempt["version"]["fields"]["summaryDTO"],
                )
            )
        candidates = [
            r
            for r in index
            if any(_matches(r, i) for i in identities)
            and (not replacement or str(r["activityId"]) != str(target_id))
        ]
        unique = {str(r["activityId"]) for r in candidates}
        if not unique and not target_id:
            same_start = {
                str(item["activityId"])
                for item in index
                if any(
                    _same_time(item.get("startTimeGMT"), identity.get("startTimeGMT"))
                    for identity in identities
                )
            }
            known_hashes = {
                desired["file_hash"],
                attempt.get("version", desired)["file_hash"],
            }
            for candidate in sorted(same_start):
                original = await client.download(candidate)
                if hashlib.sha256(original.content).hexdigest() in known_hashes:
                    unique.add(candidate)
            if same_start and not unique:
                raise Blocked("unverified_same_start_destination")
        if len(unique) > 1:
            raise Blocked("ambiguous_destination_match")
        if unique:
            matched_id = unique.pop()
            if replacement:
                attempt = {
                    "old_id": target_id,
                    "old_owned": owned,
                    "old_file_hash": confirmed["file_hash"],
                    "new_id": matched_id,
                    "new_owned": (
                        bool(attempt.get("new_owned"))
                        if attempt.get("new_id") == matched_id
                        else False
                    ),
                    "version": desired,
                }
                _write(
                    database,
                    source_id,
                    phase="retiring",
                    attempt=json.dumps(attempt, sort_keys=True),
                )
                await _retire(database, source_id, attempt, client, index)
                return
            target_id, owned = matched_id, False
            _write(database, source_id, target_id=target_id, owned=0, phase="updating")
            if (
                attempt.get("version")
                and attempt["version"]["file_hash"] != desired["file_hash"]
            ):
                # An earlier, response-lost upload cannot confirm a newer file.
                await _patch(client, target_id, desired)
                _write(
                    database,
                    source_id,
                    confirmed=json.dumps(attempt["version"], sort_keys=True),
                )
                raise Blocked("source_changed_during_unknown_upload")
        else:
            if row["phase"] == "uploading" and attempt.get("started_at"):
                if (
                    datetime.now(timezone.utc) - parse_time(attempt["started_at"])
                ).total_seconds() < 300:
                    return  # Allow an accepted upload to appear before retrying a POST.
            desired = await _ensure_original(config, row, desired, source_client)
            if desired.get("trimmed"):
                raise Blocked("trimmed_activity_requires_effective_file")
            path = _path(config.current, desired["file"])
            if digest_file(path) != desired["file_hash"]:
                raise RuntimeError("Queued original activity changed before upload")
            attempt = {
                "started_at": utcnow(),
                "version": desired,
                "old_id": target_id,
                "old_owned": owned,
                "old_file_hash": confirmed["file_hash"] if confirmed else None,
            }
            _write(
                database,
                source_id,
                phase="uploading",
                attempt=json.dumps(attempt, sort_keys=True),
            )
            result = await client.upload(path)
            if result.get("receipt"):
                attempt["receipt"] = result["receipt"]
                attempt["receipt_preexisting"] = any(
                    str(item["activityId"]) == result.get("id") for item in index
                )
                _write(
                    database,
                    source_id,
                    attempt=json.dumps(attempt, sort_keys=True),
                )
                await _poll_upload(database, source_id, attempt, client, index)
            else:
                await _finish_upload(
                    database, source_id, attempt, result, client, index
                )
            return
    await _confirm_update(
        database, source_id, target_id, owned, desired, client, index, confirmed
    )


async def run_sync(
    config,
    client,
    *,
    source_client=None,
    dry_run=False,
    limit=None,
    retry_blocked=False,
):
    database = config.current / "run_page/data.db"
    with _connect(database) as db:
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM automation_global_sync WHERE phase!='done' ORDER BY CASE WHEN last_attempt IS NULL THEN 1 ELSE 0 END,last_attempt,source_id DESC"
            )
        ]
    rows.sort(
        key=lambda r: (
            parse_time(json.loads(r["desired"])["fields"]["summaryDTO"]["startTimeGMT"])
            < datetime.now(timezone.utc) - timedelta(days=config.days),
            r["last_attempt"] is not None,
            r["last_attempt"] or "",
            -parse_time(
                json.loads(r["desired"])["fields"]["summaryDTO"]["startTimeGMT"]
            ).timestamp(),
        )
    )
    if not rows and not dry_run:
        return {"processed": 0, **sync_status(config)}
    account = await client.identity()
    with _connect(database) as db:
        bound = db.execute(
            "SELECT account FROM automation_global_account WHERE singleton=1"
        ).fetchone()
        if bound and bound[0] != account:
            raise RuntimeError(
                "International account differs from the bound destination account"
            )
        if not bound and not dry_run:
            db.execute("INSERT INTO automation_global_account VALUES(1,?)", (account,))
    index = await client.activities()  # Complete pagination is required before writes.
    if dry_run:
        plan = Counter()
        for row in rows:
            desired = json.loads(row["desired"])
            if row["target_id"]:
                plan["mapped_pending"] += 1
                continue
            identities = (desired["origin"], desired["fields"]["summaryDTO"])
            matches = {
                str(item["activityId"])
                for item in index
                if any(_matches(item, identity) for identity in identities)
            }
            if len(matches) == 1:
                plan["existing_match"] += 1
            elif len(matches) > 1:
                plan["ambiguous_match"] += 1
            elif any(
                _same_time(item.get("startTimeGMT"), identity.get("startTimeGMT"))
                for item in index
                for identity in identities
            ):
                plan["needs_source_verification"] += 1
            else:
                plan["new_candidate"] += 1
        return {
            "dry_run": True,
            "queued": len(rows),
            "destination_activities": len(index),
            "plan": dict(plan),
            **sync_status(config),
        }
    processed, failed = 0, 0
    started = time.monotonic()
    for row in rows:
        if (
            processed >= (limit or config.global_sync_limit)
            or time.monotonic() - started >= 600
        ):
            break
        if row["phase"] == "blocked" and not retry_blocked:
            continue
        _write(
            database,
            row["source_id"],
            attempts=row["attempts"] + 1,
            last_attempt=utcnow(),
            error="",
        )
        logging.info(
            "Delivering CN activity %s to international account", row["source_id"]
        )
        try:
            async with asyncio.timeout(max(1, 600 - (time.monotonic() - started))):
                await _process(config, database, row, client, index, source_client)
        except Blocked as exc:
            failed += 1
            logging.warning(
                "International delivery blocked for source %s: %s",
                row["source_id"],
                str(exc),
            )
            _write(database, row["source_id"], phase="blocked", error=str(exc))
        except Exception:
            failed += 1
            # Preserve uploading/retiring phases: a timeout is not proof that a
            # write failed. The next round must reconcile the unknown result.
            _write(database, row["source_id"], error="international_delivery_failed")
            logging.warning(
                "International delivery incomplete for source %s; retained for retry",
                row["source_id"],
            )
        processed += 1
    return {"processed": processed, "failed": failed, **sync_status(config)}


def deliver(config, *, backfill=False, dry_run=False, limit=None, retry_blocked=False):
    """CLI/pipeline interface. Call while holding the existing runner lock."""
    if config.garmin_region != "cn":
        raise RuntimeError("International delivery requires a CN source account")
    from .common import write_json
    from .global_client import GlobalClient
    from .garmin import create_client

    class LazySource:
        client = None

        async def download(self, source_id):
            if self.client is None:
                self.client = create_client(config)
            return await self.client.download(source_id)

        async def summary(self, source_id):
            if self.client is None:
                self.client = create_client(config)
            return await self.client.summary(source_id)

        async def close(self):
            if self.client is not None:
                await self.client.close()

    if backfill:
        seed_history(config)

    async def execute():
        source = LazySource()
        client = GlobalClient(config)
        try:
            return await run_sync(
                config,
                client,
                source_client=source,
                dry_run=dry_run,
                limit=limit,
                retry_blocked=retry_blocked,
            )
        finally:
            await client.close()
            await source.close()

    result = asyncio.run(execute())
    if not dry_run:
        write_json(config.state / "global-sync.json", {"last_run": utcnow(), **result})
    return result


def link_activity(config, source_id, target_id):
    """Explicitly resolve an ambiguous match; adoption never grants ownership."""
    from .global_client import GlobalClient, activity_id

    database = config.current / "run_page/data.db"
    target_id = activity_id(target_id)
    with _connect(database) as db:
        row = db.execute(
            "SELECT * FROM automation_global_sync WHERE source_id=?", (str(source_id),)
        ).fetchone()
    if row is None:
        raise RuntimeError(
            "Source activity is not queued; run global-sync --backfill --dry-run first"
        )
    desired = json.loads(row["desired"])

    async def check():
        client = GlobalClient(config)
        try:
            account = await client.identity()
            with _connect(database) as db:
                bound = db.execute(
                    "SELECT account FROM automation_global_account WHERE singleton=1"
                ).fetchone()
                if bound and bound[0] != account:
                    raise RuntimeError(
                        "International account differs from the bound destination account"
                    )
            target = await client.summary(target_id)
            if target is None:
                raise RuntimeError("Selected international activity does not exist")
            if not any(
                _same_time(
                    target.get("summaryDTO", {}).get("startTimeGMT"),
                    candidate.get("startTimeGMT"),
                )
                for candidate in (desired["origin"], desired["fields"]["summaryDTO"])
            ):
                raise RuntimeError(
                    "Selected international activity has a different start time"
                )
        finally:
            await client.close()

    asyncio.run(check())
    _write(
        database,
        str(source_id),
        target_id=target_id,
        owned=0,
        phase="pending",
        attempt="{}",
        error="",
    )
    return {"source_id": str(source_id), "target_id": target_id, "owned": False}
