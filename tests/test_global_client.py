"""Check the international protocol without real accounts or remote writes."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import zipfile

import httpx
from requests import Response

from run_page.automation.common import Config
from run_page.automation.global_client import GlobalClient
from tests.test_automation import GPX


class Auth:
    def __init__(self):
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1

    def expires_soon(self):
        return False

    def headers(self):
        return {
            "Authorization": "Bearer private-test-token",
            "Accept": "application/json",
        }

    def dumps(self):
        return "private-renewable-test-session"

    def close(self):
        pass


class GlobalLoginInitializationTest(unittest.TestCase):
    def test_real_login_command_reaches_the_account_prompt_before_network_auth(self):
        from run_page.automation.__main__ import main

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"state": str(Path(tmp) / "state")}))
            with (
                patch("builtins.input", side_effect=EOFError) as account_input,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(["--config", str(config), "global-login"]), 1)
            account_input.assert_called_once_with("International Garmin email: ")
            self.assertFalse(
                (Path(tmp) / "state/private/garmin-global-session.txt").exists()
            )

    def test_login_failure_reports_stage_without_echoing_provider_secrets(self):
        from run_page.automation.__main__ import main

        auth = Auth()
        auth.login = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("provider included private-password and private-token")
        )
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"state": str(Path(tmp) / "state")}))
            stderr = io.StringIO()
            with (
                patch(
                    "run_page.automation.global_client._ConnectAuth", return_value=auth
                ),
                patch("builtins.input", return_value="test@example.invalid"),
                patch("getpass.getpass", return_value="private-password"),
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                self.assertEqual(main(["--config", str(config), "global-login"]), 1)
            self.assertIn("authentication (RuntimeError)", stderr.getvalue())
            self.assertNotIn("private-password", stderr.getvalue())
            self.assertNotIn("private-token", stderr.getvalue())

    def test_di_oauth_login_and_renewal_with_old_login_route_blocked(self):
        from run_page.automation.__main__ import main

        exchanges = []

        def response(status, payload):
            result = Response()
            result.status_code = status
            result._content = json.dumps(payload).encode()
            return result

        def request(adapter, request, **kwargs):
            path = urlsplit(request.url).path
            if path == "/mobile/api/login":
                return response(429, {"message": "old client blocked"})
            if path == "/userprofile-service/socialProfile":
                self.assertEqual(
                    request.headers["Authorization"], "Bearer fixture-access-1"
                )
            else:
                self.assertTrue(path.endswith("/sign-in"), path)
            return response(200, {})

        def sign_in(url, *, params, **kwargs):
            self.assertEqual(url, "https://sso.garmin.com/mobile/sso/en_US/sign-in")
            self.assertEqual(params["clientId"], "GCM_IOS_DARK")
            return response(200, {})

        def submit(url, *, json, **kwargs):
            if url.endswith("/mobile/api/login"):
                return response(200, {"responseStatus": {"type": "MFA_REQUIRED"}})
            self.assertTrue(url.endswith("/mobile/api/mfa/verifyCode"), url)
            self.assertEqual(json["mfaVerificationCode"], "012345")
            return response(
                200,
                {
                    "responseStatus": {"type": "SUCCESSFUL"},
                    "serviceTicketId": "fixture-ticket",
                },
            )

        def exchange(url, *, data, **kwargs):
            self.assertEqual(
                url, "https://diauth.garmin.com/di-oauth2-service/oauth/token"
            )
            exchanges.append(data)
            number = len(exchanges)
            return response(
                200,
                {
                    "access_token": f"fixture-access-{number}",
                    "refresh_token": f"fixture-refresh-{number}",
                },
            )

        async def verify_session(config):
            used_tokens = []

            def profile(request):
                self.assertEqual(request.url.host, "connectapi.garmin.com")
                if request.url.path != "/userprofile-service/socialProfile":
                    return httpx.Response(404)
                used_tokens.append(request.headers["Authorization"])
                if len(used_tokens) == 1:
                    return httpx.Response(401)
                return httpx.Response(200, json={"profileId": 123})

            for _ in range(2):
                client = GlobalClient(
                    config,
                    http=httpx.AsyncClient(transport=httpx.MockTransport(profile)),
                )
                try:
                    self.assertEqual(len(await client.identity()), 64)
                finally:
                    await client.close()
            self.assertEqual(
                used_tokens,
                [
                    "Bearer fixture-access-1",
                    "Bearer fixture-access-2",
                    "Bearer fixture-access-2",
                ],
            )

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps({"state": str(Path(tmp) / "local")}))
            remote_path = Path(tmp) / "remote.json"
            remote_path.write_text(json.dumps({"state": str(Path(tmp) / "remote")}))
            sso = SimpleNamespace(get=sign_in, post=submit, close=lambda: None)
            stderr = io.StringIO()
            with (
                patch(
                    "requests.adapters.HTTPAdapter.send",
                    autospec=True,
                    side_effect=request,
                ),
                patch("curl_cffi.requests.Session", return_value=sso),
                patch("curl_cffi.requests.post", side_effect=exchange),
                patch("garminconnect.client.sso.time.sleep"),
                patch.dict(
                    "os.environ",
                    {
                        "GARMIN_GLOBAL_SECRET_STRING": "",
                        "GARMIN_GLOBAL_SESSION_FILE": str(Path(tmp) / "no-seed"),
                    },
                ),
                patch("builtins.input", return_value="test@example.invalid"),
                patch(
                    "getpass.getpass", side_effect=["fixture-password", "012345"]
                ) as secret_input,
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                self.assertEqual(
                    main(["--config", str(config_path), "global-login"]),
                    0,
                    stderr.getvalue(),
                )
                self.assertEqual(secret_input.call_count, 2)
                session_path = Path(tmp) / "local/private/garmin-global-session.txt"
                self.assertEqual(session_path.stat().st_mode & 0o777, 0o600)
                with patch("sys.stdin", io.StringIO(session_path.read_text())):
                    self.assertEqual(
                        main(
                            [
                                "--config",
                                str(remote_path),
                                "global-login",
                                "--import-file",
                                "-",
                            ]
                        ),
                        0,
                        stderr.getvalue(),
                    )
                asyncio.run(verify_session(Config(remote_path)))
            self.assertEqual(len(exchanges), 2)
            self.assertEqual(exchanges[0]["service_ticket"], "fixture-ticket")
            self.assertEqual(exchanges[1]["grant_type"], "refresh_token")
            self.assertEqual(exchanges[1]["refresh_token"], "fixture-refresh-1")


class GlobalClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = Config(self.root / "missing", state=self.root / "state")
        self.path = self.root / "100.gpx"
        self.path.write_bytes(GPX)

    def client(self, handler, auth=None):
        c = GlobalClient(
            self.cfg,
            auth=auth or Auth(),
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        self.addAsyncCleanup(c.close)
        return c

    async def test_full_pagination_uses_actual_returned_length(self):
        starts = []

        def respond(request):
            start = int(request.url.params["start"])
            self.assertNotIn("activityType", request.url.params)
            starts.append(start)
            return httpx.Response(
                200, json=[{"activityId": start + 100}] if start < 3 else []
            )

        self.assertEqual(len(await self.client(respond).activities()), 3)
        self.assertEqual(starts, [0, 1, 2, 3])

    async def test_repeated_pages_abort_inventory(self):
        c = self.client(lambda request: httpx.Response(200, json=[{"activityId": 123}]))
        with self.assertRaisesRegex(RuntimeError, "pagination repeated"):
            await c.activities()

    async def test_original_download_accepts_binary_content(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as packed:
            packed.writestr("123.gpx", GPX)

        def respond(request):
            if request.headers["Accept"] == "application/json":
                return httpx.Response(406)
            self.assertEqual(request.headers["Accept"], "*/*")
            return httpx.Response(200, content=archive.getvalue())

        self.assertEqual((await self.client(respond).download("123")).content, GPX)

    async def test_import_result_distinguishes_creation_duplicate_and_other_failure(
        self,
    ):
        for status, result, expected in (
            (200, {"successes": [{"internalId": 123}]}, {"id": "123", "created": True}),
            (
                409,
                {
                    "failures": [
                        {
                            "internalId": 789,
                            "messages": [{"content": "Duplicate Activity"}],
                        }
                    ]
                },
                {"id": "789", "created": False},
            ),
            (
                409,
                {
                    "failures": [
                        {
                            "internalId": 789,
                            "messages": [{"content": "Invalid activity"}],
                        }
                    ]
                },
                None,
            ),
        ):
            with self.subTest(status=status, result=result):
                c = self.client(
                    lambda request: httpx.Response(
                        status, json={"detailedImportResult": result}
                    )
                )
                if expected is None:
                    with self.assertRaises(RuntimeError):
                        await c.upload(self.path)
                else:
                    self.assertEqual(await c.upload(self.path), expected)

    async def test_post_timeout_is_not_blindly_replayed(self):
        calls = []

        def respond(request):
            calls.append(request)
            self.assertIn(GPX, request.content)
            raise httpx.ReadTimeout("lost response")

        with self.assertRaises(RuntimeError):
            await self.client(respond).upload(self.path)
        self.assertEqual(len(calls), 1)

    async def test_upload_receipt_reports_pending_then_a_created_activity(self):
        status_path = "/activity-service/activity/status/1780000000000/" + "a1" * 16
        responses = iter(
            (
                httpx.Response(
                    202,
                    headers={"Location": "https://connectapi.garmin.com" + status_path},
                ),
                httpx.Response(202),
                httpx.Response(
                    201, headers={"Location": "/activity-service/activity/123"}
                ),
            )
        )
        requests = []

        def respond(request):
            requests.append((request.method, request.url.path))
            return next(responses)

        client = self.client(respond)
        receipt = (await client.upload(self.path))["receipt"]
        self.assertIsNone(await client.upload_status(receipt))
        self.assertEqual(
            await client.upload_status(receipt), {"id": "123", "created": True}
        )
        self.assertEqual(
            requests,
            [
                ("POST", "/upload-service/upload"),
                ("GET", status_path),
                ("GET", status_path),
            ],
        )

    async def test_upload_receipt_cannot_send_credentials_to_another_host(self):
        client = self.client(lambda _: self.fail("Unexpected international request"))
        with self.assertRaisesRegex(
            RuntimeError, "Invalid international upload location"
        ):
            await client.upload_status(
                "https://example.invalid/upload-service/status/123"
            )

    async def test_upload_receipt_can_point_to_an_activity_before_it_is_visible(self):
        responses = iter(
            (
                httpx.Response(
                    202,
                    headers={
                        "Location": "https://connectapi.garmin.com/activity-service/activity/123"
                    },
                ),
                httpx.Response(404),
                httpx.Response(200, json={"activityId": 123}),
            )
        )
        client = self.client(lambda _: next(responses))
        receipt = await client.upload(self.path)
        self.assertEqual(receipt["id"], "123")
        self.assertIsNone(await client.upload_status(receipt["receipt"]))
        self.assertEqual(
            await client.upload_status(receipt["receipt"]),
            {"id": "123", "created": True},
        )

    async def test_401_renews_session_and_cache_is_private(self):
        calls = []

        def respond(request):
            calls.append(request)
            return (
                httpx.Response(401)
                if len(calls) == 1
                else httpx.Response(200, json={"profileId": 123})
            )

        auth = Auth()
        self.assertEqual(len(await self.client(respond, auth).identity()), 64)
        self.assertEqual(auth.refreshes, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            (self.cfg.state / "private/garmin-global-session.txt").stat().st_mode
            & 0o777,
            0o600,
        )

    async def test_cn_session_cannot_make_any_international_request(self):
        calls = []
        secret = json.dumps({"domain": "garmin.cn", "di_token": "fixture-cn"})
        with patch.dict("os.environ", {"GARMIN_GLOBAL_SECRET_STRING": secret}):
            c = GlobalClient(
                self.cfg,
                http=httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: calls.append(request))
                ),
            )
            self.addAsyncCleanup(c.close)
            with self.assertRaisesRegex(RuntimeError, "Invalid international"):
                await c.identity()
        self.assertEqual(calls, [])

    async def test_existing_garth_session_can_still_authenticate(self):
        secret = base64.b64encode(
            json.dumps(
                [
                    {
                        "domain": "garmin.com",
                        "oauth_token": "old",
                        "oauth_token_secret": "secret",
                    },
                    {
                        "scope": "fixture",
                        "jti": "fixture",
                        "token_type": "bearer",
                        "access_token": "fixture-legacy",
                        "refresh_token": "fixture-legacy-refresh",
                        "expires_in": 86400,
                        "expires_at": int(time.time()) + 86400,
                        "refresh_token_expires_in": 86400,
                        "refresh_token_expires_at": int(time.time()) + 86400,
                    },
                ]
            ).encode()
        ).decode()

        def respond(request):
            self.assertEqual(request.headers["Authorization"], "Bearer fixture-legacy")
            return httpx.Response(200, json={"profileId": 123})

        with patch.dict("os.environ", {"GARMIN_GLOBAL_SECRET_STRING": secret}):
            client = GlobalClient(
                self.cfg, http=httpx.AsyncClient(transport=httpx.MockTransport(respond))
            )
            self.addAsyncCleanup(client.close)
            self.assertEqual(len(await client.identity()), 64)

    async def test_metadata_update_is_a_partial_put_and_404_proves_absence(self):
        calls = []

        def respond(request):
            calls.append(request)
            return (
                httpx.Response(404) if request.method == "GET" else httpx.Response(204)
            )

        fields = {
            "activityName": "Updated",
            "activityTypeDTO": {"typeId": 1, "typeKey": "running", "parentTypeId": 0},
            "summaryDTO": {"distance": 5000},
        }
        c = self.client(respond)
        await c.update("123", fields)
        self.assertEqual(calls[0].method, "PUT")
        self.assertEqual(json.loads(calls[0].content), {"activityId": 123, **fields})
        await c.update("123", {"activityName": "Name only", "description": ""})
        self.assertEqual(
            json.loads(calls[1].content),
            {"activityId": 123, "activityName": "Name only", "description": ""},
        )
        self.assertIsNone(await c.summary("123"))
        await c.delete("123")
