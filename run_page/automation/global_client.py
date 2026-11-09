"""International Garmin writes using an isolated, renewable OAuth session.

Personal API keys continue to serve CN reads. Garmin's published Personal API
specification contains GET endpoints only; uploads use Connect's session API.
No account password is saved or passed on a command line.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

import httpx

from .activity_files import MAX_ACTIVITY_BYTES, unpack_activity
from .common import atomic_write, job_lock, read_json, write_json

BASE_URL = "https://connectapi.garmin.com"
SESSION_PATH = "private/garmin-global-session.txt"
ACTIVITY_RECEIPT_PATH = r"/activity-service/activity/[0-9]{1,20}"
UPLOAD_RECEIPT_PATH = (
    rf"(?:/activity-service/activity/status/[0-9]{{1,20}}/[a-fA-F0-9-]{{32,36}}"
    rf"|/upload-service/[A-Za-z0-9_./-]+|{ACTIVITY_RECEIPT_PATH})"
)


def activity_id(value):
    if not re.fullmatch(r"[0-9]{1,20}", str(value)) or int(value) <= 0:
        raise RuntimeError("Invalid international activity ID")
    return str(value)


def _upload_location(value, pattern):
    location = urlsplit(value or "")
    if (
        (location.scheme, location.netloc)
        not in {("", ""), ("https", "connectapi.garmin.com")}
        or location.query
        or location.fragment
        or not re.fullmatch(pattern, location.path)
        or any(part in {".", ".."} for part in location.path.split("/"))
    ):
        raise RuntimeError("Invalid international upload location")
    return location.path


def _seed_path():
    return Path(
        os.getenv("GARMIN_GLOBAL_SESSION_FILE", "/config/garmin-global-session.txt")
    )


def _session_seed():
    seed = os.getenv("GARMIN_GLOBAL_SECRET_STRING", "").strip()
    path = _seed_path()
    if not seed and path.is_file():
        seed = path.read_text().strip()
    return seed, hashlib.sha256(seed.encode()).hexdigest() if seed else None


def auth_status(config):
    return {
        "method": "oauth_session",
        "configured": bool(
            (config.state / SESSION_PATH).is_file()
            or _seed_path().is_file()
            or os.getenv("GARMIN_GLOBAL_SECRET_STRING", "").strip()
        ),
    }


class _ConnectAuth:
    domain = "garmin.com"

    def __init__(self):
        from garminconnect.client import Client

        # Report only sanitized stages and status codes at our CLI boundary.
        logging.getLogger("garminconnect").setLevel(logging.CRITICAL + 1)
        self.client = Client(domain=self.domain)

    def _require_renewable(self):
        tokens = (
            self.client.di_token,
            self.client.di_refresh_token,
            self.client.di_client_id,
        )
        if not all(isinstance(token, str) and token for token in tokens):
            raise LoginError(
                "Garmin did not provide a renewable international OAuth session"
            )

    def login(self, email, password, prompt_mfa):
        self.client.login(email, password, prompt_mfa=prompt_mfa)
        self._require_renewable()

    def loads(self, secret):
        if json.loads(secret).get("domain") != self.domain:
            raise ValueError("Wrong account region")
        self.client.loads(secret)
        self._require_renewable()

    def dumps(self):
        tokens = json.loads(self.client.dumps())
        return json.dumps({**tokens, "domain": self.domain})

    def expires_soon(self):
        return self.client._token_expires_soon()

    def refresh(self):
        self.client._refresh_di_token()
        self._require_renewable()

    def headers(self):
        return self.client.get_api_headers()

    def close(self):
        self.client.cs.close()
        self.client._api_session.close()


class _LegacyGarthAuth:
    """Read and renew an already-issued Garth session."""

    def __init__(self, secret):
        os.environ["GARTH_TELEMETRY_ENABLED"] = "false"
        from garth.http import Client

        self.client = Client(
            domain="garmin.com", ssl_verify=True, telemetry_enabled=False
        )
        self.client.configure(timeout=20, retries=0)
        try:
            self.client.loads(secret)
            if self.client.domain != "garmin.com":
                raise ValueError("Wrong account region")
        except Exception:
            self.close()
            raise

    def dumps(self):
        return self.client.dumps()

    def expires_soon(self):
        return self.client.oauth2_token.expires_at <= time.time() + 60

    def refresh(self):
        self.client.refresh_oauth2()

    def headers(self):
        return {
            "Authorization": str(self.client.oauth2_token),
            "User-Agent": "Mozilla/5.0",
        }

    def close(self):
        self.client.sess.close()


def _load_auth(secret):
    if not secret.startswith("{"):
        return _LegacyGarthAuth(secret)
    auth = _ConnectAuth()
    try:
        auth.loads(secret)
    except Exception:
        auth.close()
        raise
    return auth


class GlobalClient:
    def __init__(self, config, *, http=None, auth=None):
        self.config = config
        self.session_path = config.state / SESSION_PATH
        self.auth = auth
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(90, connect=20))
        self._account = None
        self._types = None

    async def _load(self):
        if self.auth is not None:
            return
        seed, seed_hash = _session_seed()
        marker = read_json(self.session_path.with_suffix(".origin.json"), {})
        if self.session_path.is_file() and (
            not seed or marker.get("seed_hash") == seed_hash
        ):
            secret = self.session_path.read_text().strip()
        else:
            secret = seed
        if not secret:
            raise RuntimeError("International session is missing; run ops/global-login")
        try:
            self.auth = _load_auth(secret)
        except Exception:
            raise RuntimeError(
                "Invalid international OAuth session; run ops/global-login"
            ) from None
        self._persist()
        write_json(
            self.session_path.with_suffix(".origin.json"), {"seed_hash": seed_hash}
        )

    def _persist(self):
        atomic_write(self.session_path, self.auth.dumps())

    def _renew(self):
        self.auth.refresh()
        self._persist()

    async def _headers(self, force=False):
        await self._load()
        if force or self.auth.expires_soon():
            try:
                await asyncio.to_thread(self._renew)
            except Exception:
                raise RuntimeError(
                    "International session renewal failed; run ops/global-login"
                ) from None
        return {"Origin": "https://connect.garmin.com", **self.auth.headers()}

    async def _request(
        self, method, path, *, payload=None, files=None, allow=(), accept=None
    ):
        # A POST timeout/5xx may have happened after acceptance. Never replay it
        # here: the durable delivery module reconciles before another upload.
        attempts = 3 if method in {"GET", "PUT", "DELETE"} else 2
        for attempt in range(attempts):
            headers = await self._headers()
            if accept:
                headers["Accept"] = accept
            if files:
                for _, stream, _ in files.values():
                    stream.seek(0)
            try:
                async with asyncio.timeout(95):
                    async with self.http.stream(
                        method,
                        BASE_URL + path,
                        headers=headers,
                        json=payload,
                        files=files,
                    ) as response:
                        status = response.status_code
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(content) + len(chunk) > MAX_ACTIVITY_BYTES:
                                raise RuntimeError(
                                    "International response exceeds 64 MiB"
                                )
                            content.extend(chunk)
                if status == 401 and attempt == 0:
                    await self._headers(force=True)
                    continue  # An explicit 401 did not accept a write.
                if (
                    method != "POST"
                    and (status == 429 or status >= 500)
                    and attempt < attempts - 1
                ):
                    retry = response.headers.get("Retry-After", "")
                    await asyncio.sleep(
                        min(30, int(retry)) if retry.isdigit() else 2 ** (attempt + 1)
                    )
                    continue
                if status not in (*allow, 200, 201, 202, 204):
                    raise RuntimeError(f"International request failed (HTTP {status})")
                return status, bytes(content), response.headers.get("Location")
            except (httpx.TransportError, TimeoutError):
                if method == "POST" or attempt == attempts - 1:
                    raise RuntimeError(
                        "International network outcome requires verification"
                    ) from None
                await asyncio.sleep(2 ** (attempt + 1))
        raise RuntimeError("International authentication failed")

    async def _json(self, path, **kwargs):
        status, body, _ = await self._request("GET", path, **kwargs)
        if status == 404:
            return None
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            raise RuntimeError("Invalid international JSON response") from None

    async def identity(self):
        if self._account is None:
            profile = await self._json("/userprofile-service/socialProfile")
            value = (
                profile.get("profileId")
                or profile.get("id")
                or profile.get("displayName")
            )
            if not value:
                raise RuntimeError(
                    "International account identity could not be verified"
                )
            self._account = hashlib.sha256(
                ("garmin.com\0" + str(value)).encode()
            ).hexdigest()
        return self._account

    async def activities(self):
        result, seen = [], set()
        start = 0
        for _ in range(1000):
            page = await self._json(
                f"/activitylist-service/activities/search/activities?start={start}&limit=100"
            )
            if not isinstance(page, list):
                raise RuntimeError("Invalid international activities response")
            if not page:
                return result
            ids = [activity_id(row["activityId"]) for row in page]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise RuntimeError("International pagination repeated activity IDs")
            seen.update(ids)
            result.extend(page)
            start += len(page)
        raise RuntimeError("International pagination safety limit reached")

    async def summary(self, target_id):
        target_id = activity_id(target_id)
        value = await self._json(
            "/activity-service/activity/" + target_id, allow=(404,)
        )
        if value is not None and str(value.get("activityId")) != target_id:
            raise RuntimeError("International summary identity mismatch")
        return value

    async def upload(self, path):
        path = Path(path)
        with path.open("rb") as stream:
            status, body, location = await self._request(
                "POST",
                "/upload-service/upload",
                files={"file": (path.name, stream, "application/octet-stream")},
                allow=(409,),
            )
        if status == 202 and location:
            receipt = _upload_location(location, UPLOAD_RECEIPT_PATH)
            result = {"receipt": receipt}
            if re.fullmatch(ACTIVITY_RECEIPT_PATH, receipt):
                result["id"] = activity_id(receipt.rsplit("/", 1)[1])
            return result
        return self._upload_result(status, body, location)

    async def upload_status(self, receipt):
        path = _upload_location(receipt, UPLOAD_RECEIPT_PATH)
        if re.fullmatch(ACTIVITY_RECEIPT_PATH, path):
            target_id = activity_id(path.rsplit("/", 1)[1])
            status, body, _ = await self._request("GET", path, allow=(404,))
            if status in (202, 404):
                return None
            if status != 200 or str(json.loads(body).get("activityId")) != target_id:
                raise RuntimeError("International upload activity identity mismatch")
            return {"id": target_id, "created": True}
        status, body, location = await self._request("GET", path, allow=(409,))
        if status == 202:
            return None
        return self._upload_result(status, body, location)

    @staticmethod
    def _upload_result(status, body, location):
        if status == 201 and location:
            path = _upload_location(location, ACTIVITY_RECEIPT_PATH)
            return {"id": activity_id(path.rsplit("/", 1)[1]), "created": True}
        try:
            imported = json.loads(body)["detailedImportResult"]
            successes = imported.get("successes", [])
            if len(successes) == 1 and status != 409:
                return {"id": activity_id(successes[0]["internalId"]), "created": True}
            failures = imported.get("failures", [])
            duplicates = [
                f
                for f in failures
                if "duplicate activity" in json.dumps(f.get("messages", [])).lower()
            ]
            if duplicates:
                ids = {
                    activity_id(f["internalId"])
                    for f in duplicates
                    if f.get("internalId")
                }
                return {"id": ids.pop() if len(ids) == 1 else None, "created": False}
        except (ValueError, KeyError, TypeError, UnicodeError):
            pass
        raise RuntimeError("International upload acceptance is not verified")

    async def update(self, target_id, fields):
        target_id = activity_id(target_id)
        payload = deepcopy(fields)
        kind = payload.get("activityTypeDTO")
        if kind is not None and ("typeId" not in kind or "parentTypeId" not in kind):
            if self._types is None:
                self._types = await self._json(
                    "/activity-service/activity/activityTypes"
                )
            matches = [r for r in self._types if r.get("typeKey") == kind["typeKey"]]
            if len(matches) != 1:
                raise RuntimeError("International activity type cannot be resolved")
            payload["activityTypeDTO"] = {
                k: matches[0][k]
                for k in ("typeId", "typeKey", "parentTypeId")
                if k in matches[0]
            }
        await self._request(
            "PUT",
            "/activity-service/activity/" + target_id,
            payload={"activityId": int(target_id), **payload},
        )

    async def download(self, target_id):
        _, body, _ = await self._request(
            "GET",
            "/download-service/files/activity/" + activity_id(target_id),
            accept="*/*",
        )
        return unpack_activity(body)

    async def delete(self, target_id):
        await self._request(
            "DELETE",
            "/activity-service/activity/" + activity_id(target_id),
            allow=(404,),
        )

    async def close(self):
        await self.http.aclose()
        if self.auth is not None:
            self.auth.close()


class LoginError(RuntimeError):
    """A deliberately credential-free diagnostic safe to display in the CLI."""


def login(config, import_file=None):
    """Run interactively once; passwords/MFA codes are never persisted."""
    import getpass

    auth = None
    stage = "client initialization"
    try:
        if import_file:
            stage = "session import"
            secret = (
                sys.stdin.read()
                if import_file == "-"
                else Path(import_file).read_text()
            )
            auth = _load_auth(secret.strip())
        else:
            auth = _ConnectAuth()
            stage = "account input"
            email = input("International Garmin email: ").strip()
            stage = "password input"
            password = getpass.getpass("International Garmin password: ")
            stage = "authentication"
            auth.login(
                email,
                password,
                prompt_mfa=lambda: getpass.getpass("Garmin verification code: "),
            )
            del password
        stage = "session storage"
        destination = config.state / SESSION_PATH
        with job_lock(config, blocking=True):
            _, seed_hash = _session_seed()
            atomic_write(destination, auth.dumps())
            write_json(
                destination.with_suffix(".origin.json"), {"seed_hash": seed_hash}
            )
        print(
            "Saved international session to "
            + str(destination)
            + " (0600); no password saved."
        )
    except EOFError:
        raise LoginError(
            "International login needs interactive input; use ssh -t and ops/global-login"
        ) from None
    except KeyboardInterrupt:
        raise LoginError("International login cancelled") from None
    except LoginError:
        raise
    except Exception as exc:
        response = getattr(exc, "response", None)
        if response is None:
            response = getattr(getattr(exc, "error", None), "response", None)
        status = getattr(response, "status_code", None)
        detail = type(exc).__name__
        if detail == "GarminConnectTooManyRequestsError":
            status = 429
        if isinstance(status, int) and 100 <= status <= 599:
            detail += f", HTTP {status}"
        # Provider exception messages and bodies can contain passwords/tokens.
        raise LoginError(
            f"International login failed during {stage} ({detail})"
        ) from None
    finally:
        if auth is not None:
            auth.close()
