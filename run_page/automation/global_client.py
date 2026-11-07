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
import time

import httpx

from .activity_files import MAX_ACTIVITY_BYTES, unpack_activity
from .common import atomic_write, read_json, write_json

BASE_URL = "https://connectapi.garmin.com"


def activity_id(value):
    if not re.fullmatch(r"[0-9]{1,20}", str(value)) or int(value) <= 0:
        raise RuntimeError("Invalid international activity ID")
    return str(value)


def auth_status(config):
    cached = config.state / "private/garmin-global-session.txt"
    seed = Path(
        os.getenv("GARMIN_GLOBAL_SESSION_FILE", "/config/garmin-global-session.txt")
    )
    return {
        "method": "oauth_session",
        "configured": bool(
            cached.is_file()
            or seed.is_file()
            or os.getenv("GARMIN_GLOBAL_SECRET_STRING", "").strip()
        ),
    }


def _new_auth():
    os.environ["GARTH_TELEMETRY_ENABLED"] = "false"
    from garth.http import Client

    return Client(
        domain="garmin.com",
        ssl_verify=True,
        telemetry_enabled=False,
        timeout=20,
        retries=0,
    )


class GlobalClient:
    def __init__(self, config, *, http=None, auth=None):
        self.config = config
        self.session_path = config.state / "private/garmin-global-session.txt"
        self.seed_path = Path(
            os.getenv("GARMIN_GLOBAL_SESSION_FILE", "/config/garmin-global-session.txt")
        )
        self.auth = auth or _new_auth()
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(90, connect=20))
        self._loaded = auth is not None
        self._account = None
        self._types = None

    async def _load(self):
        if self._loaded:
            return
        seed = os.getenv("GARMIN_GLOBAL_SECRET_STRING", "").strip()
        if not seed and self.seed_path.is_file():
            seed = self.seed_path.read_text().strip()
        seed_hash = hashlib.sha256(seed.encode()).hexdigest() if seed else None
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
            self.auth.loads(secret)
            if self.auth.domain != "garmin.com":
                raise ValueError("Wrong account region")
        except Exception:
            raise RuntimeError(
                "Invalid international OAuth session; run ops/global-login"
            ) from None
        self._loaded = True
        self._persist()
        write_json(
            self.session_path.with_suffix(".origin.json"), {"seed_hash": seed_hash}
        )

    def _persist(self):
        atomic_write(self.session_path, self.auth.dumps())

    async def _token(self, force=False):
        await self._load()
        token = self.auth.oauth2_token
        if force or token.expired or float(token.expires_at) <= time.time() + 60:
            try:
                await asyncio.to_thread(self.auth.refresh_oauth2)
                self._persist()
            except Exception:
                raise RuntimeError(
                    "International session renewal failed; run ops/global-login"
                ) from None
        return str(self.auth.oauth2_token)

    async def _request(self, method, path, *, payload=None, files=None, allow=()):
        # A POST timeout/5xx may have happened after acceptance. Never replay it
        # here: the durable delivery module reconciles before another upload.
        attempts = 3 if method in {"GET", "PUT", "DELETE"} else 2
        for attempt in range(attempts):
            token = await self._token()
            if files:
                for _, stream, _ in files.values():
                    stream.seek(0)
            try:
                async with asyncio.timeout(95):
                    async with self.http.stream(
                        method,
                        BASE_URL + path,
                        headers={
                            "Authorization": token,
                            "User-Agent": "Mozilla/5.0",
                            "origin": "https://connect.garmin.com",
                        },
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
                    await self._token(force=True)
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
                return status, bytes(content)
            except (httpx.TransportError, TimeoutError):
                if method == "POST" or attempt == attempts - 1:
                    raise RuntimeError(
                        "International network outcome requires verification"
                    ) from None
                await asyncio.sleep(2 ** (attempt + 1))
        raise RuntimeError("International authentication failed")

    async def _json(self, path, **kwargs):
        status, body = await self._request("GET", path, **kwargs)
        if status == 404:
            return None
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            raise RuntimeError("Invalid international JSON response") from None

    async def identity(self):
        if self._account is None:
            profile = await self._json("/userprofile-service/userprofile/userprofile")
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
                f"/activitylist-service/activities/search/activities?start={start}&limit=100&activityType=running"
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
            status, body = await self._request(
                "POST",
                "/upload-service/upload",
                files={"file": (path.name, stream, "application/octet-stream")},
                allow=(409,),
            )
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
        kind = payload["activityTypeDTO"]
        if "typeId" not in kind or "parentTypeId" not in kind:
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
        _, body = await self._request(
            "GET", "/download-service/files/activity/" + activity_id(target_id)
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
        self.auth.sess.close()


def login(config, import_file=None):
    """Run interactively once; passwords/MFA codes are never persisted."""
    import getpass

    auth = _new_auth()
    try:
        if import_file:
            auth.loads(Path(import_file).read_text().strip())
        else:
            email = input("International Garmin email: ").strip()
            password = getpass.getpass("International Garmin password: ")
            auth.login(
                email,
                password,
                mfa_callback=lambda: getpass.getpass("Garmin verification code: "),
            )
            del password
        if auth.domain != "garmin.com":
            raise RuntimeError("A Garmin international session is required")
        destination = config.state / "private/garmin-global-session.txt"
        atomic_write(destination, auth.dumps())
        print(
            "Saved international session to "
            + str(destination)
            + " (0600); no password saved."
        )
    except Exception:
        raise RuntimeError(
            "International login failed; retry interactively or import a valid global session"
        ) from None
    finally:
        auth.sess.close()
