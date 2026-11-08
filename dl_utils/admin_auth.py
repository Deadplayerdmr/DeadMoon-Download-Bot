"""Network boundaries for administrator-initiated provider authentication."""

from __future__ import annotations

import asyncio
import time

import aiohttp

TIDAL_CLIENT_ID = "zU4XHVVkc2tDPo4t"
TIDAL_CLIENT_SECRET = "VJKhDFqJPqvsPVNBV6ukXTJmwlvbttP7wlMlrc72se4="
TIDAL_SCOPE = "r_usr+w_usr+w_sub"
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
    if status != 200 or not payload.get("device_code") or not payload.get("user_code"):
        raise RuntimeError(f"Tidal device authorization failed (HTTP {status}).")
    return payload


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
            if status == 200 and payload.get("access_token"):
                return payload
            error = str(payload.get("error") or "")
            if error == "slow_down":
                delay += 5
            elif error == "expired_token":
                raise TimeoutError("The Tidal device code expired. Run /authenticatetidal again.")
            elif status >= 400 and error != "authorization_pending":
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
