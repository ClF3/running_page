"""Personal API authentication and real FIT decoding without remote credentials."""

import asyncio
import base64
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import httpx

from run_page.automation.activity_files import ActivityFile, unpack_activity
from run_page.automation.common import Config, read_json
from run_page.automation.garmin import authentication_status, create_client
from run_page.automation.personal_api import PersonalApiClient, TOKEN_PATH
from run_page.automation.pipeline import (
    bootstrap,
    make_backup,
    restore_archive,
    synchronize,
)
from tests.test_automation import GPX, RUN_ID, START, seed, summary


def jwt(expires=None, suffix="signature"):
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"exp": expires or time.time() + 86400}).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"header.{payload}.{suffix}"


def activity_zip(raw, filename="activity.fit"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, raw)
    return buffer.getvalue()


def real_fit(*, indoor=False, hour=0):
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.profile_type import Sport

    start = int((START + timedelta(hours=hour)).timestamp() * 1000)
    builder = FitFileBuilder()
    session = SessionMessage()
    session.start_time, session.timestamp = start, start + 300000
    session.total_elapsed_time, session.total_timer_time = 300, 290
    session.total_distance, session.sport = 1100, Sport.RUNNING
    builder.add(session)
    if not indoor:
        for offset, lat, lon in ((0, 0.0, 0.0), (7000, 0.001, 0.001)):
            record = RecordMessage()
            record.timestamp = start + offset
            record.position_lat, record.position_long = lat, lon
            record.altitude, record.heart_rate = 0, 150
            builder.add(record)
    return bytes(builder.build().to_bytes())


class PersonalApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def client(self, handler, *, key="test-personal-key", region="cn"):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        return PersonalApiClient(self.state, region, api_key=key, http=http)

    async def test_key_exchange_and_private_cache_survive_a_new_process(self):
        token, calls = jwt(), []

        def respond(request):
            calls.append(request)
            self.assertEqual(request.url.host, "personal.connectapi.garmin.cn")
            if request.method == "POST":
                self.assertEqual(request.url.path, TOKEN_PATH)
                self.assertEqual(
                    request.headers["Authorization"], "ApiKey test-personal-key"
                )
                return httpx.Response(200, text=token)
            self.assertEqual(request.headers["Authorization"], "Bearer " + token)
            return httpx.Response(200, json={"ok": True})

        client = self.client(respond)
        self.assertEqual(await client.request("/test"), {"ok": True})
        restarted = self.client(respond)
        self.assertEqual(await restarted.request("/test"), {"ok": True})
        self.assertEqual([r.method for r in calls], ["POST", "GET", "GET"])
        self.assertEqual(client.cache_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("test-personal-key", client.cache_path.read_text())

    async def test_token_near_expiry_is_replaced(self):
        tokens = [jwt(time.time() + 60), jwt()]
        posts = []

        def respond(request):
            if request.method == "POST":
                posts.append(request)
                return httpx.Response(200, text=tokens[len(posts) - 1])
            self.assertEqual(request.headers["Authorization"], "Bearer " + tokens[1])
            return httpx.Response(200, json=[])

        first = self.client(respond)
        await first.obtain_token()
        restarted = self.client(respond)
        self.assertEqual(await restarted.request("/test"), [])
        self.assertEqual(len(posts), 2)

    async def test_key_rotation_and_region_change_never_reuse_another_token(self):
        exchanges = []

        def respond(request):
            self.assertEqual(request.method, "POST")
            exchanges.append((request.url.host, request.headers["Authorization"]))
            return httpx.Response(200, text=jwt())

        await self.client(respond).obtain_token()
        await self.client(respond, key="new-personal-key").obtain_token()
        await self.client(
            respond, key="new-personal-key", region="global"
        ).obtain_token()
        self.assertEqual(
            exchanges,
            [
                ("personal.connectapi.garmin.cn", "ApiKey test-personal-key"),
                ("personal.connectapi.garmin.cn", "ApiKey new-personal-key"),
                ("personal.connectapi.garmin.com", "ApiKey new-personal-key"),
            ],
        )

    async def test_unauthorized_data_request_gets_one_new_token_then_succeeds(self):
        tokens, gets, posts = [jwt(suffix="first"), jwt(suffix="second")], [], []

        def respond(request):
            if request.method == "POST":
                posts.append(request)
                return httpx.Response(200, text=tokens[len(posts) - 1])
            gets.append(request)
            return httpx.Response(401 if len(gets) == 1 else 200, json={"ok": True})

        self.assertEqual(await self.client(respond).request("/test"), {"ok": True})
        self.assertEqual(len(posts), 2)
        self.assertEqual(
            [r.headers["Authorization"] for r in gets], ["Bearer " + t for t in tokens]
        )

    async def test_repeated_unauthorized_response_stops_after_one_renewal(self):
        calls = []

        def respond(request):
            calls.append(request)
            return (
                httpx.Response(200, text=jwt())
                if request.method == "POST"
                else httpx.Response(401)
            )

        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            await self.client(respond).request("/test")
        self.assertEqual([r.method for r in calls], ["POST", "GET", "POST", "GET"])

    async def test_transient_token_error_and_rate_limit_are_retried(self):
        calls = []

        def respond(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(503)
            if request.method == "POST":
                return httpx.Response(200, text=jwt())
            if len(calls) == 3:
                return httpx.Response(429, headers={"Retry-After": "999"})
            return httpx.Response(200, json={"ok": True})

        with patch("asyncio.sleep") as sleep:
            self.assertEqual(await self.client(respond).request("/test"), {"ok": True})
        self.assertEqual([x.args[0] for x in sleep.await_args_list], [2, 120])

    async def test_transport_failures_have_a_bounded_retry(self):
        calls = []

        def respond(request):
            calls.append(request)
            raise httpx.ConnectError("network unavailable")

        with patch("asyncio.sleep"):
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
                await self.client(respond).request("/test")
        self.assertEqual(len(calls), 3)

    async def test_whole_response_timeout_closes_stalled_stream_and_retries(self):
        class StalledStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b"["
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        stalled, gets = StalledStream(), []

        def respond(request):
            if request.method == "POST":
                return httpx.Response(200, text=jwt())
            gets.append(request)
            if len(gets) == 1:
                return httpx.Response(200, stream=stalled)
            return httpx.Response(200, json=[])

        client = self.client(respond)
        await client.obtain_token()
        client.request_timeout = 0.1
        with patch("asyncio.sleep"):
            self.assertEqual(await client.request("/test"), [])
        self.assertTrue(stalled.closed)
        self.assertEqual(len(gets), 2)

    async def test_errors_do_not_expose_key_or_server_response_body(self):
        secret = "test-personal-key"
        client = self.client(
            lambda request: httpx.Response(401, text="secret=" + secret)
        )
        with self.assertRaises(RuntimeError) as failure:
            await client.request("/test")
        self.assertNotIn(secret, str(failure.exception))
        self.assertNotIn(secret, repr(client))
        self.assertFalse(client.cache_path.exists())

    async def test_invalid_and_expired_tokens_are_not_cached(self):
        for token in ("not-a-jwt", jwt(time.time() - 1), "header.bm90anNvbg.signature"):
            client = self.client(lambda request: httpx.Response(200, text=token))
            with self.assertRaises(RuntimeError):
                await client.obtain_token()
            self.assertFalse(client.cache_path.exists())

    async def test_original_download_accepts_garmin_zip_content_type(self):
        def respond(request):
            if request.method == "POST":
                return httpx.Response(200, text=jwt())
            if "application/x-zip-compressed" not in request.headers["Accept"]:
                return httpx.Response(406)
            return httpx.Response(200, content=activity_zip(GPX, "activity.gpx"))

        self.assertEqual(
            await self.client(respond).download("100"), ActivityFile("gpx", GPX)
        )

    async def test_corrupt_token_cache_is_replaced(self):
        path = self.state / "private/personal-api-token.json"
        path.parent.mkdir()
        path.write_text("incomplete-json")
        client = self.client(lambda request: httpx.Response(200, text=jwt()))
        await client.obtain_token()
        self.assertIn("access_token", read_json(path))

    async def test_response_size_is_checked_while_streaming(self):
        client = self.client(lambda request: httpx.Response(200, content=b"123456789"))
        with self.assertRaisesRegex(RuntimeError, "size limit"):
            await client._send("GET", "/test", {}, limit=8)

    async def test_activity_pagination_uses_supported_page_size_without_losing_history(
        self,
    ):
        starts = []
        rows = [{"activityId": number} for number in range(1, 56)]

        def respond(request):
            if request.method == "POST":
                return httpx.Response(200, text=jwt())
            size = int(request.url.params["limit"])
            if size > 20:
                return httpx.Response(500)
            start = int(request.url.params["start"])
            starts.append(start)
            return httpx.Response(200, json=rows[start : start + size])

        self.assertEqual(await self.client(respond).activities(), rows)
        self.assertEqual(starts, [0, 20, 40, 60])

    async def test_personal_key_is_preferred_over_an_existing_legacy_session(self):
        cfg = Config(state=self.state)
        with patch.dict(
            os.environ,
            {
                "GARMIN_PERSONAL_API_KEY": "official-key",
                "GARMIN_SECRET_STRING_CN": "old-session",
            },
        ):
            self.assertEqual(
                authentication_status(cfg), {"method": "personal_api", "region": "cn"}
            )
            with patch("run_page.automation.garmin.GarminClient") as legacy:
                client = create_client(cfg)
                self.addAsyncCleanup(client.close)
                self.assertIsInstance(client, PersonalApiClient)
                legacy.assert_not_called()


class ActivityFilesTest(unittest.TestCase):
    def test_original_gpx_is_read_without_using_the_archive_filename(self):
        activity = unpack_activity(activity_zip(GPX, "nested/Original.GPX"))
        self.assertEqual(activity, ActivityFile("gpx", GPX))

    def test_unsafe_ambiguous_empty_and_corrupt_archives_are_rejected(self):
        for name in ("../escape.fit", "/escape.fit", "..\\escape.fit"):
            with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                unpack_activity(activity_zip(b"data", name))
        with self.assertRaises(RuntimeError):
            unpack_activity(activity_zip(b"", "empty.fit"))
        with self.assertRaises(RuntimeError):
            unpack_activity(b"truncated ZIP")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("first.fit", b"one")
            archive.writestr("second.fit", b"two")
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            unpack_activity(out.getvalue())

    def test_decompressed_activity_size_is_bounded(self):
        with patch("run_page.automation.activity_files.MAX_ACTIVITY_BYTES", 8):
            with self.assertRaisesRegex(RuntimeError, "64 MiB"):
                unpack_activity(activity_zip(b"123456789"))

    def test_real_fit_keeps_gps_time_elevation_heart_rate_and_zero_coordinates(self):
        raw = real_fit()
        activity = unpack_activity(activity_zip(raw))
        self.assertEqual(activity.content, raw)
        root = ET.fromstring(activity.gpx())
        points = root.findall(".//{*}trkpt")
        self.assertEqual(len(points), 2)
        self.assertAlmostEqual(float(points[0].attrib["lat"]), 0)
        self.assertAlmostEqual(float(points[1].attrib["lat"]), 0.001, places=6)
        self.assertEqual(points[0].findtext("{*}time"), START.isoformat())
        self.assertEqual(float(points[0].findtext("{*}ele")), 0)
        self.assertEqual(
            points[0].findtext("{*}extensions/{*}TrackPointExtension/{*}hr"), "150"
        )

    def test_fit_crc_error_cannot_create_a_partial_track(self):
        raw = bytearray(real_fit())
        raw[-1] ^= 1
        with self.assertRaises(RuntimeError):
            ActivityFile("fit", bytes(raw)).gpx()

    def test_tcx_route_can_be_imported_through_the_existing_gpx_pipeline(self):
        content = b"""<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
        <Activities><Activity Sport="Running"><Lap><Track><Trackpoint>
        <Time>2026-11-06T00:00:00Z</Time><Position><LatitudeDegrees>0</LatitudeDegrees>
        <LongitudeDegrees>0</LongitudeDegrees></Position><AltitudeMeters>0</AltitudeMeters>
        <HeartRateBpm><Value>150</Value></HeartRateBpm></Trackpoint></Track></Lap></Activity></Activities>
        </TrainingCenterDatabase>"""
        result = unpack_activity(activity_zip(content, "original.tcx")).gpx()
        points = ET.fromstring(result).findall(".//{*}trkpt")
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].findtext("{*}time"), START.isoformat())


class PersonalPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.seed = self.root / "source", self.root / "seed"
        self.source.mkdir()
        (self.source / "app.py").write_text("print('version 1')")
        seed(self.seed)
        self.cfg = Config(source=self.source, state=self.root / "state")
        bootstrap(self.cfg, self.seed)

    def api_client(
        self, *, fail=False, indoor=False, new=False, activity_type="running"
    ):
        self.original = real_fit(indoor=indoor)
        records = ["100", "200"] if fail or new else ["100"]

        def respond(request):
            path = request.url.path
            if path == TOKEN_PATH:
                return httpx.Response(200, text=jwt())
            if "search/activities" in path:
                rows = [
                    {"activityId": int(i), "startTimeGMT": START.isoformat()}
                    for i in records
                ]
                return httpx.Response(
                    200, json=rows if request.url.params["start"] == "0" else []
                )
            source_id = path.rsplit("/", 1)[-1]
            if path.startswith("/activity-service/"):
                dto = summary(source_id, hour=1 if source_id == "200" else 0)
                dto["activityTypeDTO"]["typeKey"] = activity_type
                return httpx.Response(200, json=dto)
            if path.startswith("/download-service/files/"):
                raw = (
                    b"broken FIT"
                    if fail and source_id == "200"
                    else (real_fit(hour=1) if source_id == "200" else self.original)
                )
                return httpx.Response(200, content=activity_zip(raw))
            raise AssertionError(path)

        return PersonalApiClient(
            self.cfg.state,
            api_key="key-not-in-backup",
            http=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )

    def sync(self, **kwargs):
        client = self.api_client(**kwargs)
        with (
            patch("run_page.automation.pipeline.create_client", return_value=client),
            patch("generator.db.g.reverse", return_value="Test location"),
        ):
            synchronize(self.cfg, full=True)

    def test_key_to_fit_to_database_keeps_ids_and_backups_exclude_private_tokens(self):
        self.sync(new=True)
        rows = read_json(self.cfg.current / "src/static/activities.json")
        self.assertEqual(len(rows), 2)
        old = next(row for row in rows if row["run_id"] == RUN_ID)
        self.assertEqual(old["distance"], 1100)
        self.assertEqual(old["average_heartrate"], 155)
        self.assertTrue(old["summary_polyline"])
        self.assertEqual(
            (self.cfg.current / "FIT_OUT/100.fit").read_bytes(), self.original
        )
        self.assertIn("100.fit", read_json(self.cfg.current / "imported.json"))
        make_backup(self.cfg, local_only=True)
        recovered = self.root / "recovered"
        self.assertEqual(
            restore_archive(self.cfg.state / "latest-backup.tar.gz", recovered)[
                "activities"
            ],
            2,
        )
        self.assertEqual((recovered / "FIT_OUT/100.fit").read_bytes(), self.original)
        self.assertFalse((recovered / "private").exists())

    def test_route_less_fit_does_not_override_summary_running_type(self):
        self.sync(indoor=True)
        row = read_json(self.cfg.current / "src/static/activities.json")[0]
        self.assertEqual(row["run_id"], RUN_ID)
        self.assertEqual(row["distance"], 1100)
        self.assertEqual(row["average_speed"], 3.8)
        self.assertFalse(row["summary_polyline"])
        self.assertEqual(row["subtype"], "running")
        self.assertFalse(row["is_indoor"])
        self.assertFalse(row["has_route"])
        self.assertEqual(row["route_status"], "missing")

    def test_garmin_indoor_types_survive_sync_without_normalizing_subtype(self):
        for activity_type in ("indoor_running", "treadmill_running"):
            with self.subTest(activity_type=activity_type):
                self.sync(indoor=True, activity_type=activity_type)
                row = read_json(self.cfg.current / "src/static/activities.json")[0]
                self.assertEqual(row["subtype"], activity_type)
                self.assertTrue(row["is_indoor"])
                self.assertFalse(row["has_route"])

    def test_garmin_indoor_type_takes_priority_even_with_gps(self):
        self.sync(activity_type="indoor_running")
        row = read_json(self.cfg.current / "src/static/activities.json")[0]
        self.assertEqual(row["subtype"], "indoor_running")
        self.assertTrue(row["is_indoor"])
        self.assertTrue(row["has_route"])

    def test_one_broken_fit_rolls_back_the_whole_round(self):
        previous = self.cfg.current
        with self.assertRaises(RuntimeError):
            self.sync(fail=True)
        self.assertEqual(self.cfg.current, previous)
        self.assertEqual((previous / "GPX_OUT/100.gpx").read_bytes(), GPX)
        self.assertFalse((previous / "FIT_OUT").exists())
        self.assertEqual(len(list((self.cfg.state / "snapshots").iterdir())), 1)
