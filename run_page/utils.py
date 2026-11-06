import json
import time
from datetime import datetime
from pathlib import Path

import pytz

try:
    from rich import print
except Exception:
    pass
from generator import Generator
from stravalib.client import Client
from stravalib.exc import RateLimitExceeded

ACTIVITY_CHUNKS_DIR_NAME = "activity_chunks"
ACTIVITY_CHUNKS_MANIFEST = "manifest.json"
ACTIVITY_CHUNKS_VERSION = 1


def adjust_time(time, tz_name):
    tc_offset = datetime.now(pytz.timezone(tz_name)).utcoffset()
    return time + tc_offset


def adjust_time_to_utc(time, tz_name):
    tc_offset = datetime.now(pytz.timezone(tz_name)).utcoffset()
    return time - tc_offset


def adjust_timestamp_to_utc(timestamp, tz_name):
    tc_offset = datetime.now(pytz.timezone(tz_name)).utcoffset()
    delta = int(tc_offset.total_seconds())
    return int(timestamp) - delta


def to_date(ts):
    """
    Parse ISO format timestamp string to datetime object.
    Uses datetime.fromisoformat() for standard ISO format strings.
    Falls back to strptime for non-standard formats.
    """
    # Try fromisoformat first (Python 3.7+)
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        # Fallback to strptime for non-standard formats
        ts_fmts = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"]
        for ts_fmt in ts_fmts:
            try:
                return datetime.strptime(ts, ts_fmt)
            except ValueError:
                pass
        raise ValueError(f"cannot parse timestamp {ts} into date")


def write_activities_files(activities_list, json_file):
    json_path = Path(json_file)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w") as f:
        json.dump(activities_list, f)

    chunks_dir = json_path.parent / ACTIVITY_CHUNKS_DIR_NAME
    chunks_dir.mkdir(parents=True, exist_ok=True)
    for stale_chunk in chunks_dir.glob("year_*.json"):
        stale_chunk.unlink()

    activities_by_year = {}
    for activity in activities_list:
        year = activity.get("start_date_local", "")[:4]
        if not year:
            continue
        activities_by_year.setdefault(year, []).append(activity)

    years = sorted(activities_by_year.keys(), reverse=True)
    manifest_years = []
    for year in years:
        year_activities = activities_by_year[year]
        chunk_file = f"year_{year}.json"
        with open(chunks_dir / chunk_file, "w") as f:
            json.dump(year_activities, f)
        manifest_years.append(
            {
                "year": year,
                "file": chunk_file,
                "count": len(year_activities),
                "run_ids": [activity["run_id"] for activity in year_activities],
                "first_start_date_local": year_activities[0].get("start_date_local"),
                "last_start_date_local": year_activities[-1].get("start_date_local"),
            }
        )

    manifest = {
        "version": ACTIVITY_CHUNKS_VERSION,
        "total_count": len(activities_list),
        "years": manifest_years,
    }
    with open(chunks_dir / ACTIVITY_CHUNKS_MANIFEST, "w") as f:
        json.dump(manifest, f)


def make_activities_file(
    sql_file, data_dir, json_file, file_suffix="gpx", activity_title_dict={}
):
    generator = Generator(sql_file)
    generator.sync_from_data_dir(
        data_dir, file_suffix=file_suffix, activity_title_dict=activity_title_dict
    )
    activities_list = generator.load()
    write_activities_files(activities_list, json_file)


def make_strava_client(client_id, client_secret, refresh_token):
    client = Client()

    refresh_response = client.refresh_access_token(
        client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
    )
    client.access_token = refresh_response["access_token"]
    return client


def get_strava_last_time(client, is_milliseconds=True):
    """
    if there is no activities cause exception return 0
    """
    try:
        activity = None
        activities = client.get_activities(limit=10)
        activities = list(activities)
        activities.sort(key=lambda x: x.start_date, reverse=True)
        # for else in python if you don't know please google it.
        for a in activities:
            if a.type == "Run":
                activity = a
                break
        else:
            return 0
        end_date = activity.start_date + activity.elapsed_time
        last_time = int(datetime.timestamp(end_date))
        if is_milliseconds:
            last_time = last_time * 1000
        return last_time
    except Exception as e:
        print(f"Something wrong to get last time err: {str(e)}")
        return 0


def upload_file_to_strava(client, file_name, data_type, force_to_run=True):
    with open(file_name, "rb") as f:
        try:
            if force_to_run:
                r = client.upload_activity(
                    activity_file=f, data_type=data_type, activity_type="run"
                )
            else:
                r = client.upload_activity(activity_file=f, data_type=data_type)

        except RateLimitExceeded as e:
            timeout = e.timeout
            print(f"Strava API Rate Limit Exceeded. Retry after {timeout} seconds")
            time.sleep(timeout)
            if force_to_run:
                r = client.upload_activity(
                    activity_file=f, data_type=data_type, activity_type="run"
                )
            else:
                r = client.upload_activity(activity_file=f, data_type=data_type)
        print(
            f"Uploading {data_type} file: {file_name} to strava, upload_id: {r.upload_id}."
        )
