"""Personal API key authentication, bounded requests and private token caching.

Protocol references: Garmin's Personal API FAQ and official Python client:
https://developer.garmin.com/gc-developer-program/personal/personal-faq/
https://github.com/garmin/connect-personal-api-python
"""

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import time

import httpx

from .activity_files import MAX_ACTIVITY_BYTES, unpack_activity
from .common import read_json, write_json
from .garmin import ActivityEndpoints

BASE_URLS = {
    "cn": "https://personal.connectapi.garmin.cn",
    "global": "https://personal.connectapi.garmin.com",
}
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"


def token_expiration(token):
    try:
        if not isinstance(token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token
        ):
            raise ValueError()
        payload = token.split(".")[1]
        expires = float(
            json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))[
                "exp"
            ]
        )
        if not math.isfinite(expires):
            raise ValueError()
        return expires
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        # JWT claims are used only for cache expiry. HTTPS authenticates the
        # server response; this is not a local authorization/signature check.
        raise RuntimeError("Personal API returned an invalid token or expiry") from exc


class PersonalApiClient(ActivityEndpoints):
    # Match Garmin's reference client. Larger pages currently return HTTP 500
    # on the CN Personal API even though they work with legacy authentication.
    page_size = 20
    request_timeout = 120

    def __init__(self, state, region="cn", *, api_key=None, http=None):
        key = (
            api_key if api_key is not None else os.getenv("GARMIN_PERSONAL_API_KEY", "")
        )
        if not isinstance(key, str) or not key.strip():
            raise RuntimeError("GARMIN_PERSONAL_API_KEY is required")
        if region not in BASE_URLS:
            raise RuntimeError("garmin_region must be cn or global")
        self._key = key.strip()
        self.base_url = BASE_URLS[region]
        self._fingerprint = hashlib.sha256(
            (self.base_url + "\0" + self._key).encode()
        ).hexdigest()
        self.cache_path = Path(state) / "private/personal-api-token.json"
        self._token, self._expires = None, 0
        try:
            cache = read_json(self.cache_path, {})
            if (
                isinstance(cache, dict)
                and cache.get("fingerprint") == self._fingerprint
            ):
                self._expires = token_expiration(cache.get("access_token"))
                self._token = cache["access_token"]
        except (ValueError, RuntimeError):
            logging.warning("Ignoring invalid Personal API token cache")
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20))

    async def _send(self, method, path, headers, limit):
        for attempt in range(3):
            try:
                # A read timeout resets whenever bytes arrive. Also bound the
                # complete response so a stalled/trickling transfer is retried.
                async with asyncio.timeout(self.request_timeout):
                    async with self.http.stream(
                        method, self.base_url + path, headers=headers
                    ) as response:
                        status = response.status_code
                        if status == 429 or status >= 500:
                            if attempt == 2:
                                raise RuntimeError(
                                    f"Garmin Personal API request failed (HTTP {status})"
                                )
                            retry = response.headers.get("Retry-After", "")
                            delay = (
                                min(120, int(retry))
                                if retry.isdigit()
                                else 2 ** (attempt + 1)
                            )
                        elif status != 200:
                            return status, b""  # Error bodies may contain credentials.
                        else:
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(body) + len(chunk) > limit:
                                    raise RuntimeError(
                                        "Garmin Personal API response exceeds its size limit"
                                    )
                                body.extend(chunk)
                            return status, bytes(body)
                await asyncio.sleep(delay)
            except (httpx.TransportError, TimeoutError):
                if attempt == 2:
                    raise RuntimeError(
                        "Garmin Personal API network request failed after 3 attempts"
                    ) from None
                logging.warning(
                    "Garmin Personal API network request interrupted; retrying (%s/3)",
                    attempt + 2,
                )
                await asyncio.sleep(2 ** (attempt + 1))
        raise RuntimeError("Garmin Personal API request failed")

    async def obtain_token(self, force=False):
        if not force and self._token and self._expires - time.time() > 3600:
            return self._token
        status, body = await self._send(
            "POST",
            TOKEN_PATH,
            {"Authorization": "ApiKey " + self._key, "Accept": "*/*"},
            16384,
        )
        if status != 200:
            raise RuntimeError(
                f"Personal API token exchange failed (HTTP {status}); check the key, expiry and garmin_region"
            )
        try:
            token = body.decode("utf-8").strip()
        except UnicodeError as exc:
            raise RuntimeError("Personal API returned an invalid token") from exc
        expires = token_expiration(token)
        if expires <= time.time():
            raise RuntimeError("Personal API returned an expired token")
        write_json(
            self.cache_path,
            {
                "fingerprint": self._fingerprint,
                "access_token": token,
                "expires_at": expires,
            },
        )
        self._token, self._expires = token, expires
        logging.info("Personal API access token renewed and cached")
        return token

    async def request(self, path, as_json=True):
        token = await self.obtain_token()
        for attempt in range(2):
            status, body = await self._send(
                "GET",
                path,
                {
                    "Authorization": "Bearer " + token,
                    "Accept": (
                        "application/json"
                        if as_json
                        else "application/zip, application/x-zip-compressed, application/octet-stream"
                    ),
                },
                MAX_ACTIVITY_BYTES,
            )
            if status == 200:
                try:
                    return json.loads(body) if as_json else body
                except (ValueError, UnicodeError) as exc:
                    raise RuntimeError("Personal API returned invalid JSON") from exc
            if status == 401 and attempt == 0:
                token = await self.obtain_token(force=True)
                continue
            hint = "; check the key and region" if status in (401, 403) else ""
            raise RuntimeError(
                f"Garmin Personal API request failed (HTTP {status}){hint}"
            )

    async def download(self, source_id):
        if not re.fullmatch(r"[0-9]{1,20}", str(source_id)) or int(source_id) <= 0:
            raise RuntimeError("Invalid Garmin activity ID")
        return unpack_activity(
            await self.request(f"/download-service/files/activity/{source_id}", False)
        )
