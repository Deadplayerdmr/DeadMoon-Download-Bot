"""Network boundaries for administrator-initiated provider authentication."""

from __future__ import annotations

import asyncio
import time

import aiohttp

from dl_utils import platform_config as pcfg

TIDAL_CLIENT_ID = "fX2JxdmntZWK0ixT"
TIDAL_CLIENT_SECRET = "1Nm5AfDAjxrgJFJbKNWLeAyKGVGmINuXPPLHVXAvxAg="
TIDAL_SCOPE = "r_usr w_usr w_sub"
TIDAL_DEVICE_URL = "https://auth.tidal.com/v1/oauth2/device_authorization"
TIDAL_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"


async def _post_oauth(session: aiohttp.ClientSession, url: str, data: dict) -> tuple[int, dict]:
    async with session.post(url, data=data) as response:
        try:
            payload = await response.json(content_type=None)
        except (ValueError, TypeError):
            payload = {}
        return response.status, payload


async def request_tidal_device_code() -> dict:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        status, payload = await _post_oauth(session, TIDAL_DEVICE_URL, {
            "client_id": TIDAL_CLIENT_ID,
            "client_secret": TIDAL_CLIENT_SECRET,
            "scope": TIDAL_SCOPE,
        })
    if status != 200:
        raise RuntimeError(f"Tidal device authorization failed (HTTP {status}).")
    data = payload if isinstance(payload, dict) else {}
    device_code = str(data.get("device_code") or data.get("deviceCode") or "")
    user_code = str(data.get("user_code") or data.get("userCode") or "")
    if not device_code or not user_code:
        raise RuntimeError(f"Tidal device authorization failed (HTTP {status}).")
    verification_uri_complete = str(
        data.get("verification_uri_complete")
        or data.get("verificationUriComplete")
        or f"https://link.tidal.com/{user_code}"
    )
    if not verification_uri_complete.startswith(("http://", "https://")):
        verification_uri_complete = f"https://{verification_uri_complete}"
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": str(
            data.get("verification_uri") or data.get("verificationUri") or "https://link.tidal.com"
        ),
        "verification_uri_complete": verification_uri_complete,
        "expires_in": int(data.get("expires_in") or data.get("expiresIn") or 300),
        "interval": int(data.get("interval") or 5),
    }


def _normalize_tidal_token(payload: dict) -> dict:
    data = payload if isinstance(payload, dict) else {}
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    access_token = data.get("access_token") or data.get("accessToken")
    claims = pcfg.decode_tidal_jwt_claims(str(access_token or ""))
    user_id = (
        data.get("user_id")
        or data.get("userId")
        or user.get("userId")
        or user.get("id")
        or claims.get("user_id")
    )
    country_code = (
        claims.get("country_code")
        or data.get("country_code")
        or data.get("countryCode")
        or user.get("countryCode")
        or "US"
    )
    return {
        "access_token": access_token,
        "refresh_token": data.get("refresh_token") or data.get("refreshToken"),
        "token_type": "Bearer",
        "user_id": user_id,
        "country_code": country_code,
        "token_expiry": claims.get("token_expiry") or str(
            int(time.time()) + int(data.get("expires_in") or data.get("expiresIn") or 86400)
        ),
    }


async def poll_tidal_device_token(
    device_code: str, interval: int = 5, expires_in: int = 300
) -> dict:
    """Poll until approval or device-code expiry; pending responses are normal."""
    deadline = time.monotonic() + max(int(expires_in or 300), 1)
    delay = max(int(interval or 5), 1)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while time.monotonic() < deadline:
            status, payload = await _post_oauth(session, TIDAL_TOKEN_URL, {
                "client_id": TIDAL_CLIENT_ID,
                "client_secret": TIDAL_CLIENT_SECRET,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "scope": TIDAL_SCOPE,
            })
            data = payload if isinstance(payload, dict) else {}
            access_token = data.get("access_token") or data.get("accessToken")
            if status == 200 and access_token:
                return _normalize_tidal_token(data)
            error = str(data.get("error") or "")
            body = str(data).casefold()
            pending = (
                error in ("authorization_pending", "slow_down")
                or "authorization_pending" in body
                or str(data.get("sub_status") or "") == "1002"
                or "1002" in body
            )
            if status == 400 and pending:
                if error == "slow_down":
                    delay += 5
            elif error == "expired_token":
                raise TimeoutError("The Tidal device code expired. Run /authenticatetidal again.")
            elif status >= 400:
                raise RuntimeError(f"Tidal device authorization failed (HTTP {status}: {error or 'request failed'}).")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(delay, remaining))
    raise TimeoutError("The Tidal device code expired. Run /authenticatetidal again.")


def discover_qobuz_app_credentials_sync() -> tuple[str, list[str]]:
    """Run Streamrip's async Qobuz discovery in a fresh worker-thread loop."""
    async def discover():
        from streamrip.client.qobuz import QobuzSpoofer

        async with QobuzSpoofer() as spoofer:
            return await spoofer.get_app_id_and_secrets()

    return asyncio.run(discover())
