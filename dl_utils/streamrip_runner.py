"""Run Streamrip with compatibility fixes for Tidal's authenticated API."""

import base64
import inspect
import json
import logging
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

logger = logging.getLogger("streamrip")
TIDAL_CLIENT_ID = "fX2JxdmntZWK0ixT"
TIDAL_CLIENT_SECRET = "1Nm5AfDAjxrgJFJbKNWLeAyKGVGmINuXPPLHVXAvxAg="
_TIDAL_HOST = "https://tidal.com/v1"
_TIDAL_API_HOST = "https://api.tidal.com/v1"
_TIDAL_HIFI_HOST = "https://api.tidalhifi.com/v1"
_TIDAL_DESKTOP_HOST = "https://desktop.tidal.com/v1"
_TIDAL_API_HOSTS = (_TIDAL_API_HOST, _TIDAL_HIFI_HOST)
_TIDAL_PLAYBACK_HOSTS = (_TIDAL_HIFI_HOST, _TIDAL_API_HOST, _TIDAL_DESKTOP_HOST)
_PLAYBACK_PATHS = ("/playbackinfopostpaywall", "/playbackinfo")
_PLAYBACK_QUALITIES = ("HI_RES_LOSSLESS", "HI_RES", "LOSSLESS")
_LOSSLESS_QUALITIES = frozenset(_PLAYBACK_QUALITIES)
_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
_SESSIONS_URL = "https://api.tidal.com/v1/sessions"


def _pop_header(headers, name: str) -> None:
    for key in tuple(headers):
        if str(key).casefold() == name.casefold():
            headers.pop(key, None)


def _set_bearer(client, token: str | None = None) -> None:
    access_token = str(client.config.access_token or "") if token is None else str(token)
    try:
        client.config.token_type = "Bearer"
    except (AttributeError, TypeError):
        pass
    _pop_header(client.session.headers, "authorization")
    _pop_header(client.session.headers, "x-tidal-token")
    client.session.headers["Authorization"] = f"Bearer {access_token}"


def _request_base(method) -> str:
    try:
        return str(inspect.signature(method).parameters["base"].default)
    except (KeyError, TypeError, ValueError):
        return _TIDAL_API_HOST


def _next_quality(value: str) -> str | None:
    try:
        index = _PLAYBACK_QUALITIES.index(value)
    except ValueError:
        return None
    return _PLAYBACK_QUALITIES[index + 1] if index + 1 < len(_PLAYBACK_QUALITIES) else None


def _error_status(error: Exception) -> int | None:
    status = getattr(error, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _unavailable_lyrics(error: Exception, lyrics: bool) -> bool:
    return lyrics and (
        _error_status(error) in (401, 403, 404)
        or error.__class__.__name__ == "NonStreamableError"
    )


def _patch_tidal_credentials(tidal_module, client_class) -> None:
    import aiohttp

    tidal_module.CLIENT_ID = TIDAL_CLIENT_ID
    tidal_module.CLIENT_SECRET = TIDAL_CLIENT_SECRET
    tidal_module.AUTH = aiohttp.BasicAuth(TIDAL_CLIENT_ID, TIDAL_CLIENT_SECRET)
    for name, value in (
        ("CLIENT_ID", TIDAL_CLIENT_ID),
        ("CLIENT_SECRET", TIDAL_CLIENT_SECRET),
    ):
        if hasattr(client_class, name):
            setattr(client_class, name, value)


def _sub_status(value) -> int | None:
    if isinstance(value, dict):
        status = value.get("subStatus", value.get("sub_status"))
    else:
        status = getattr(value, "subStatus", getattr(value, "sub_status", None))
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _decode_manifest(response: dict) -> tuple[str, dict | ET.Element]:
    encoded = response.get("manifest")
    if not encoded:
        raise ValueError("Tidal response has no manifest")
    raw = base64.b64decode(encoded)
    mime = str(response.get("manifestMimeType") or "")
    if mime == "application/dash+xml" or raw.lstrip().startswith(b"<"):
        return "dash", ET.fromstring(raw)
    return "bts", json.loads(raw)


def _manifest_codecs(kind: str, manifest) -> str:
    if kind == "bts":
        return str(manifest.get("codecs") or "")
    representation = next(
        (item for item in manifest.iter() if item.tag.rsplit("}", 1)[-1] == "Representation"),
        None,
    )
    return str(representation.attrib.get("codecs") or "") if representation is not None else ""


def _lossless_manifest(response: dict, requested_quality: str) -> bool:
    try:
        kind, manifest = _decode_manifest(response)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, ET.ParseError):
        return False
    codecs = _manifest_codecs(kind, manifest).casefold()
    if codecs.startswith("mp4a"):
        return False
    if not codecs.startswith(("flac", "mqa")):
        return False
    returned_quality = str(response.get("audioQuality") or "")
    if requested_quality in _LOSSLESS_QUALITIES and returned_quality in ("HIGH", "LOW"):
        return False
    if requested_quality == "HI_RES_LOSSLESS":
        try:
            if int(response.get("bitDepth") or 0) != 24:
                return False
        except (TypeError, ValueError):
            return False
        if returned_quality != "HI_RES_LOSSLESS":
            return False
    return True


async def _convert_dash_to_flac(
    session, manifest: ET.Element, raw_manifest: bytes, destination: str, callback
) -> None:
    import asyncio
    import shutil

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required for Tidal DASH FLAC downloads")
    from pathlib import Path

    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deadmoon-tidal-dash-", dir=output.parent) as temporary:
        manifest_path = Path(temporary) / "manifest.mpd"
        manifest_path.write_bytes(raw_manifest)
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file,crypto,data,http,https,tcp,tls",
            "-y", "-i", str(manifest_path), "-map", "0:a:0", "-c:a", "flac",
            str(output),
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            output.unlink(missing_ok=True)
            detail = stderr.decode("utf-8", "replace")[-1000:]
            for item in manifest.iter():
                for key, value in list(item.attrib.items()):
                    if value.startswith(("http://", "https://")):
                        detail = detail.replace(value, "<redacted-url>")
            raise RuntimeError(f"Tidal DASH FLAC conversion failed: {detail}")
    if output.read_bytes()[:4] != b"fLaC":
        output.unlink(missing_ok=True)
        raise RuntimeError("Tidal DASH conversion did not produce native FLAC")
    from mutagen.flac import FLAC

    audio = FLAC(output)
    if audio.info.bits_per_sample < 16 or audio.info.length <= 0:
        output.unlink(missing_ok=True)
        raise RuntimeError("Tidal DASH conversion produced invalid FLAC audio")
    if callable(callback):
        result = callback(output.stat().st_size)
        if inspect.isawaitable(result):
            await result


async def _downloadable_from_manifest(self, response: dict):
    from streamrip.client.downloadable import BasicDownloadable, TidalDownloadable
    from streamrip.exceptions import NonStreamableError

    raw_manifest = base64.b64decode(response["manifest"])
    kind, manifest = _decode_manifest(response)
    codecs = _manifest_codecs(kind, manifest)
    if not codecs.casefold().startswith(("flac", "mqa")):
        raise NonStreamableError(f"Rejected non-FLAC Tidal manifest codec: {codecs or 'unknown'}")
    if kind == "dash":
        downloadable = BasicDownloadable(self.session, "", "flac", "tidal")

        async def download_dash(path, callback):
            await _convert_dash_to_flac(self.session, manifest, raw_manifest, path, callback)

        async def estimated_size():
            duration = 0.0
            bandwidth = 0
            for item in manifest.iter():
                name = item.tag.rsplit("}", 1)[-1]
                if name == "MPD":
                    value = item.attrib.get("mediaPresentationDuration", "")
                    try:
                        duration = float(value.removeprefix("PT").removesuffix("S"))
                    except ValueError:
                        pass
                elif name == "Representation":
                    bandwidth = max(bandwidth, int(item.attrib.get("bandwidth") or 0))
            return max(1, int(duration * bandwidth / 8)) if duration and bandwidth else 1

        downloadable._download = download_dash
        downloadable.size = estimated_size
        return downloadable
    encryption_key = None if manifest.get("encryptionType") == "NONE" else manifest.get("keyId")
    return TidalDownloadable(
        self.session,
        url=manifest["urls"][0],
        codec=codecs,
        encryption_key=encryption_key,
        restrictions=manifest.get("restrictions"),
    )


def _playback_rejected(value) -> bool:
    if isinstance(value, dict):
        return _sub_status(value) == 4005 or (
            not value.get("manifest")
            and any(key in value for key in (
                "status", "subStatus", "sub_status", "userMessage", "error"
            ))
        )
    return _error_status(value) in (401, 403) or _sub_status(value) == 4005


def _valid_tidal_session(payload, user_id) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("status", 200) == 200
        and payload.get("userId") is not None
        and str(payload["userId"]) == str(user_id)
    )


def _claim(token: str, name: str):
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        return payload.get(name)
    except (IndexError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


def _token_expiry(client) -> float:
    claim = _claim(str(client.config.access_token or ""), "exp")
    try:
        return float(claim if claim is not None else client.token_expiry)
    except (AttributeError, TypeError, ValueError):
        return 0


def patch_tidal_client(client_class) -> None:
    """Patch only the process-local Tidal client used by this invocation."""
    from streamrip.client import tidal as tidal_module

    _patch_tidal_credentials(tidal_module, client_class)
    original_login = getattr(client_class, "login", None)
    original_access_login = client_class._login_by_access_token
    original_request = client_class._api_request
    original_post = getattr(client_class, "_api_post", None)
    original_downloadable = getattr(client_class, "get_downloadable", None)
    default_base = _request_base(original_request)

    async def login_with_bearer(self, token: str, user_id: str):
        _set_bearer(self, token)
        result = await original_access_login(self, token, user_id)
        _set_bearer(self)
        return result

    async def login_without_premature_refresh(self):
        if original_login is None:
            return None
        if _token_expiry(self) > time.time() + 300:
            self.session = await self.get_session(
                verify_ssl=self.global_config.session.downloads.verify_ssl
            )
            self.refresh_token = self.config.refresh_token
            await login_with_bearer(self, self.config.access_token, self.config.user_id)
            self.logged_in = True
            return None
        return await original_login(self)

    async def refresh_with_validated_fallback(self):
        _set_bearer(self)
        last_status = None
        for extra in ({"scope": "r_usr w_usr w_sub"}, {}):
            data = {
                "client_id": TIDAL_CLIENT_ID,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
                **extra,
            }
            response = await original_post(self, _TOKEN_URL, data, tidal_module.AUTH)
            last_status = response.get("status") if isinstance(response, dict) else None
            access_token = str(response.get("access_token") or "") if isinstance(response, dict) else ""
            if not access_token:
                if last_status in (400, 401, 403) and extra:
                    continue
                break
            async with self.session.get(
                _SESSIONS_URL, headers={"Authorization": f"Bearer {access_token}"}
            ) as session_response:
                session_payload = await session_response.json(content_type=None)
            if not _valid_tidal_session(session_payload, self.config.user_id):
                raise RuntimeError("Tidal refresh returned an unverified access token")
            self.config.access_token = access_token
            self.refresh_token = str(response.get("refresh_token") or self.refresh_token)
            self.config.refresh_token = self.refresh_token
            expiry = _claim(access_token, "exp")
            self.config.token_expiry = float(
                expiry or time.time() + int(response.get("expires_in") or 86400)
            )
            self.token_expiry = self.config.token_expiry
            country = _claim(access_token, "cc") or session_payload.get("countryCode")
            if country:
                self.config.country_code = str(country)
            _set_bearer(self)
            return response
        raise RuntimeError(f"Tidal token refresh failed (status={last_status})")

    def playback_attempts(path, params, base):
        normalized = str(base or default_base).rstrip("/")
        playback_path = next(
            (suffix for suffix in _PLAYBACK_PATHS if path.rstrip("/").endswith(suffix)),
            None,
        )
        if playback_path is None:
            attempts = [(path, normalized, dict(params))]
            if normalized.casefold() == _TIDAL_HOST:
                attempts.append((path, _TIDAL_API_HOST, dict(params)))
            return attempts
        requested = str(params.get("audioquality") or _PLAYBACK_QUALITIES[0])
        if requested == "HI_RES":
            requested = "HI_RES_LOSSLESS"
        qualities = [requested]
        quality = _next_quality(requested)
        while quality:
            qualities.append(quality)
            quality = _next_quality(quality)
        hosts = [normalized, *[host for host in _TIDAL_PLAYBACK_HOSTS if host != normalized]]
        paths = [path]
        if playback_path == _PLAYBACK_PATHS[0]:
            paths.append(path[: -len(_PLAYBACK_PATHS[0])] + _PLAYBACK_PATHS[1])
        return [
            (attempt_path, host, {**params, "audioquality": attempt_quality})
            for attempt_path in paths
            for attempt_quality in qualities
            for host in hosts
        ]

    async def request_with_fallback(self, path: str, params=None, base=default_base):
        normalized = str(base or default_base).rstrip("/")
        lyrics = path.rstrip("/").endswith("/lyrics")
        playback = any(path.rstrip("/").endswith(suffix) for suffix in _PLAYBACK_PATHS)
        request_params = dict(params or {})
        if playback and getattr(self.config, "country_code", None):
            request_params["countryCode"] = str(self.config.country_code)
        attempts = playback_attempts(path, request_params, normalized)
        last_result = None
        for index, (attempt_path, attempt_base, attempt_params) in enumerate(attempts):
            _set_bearer(self)
            try:
                result = await original_request(
                    self, attempt_path, dict(attempt_params), base=attempt_base
                )
                if playback:
                    requested_quality = str(attempt_params.get("audioquality") or "")
                    rejected = _playback_rejected(result) or (
                        requested_quality in _LOSSLESS_QUALITIES
                        and not _lossless_manifest(result, requested_quality)
                    )
                    if rejected and index + 1 < len(attempts):
                        last_result = result
                        continue
                    if rejected:
                        from streamrip.exceptions import NonStreamableError
                        raise NonStreamableError("Tidal returned no lossless FLAC manifest")
                return result
            except Exception as error:
                if lyrics:
                    if _error_status(error) in (401, 403) and index + 1 < len(attempts):
                        continue
                    if _unavailable_lyrics(error, lyrics):
                        logger.warning("TIDAL lyrics unavailable; continuing without lyrics")
                        return {}
                retryable = _playback_rejected(error) if playback else _error_status(error) in (401, 403)
                if not retryable or index + 1 == len(attempts):
                    raise
        return last_result

    async def downloadable_with_fallback(self, track_id: str, quality: int):
        quality = max(0, min(int(quality), 3))
        requested_quality = "HI_RES_LOSSLESS" if quality >= 2 else "LOSSLESS"
        params = {
            "audioquality": requested_quality,
            "playbackmode": "STREAM",
            "assetpresentation": "FULL",
            "countryCode": str(self.config.country_code),
        }
        response = await request_with_fallback(
            self, f"tracks/{track_id}/playbackinfopostpaywall", params
        )
        if not _lossless_manifest(response, requested_quality):
            from streamrip.exceptions import NonStreamableError
            raise NonStreamableError("Tidal returned no lossless FLAC manifest")
        return await _downloadable_from_manifest(self, response)

    if original_post is not None:
        async def post_with_bearer(self, url, data, auth=None):
            _set_bearer(self)
            result = await original_post(self, url, data, auth)
            _set_bearer(self)
            return result

        client_class._api_post = post_with_bearer
    if original_login is not None:
        client_class.login = login_without_premature_refresh
    if original_downloadable is not None:
        client_class.get_downloadable = downloadable_with_fallback
    if hasattr(client_class, "_refresh_access_token"):
        client_class._refresh_access_token = refresh_with_validated_fallback
    client_class._login_by_access_token = login_with_bearer
    client_class._api_request = request_with_fallback


def patch_tidal() -> None:
    from streamrip.client.tidal import TidalClient

    patch_tidal_client(TidalClient)


def _runtime_config_path() -> tuple[str, str, object]:
    """Keep the runtime config alive until Streamrip has saved refreshed tokens."""
    import dataclasses
    import tempfile
    from pathlib import Path

    import tomlkit
    from streamrip.config import TidalConfig

    path = Path(sys.argv[sys.argv.index("--config-path") + 1])
    if "token_type" in {item.name for item in dataclasses.fields(TidalConfig)}:
        return str(path), str(path), None
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    document.get("tidal", {}).pop("token_type", None)
    temporary = tempfile.TemporaryDirectory(prefix="deadmoon-streamrip-")
    compatible = Path(temporary.name) / "config.toml"
    compatible.write_text(document.as_string(), encoding="utf-8")
    return str(compatible), str(path), temporary


def main() -> None:
    patch_tidal()
    if "--max-connections" in sys.argv:
        index = sys.argv.index("--max-connections")
        del sys.argv[index:index + 2]
    if "--config-path" not in sys.argv:
        try:
            from streamrip.rip.main import rip
        except ImportError:
            from streamrip.rip import rip
        rip()
        return
    config_path, original_path, temporary = _runtime_config_path()
    try:
        from streamrip.rip.main import rip
    except ImportError:
        from streamrip.rip import rip
    index = sys.argv.index("--config-path")
    sys.argv[index + 1] = config_path
    try:
        rip()
    finally:
        if temporary is not None:
            from pathlib import Path
            import tomlkit

            runtime = Path(config_path)
            original = Path(original_path)
            if runtime.is_file():
                document = tomlkit.parse(runtime.read_text(encoding="utf-8"))
                document["tidal"]["token_type"] = "Bearer"
                original.write_text(document.as_string(), encoding="utf-8")
            temporary.cleanup()


if __name__ == "__main__":
    sys.exit(main())
