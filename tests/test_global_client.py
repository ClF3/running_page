"""Check the international protocol without real accounts or remote writes."""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
import io
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import httpx

from run_page.automation.common import Config
from run_page.automation.global_client import GlobalClient, _new_auth
from tests.test_automation import GPX


class Token:
    expired = False
    expires_at = time.time() + 86400

    def __str__(self):
        return "Bearer private-test-token"


class Auth:
    domain = "garmin.com"

    def __init__(self):
        self.oauth2_token = Token()
        self.sess = SimpleNamespace(close=lambda: None)
        self.refreshes = 0

    def refresh_oauth2(self):
        self.refreshes += 1
        self.oauth2_token.expired = False
        self.oauth2_token.expires_at = time.time() + 86400

    def dumps(self):
        return "private-renewable-test-session"

    def loads(self, value):
        pass


class GlobalLoginInitializationTest(unittest.TestCase):
    def test_real_auth_factory_supports_the_pinned_garth_version(self):
        auth = _new_auth()
        self.addCleanup(auth.sess.close)
        self.assertEqual(auth.domain, "garmin.com")
        self.assertTrue(auth.sess.verify)
        self.assertEqual(auth.timeout, 20)
        self.assertEqual(auth.retries, 0)

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
                patch("run_page.automation.global_client._new_auth", return_value=auth),
                patch("builtins.input", return_value="test@example.invalid"),
                patch("getpass.getpass", return_value="private-password"),
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                self.assertEqual(main(["--config", str(config), "global-login"]), 1)
            self.assertIn("authentication (RuntimeError)", stderr.getvalue())
            self.assertNotIn("private-password", stderr.getvalue())
            self.assertNotIn("private-token", stderr.getvalue())


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
        auth = Auth()
        auth.domain = "garmin.cn"
        calls = []
        with (
            patch.dict("os.environ", {"GARMIN_GLOBAL_SECRET_STRING": "test-session"}),
            patch("run_page.automation.global_client._new_auth", return_value=auth),
        ):
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
        self.assertIsNone(await c.summary("123"))
        await c.delete("123")
