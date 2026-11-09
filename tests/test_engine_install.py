import json
import base64
"""Offline checks for the direct-engine engine install: imports, CLI entry points, pins.

The engine dependency graph in requirements.txt + the Dockerfile overrides three
declared version caps (streamrip's Pillow/tomlkit/rich, pyplayready's
cryptography) with --no-deps installs. Overriding a cap is
only safe while the newer version actually runs the code, so this module asserts
that at the call sites the bot really uses rather than at import level alone.

Run with `python3 -m unittest discover tests`. No network, no Telegram, no
credentials: nothing here performs a download.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")


def _have(module: str) -> bool:
    return importlib.util.find_spec(module) is not None



class TestStreamripTidalRunner(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _error(status):
        import aiohttp

        return aiohttp.ClientResponseError(
            request_info=None, history=(), status=status, message="Unauthorized"
        )

    @staticmethod
    def _client():
        import types

        return types.SimpleNamespace(
            config=types.SimpleNamespace(token_type="", access_token="secret-token"),
            session=types.SimpleNamespace(headers={}),
        )

    def test_command_uses_runner_and_connection_limit(self):
        from dl_utils import provider_engines as pe

        command = pe._streamrip_command(Path("/tmp/config.toml"), "https://tidal.com/album/1")
        self.assertEqual(command[1:3], ["-m", "dl_utils.streamrip_runner"])
        self.assertIn("--max-connections", command)
        self.assertIn("--no-progress", command)

    async def test_login_enforces_bearer_before_and_after_streamrip_login(self):
        from dl_utils import streamrip_runner

        client = self._client()

        class FakeClient:
            async def _login_by_access_token(self, token, user_id):
                assert self.config.token_type == "Bearer"
                assert self.session.headers["Authorization"] == "Bearer secret-token"
                self.session.headers["Authorization"] = " secret-token"
                return "logged-in"

            async def _api_request(self, path, params=None, base="https://tidal.com/v1"):
                return {}

        streamrip_runner.patch_tidal_client(FakeClient)
        result = await self._run_login(FakeClient, client)
        self.assertEqual(result, "logged-in")
        self.assertEqual(client.config.token_type, "Bearer")
        self.assertEqual(client.session.headers["Authorization"], "Bearer secret-token")

    @staticmethod
    async def _run_login(client_class, client):
        return await client_class._login_by_access_token(client, "secret-token", "1")

    def test_patch_replaces_module_and_class_client_credentials(self):
        from streamrip.client import tidal as tidal_module
        from dl_utils import streamrip_runner

        class FakeClient:
            CLIENT_ID = "old-id"
            CLIENT_SECRET = "old-secret"

            async def _login_by_access_token(self, token, user_id):
                return None

            async def _api_request(self, path, params=None, base="https://api.tidal.com/v1"):
                return {}

        streamrip_runner.patch_tidal_client(FakeClient)
        self.assertEqual(tidal_module.CLIENT_ID, streamrip_runner.TIDAL_CLIENT_ID)
        self.assertEqual(tidal_module.CLIENT_SECRET, streamrip_runner.TIDAL_CLIENT_SECRET)
        self.assertEqual(tidal_module.AUTH.login, streamrip_runner.TIDAL_CLIENT_ID)
        self.assertEqual(tidal_module.AUTH.password, streamrip_runner.TIDAL_CLIENT_SECRET)
        self.assertEqual(FakeClient.CLIENT_ID, streamrip_runner.TIDAL_CLIENT_ID)
        self.assertEqual(FakeClient.CLIENT_SECRET, streamrip_runner.TIDAL_CLIENT_SECRET)

    async def test_valid_jwt_skips_premature_refresh(self):
        import base64
        import json
        import time
        from dl_utils import streamrip_runner

        expiry = int(time.time()) + 600
        payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
        token = f"header.{payload}.signature"
        client = self._client()
        client.config.access_token = token
        client.config.refresh_token = "refresh"
        client.config.user_id = "1"
        client.token_expiry = 1
        client.global_config = types.SimpleNamespace(
            session=types.SimpleNamespace(downloads=types.SimpleNamespace(verify_ssl=True))
        )
        calls = []

        class FakeClient:
            async def login(self):
                raise AssertionError("upstream prematurely refreshed the token")

            async def get_session(self, verify_ssl=True):
                calls.append(("session", verify_ssl))
                return self.session

            async def _login_by_access_token(self, token, user_id):
                calls.append(("login", token, user_id))

            async def _refresh_access_token(self):
                raise AssertionError("refresh must not run")

            async def _api_request(self, path, params=None, base="https://api.tidal.com/v1"):
                return {}

        client.session = types.SimpleNamespace(headers={})
        client.get_session = types.MethodType(FakeClient.get_session, client)
        streamrip_runner.patch_tidal_client(FakeClient)
        await FakeClient.login(client)
        self.assertEqual(calls, [("session", True), ("login", token, "1")])
        self.assertTrue(client.logged_in)

    async def test_refresh_retries_without_scope_and_verifies_token(self):
        import base64
        import json
        import time
        from dl_utils import streamrip_runner

        expiry = int(time.time()) + 3600
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": expiry, "cc": "CA"}).encode()
        ).decode().rstrip("=")
        token = f"header.{payload}.signature"
        client = self._client()
        client.config.user_id = "1"
        client.config.refresh_token = "refresh"
        client.refresh_token = "refresh"
        posts = []
        session_requests = []

        class Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def json(self, content_type=None):
                return {"status": 200, "userId": 1, "countryCode": "CA"}

        class Session:
            def __init__(self):
                self.headers = {}

            def get(self, url, headers=None):
                session_requests.append((url, headers))
                return Response()

        client.session = Session()

        class FakeClient:
            async def _login_by_access_token(self, token, user_id):
                return None

            async def _api_request(self, path, params=None, base="https://api.tidal.com/v1"):
                return {}

            async def _api_post(self, url, data, auth=None):
                posts.append((url, data.copy(), auth))
                if "scope" in data:
                    return {"status": 403, "error": "invalid_scope"}
                return {"access_token": token, "refresh_token": "rotated", "expires_in": 3600}

            async def _refresh_access_token(self):
                raise AssertionError("replaced by patch")

        streamrip_runner.patch_tidal_client(FakeClient)
        await FakeClient._refresh_access_token(client)
        self.assertEqual(len(posts), 2)
        self.assertIn("scope", posts[0][1])
        self.assertNotIn("scope", posts[1][1])
        self.assertEqual(posts[1][2].login, streamrip_runner.TIDAL_CLIENT_ID)
        self.assertEqual(session_requests[0][0], "https://api.tidal.com/v1/sessions")
        self.assertEqual(client.config.access_token, token)
        self.assertEqual(client.refresh_token, "rotated")
        self.assertEqual(client.config.token_expiry, expiry)
        self.assertEqual(client.config.country_code, "CA")

    async def test_api_request_retries_tidal_host_with_bearer(self):
        from dl_utils import streamrip_runner

        client = self._client()
        calls = []

        class FakeClient:
            async def _login_by_access_token(self, token, user_id):
                return None

            async def _api_request(self, path, params=None, base="https://tidal.com/v1"):
                calls.append((path, params, base, self.session.headers["Authorization"]))
                if base == "https://tidal.com/v1":
                    raise TestStreamripTidalRunner._error(401)
                return {"manifest": "ok"}

        streamrip_runner.patch_tidal_client(FakeClient)
        result = await FakeClient._api_request(client, "tracks/1", {"audioquality": "LOSSLESS"})
        self.assertEqual(result, {"manifest": "ok"})
        self.assertEqual([call[2] for call in calls], ["https://tidal.com/v1", "https://api.tidal.com/v1"])
        self.assertTrue(all(call[3] == "Bearer secret-token" for call in calls))
        self.assertEqual(calls[0][1], calls[1][1])

    async def test_lyrics_auth_and_missing_errors_do_not_abort_tracks(self):
        from dl_utils import streamrip_runner

        for first_status, retry_status in ((401, 403), (403, 404), (404, None)):
            with self.subTest(first_status=first_status, retry_status=retry_status):
                client = self._client()
                calls = []

                class FakeClient:
                    async def _login_by_access_token(self, token, user_id):
                        return None

                    async def _api_request(self, path, params=None, base="https://tidal.com/v1"):
                        calls.append(base)
                        status = first_status if len(calls) == 1 else retry_status
                        raise TestStreamripTidalRunner._error(status)

                streamrip_runner.patch_tidal_client(FakeClient)
                result = await FakeClient._api_request(client, "tracks/1/lyrics")
                self.assertEqual(result, {})
                expected_calls = 1 if first_status == 404 else 2
                self.assertEqual(len(calls), expected_calls)

    async def test_streamrip_non_streamable_lyrics_404_is_nonfatal(self):
        from streamrip.exceptions import NonStreamableError
        from dl_utils import streamrip_runner

        client = self._client()

        class FakeClient:
            async def _login_by_access_token(self, token, user_id):
                return None

            async def _api_request(self, path, params=None, base="https://listen.tidal.com/v1"):
                raise NonStreamableError("TIDAL: Track not found")

        streamrip_runner.patch_tidal_client(FakeClient)
        result = await FakeClient._api_request(client, "tracks/1/lyrics")
        self.assertEqual(result, {})


    async def test_non_lyrics_retry_failure_remains_fatal(self):
        from dl_utils import streamrip_runner

        client = self._client()

        class FakeClient:
            async def _login_by_access_token(self, token, user_id):
                return None

            async def _api_request(self, path, params=None, base="https://tidal.com/v1"):
                raise TestStreamripTidalRunner._error(401)

        streamrip_runner.patch_tidal_client(FakeClient)
        with self.assertRaises(Exception) as raised:
            await FakeClient._api_request(client, "tracks/1/playbackinfopostpaywall")
        self.assertEqual(getattr(raised.exception, "status", None), 401)


    async def test_playback_retries_hosts_and_lower_quality_without_client_header(self):
        from dl_utils import streamrip_runner

        client = self._client()
        client.session.headers["x-tidal-token"] = "conflicting-client"
        calls = []

        class FakeClient:
            async def _login_by_access_token(self, token, user_id):
                return None

            async def _api_request(self, path, params=None, base="https://api.tidalhifi.com/v1"):
                calls.append((base, params.copy(), dict(self.session.headers)))
                if params["audioquality"] != "LOSSLESS":
                    raise TestStreamripTidalRunner._error(401)
                return {
                    "audioQuality": "LOSSLESS",
                    "manifestMimeType": "application/vnd.tidal.bts",
                    "manifest": base64.b64encode(json.dumps({
                        "codecs": "flac", "encryptionType": "NONE", "urls": ["https://example.invalid/flac"]
                    }).encode()).decode(),
                }

        streamrip_runner.patch_tidal_client(FakeClient)
        result = await FakeClient._api_request(
            client, "tracks/1/playbackinfopostpaywall", {"audioquality": "HI_RES"}
        )
        self.assertEqual(result["audioQuality"], "LOSSLESS")
        self.assertEqual(
            [(base, params["audioquality"]) for base, params, _ in calls],
            [
                ("https://api.tidalhifi.com/v1", "HI_RES_LOSSLESS"),
                ("https://api.tidal.com/v1", "HI_RES_LOSSLESS"),
                ("https://desktop.tidal.com/v1", "HI_RES_LOSSLESS"),
                ("https://api.tidalhifi.com/v1", "HI_RES"),
                ("https://api.tidal.com/v1", "HI_RES"),
                ("https://desktop.tidal.com/v1", "HI_RES"),
                ("https://api.tidalhifi.com/v1", "LOSSLESS"),
            ],
        )
        self.assertTrue(all("x-tidal-token" not in headers for _, _, headers in calls))
        self.assertTrue(all(
            sum(key.casefold() == "authorization" for key in headers) == 1
            for _, _, headers in calls
        ))
        self.assertTrue(all(headers["Authorization"] == "Bearer secret-token" for _, _, headers in calls))

    async def test_playback_rejects_aac_downgrade_without_high_or_low_fallback(self):
        from dl_utils import streamrip_runner

        client = self._client()
        calls = []
        aac = {
            "audioQuality": "HIGH",
            "manifestMimeType": "application/vnd.tidal.bts",
            "manifest": base64.b64encode(json.dumps({
                "codecs": "mp4a.40.2", "encryptionType": "NONE", "urls": ["https://example.invalid/aac"]
            }).encode()).decode(),
        }

        class FakeClient:
            async def _login_by_access_token(self, token, user_id):
                return None

            async def _api_request(self, path, params=None, base="https://api.tidalhifi.com/v1"):
                calls.append(params["audioquality"])
                return aac

        streamrip_runner.patch_tidal_client(FakeClient)
        from streamrip.exceptions import NonStreamableError
        with self.assertRaises(NonStreamableError):
            await FakeClient._api_request(
                client, "tracks/1/playbackinfopostpaywall", {"audioquality": "HI_RES"}
            )
        self.assertEqual(len(calls), 18)
        self.assertNotIn("HIGH", calls)
        self.assertNotIn("LOW", calls)
        self.assertTrue(all(not streamrip_runner._lossless_manifest(aac, quality) for quality in (
            "HI_RES_LOSSLESS", "HI_RES", "LOSSLESS"
        )))

    async def test_dash_flac_manifest_is_accepted_and_downloadable(self):
        from dl_utils import streamrip_runner

        response = {
            "audioQuality": "HI_RES_LOSSLESS", "bitDepth": 24, "sampleRate": 48000,
            "manifestMimeType": "application/dash+xml",
            "manifest": base64.b64encode(
                b'<MPD mediaPresentationDuration="PT10S"><Representation codecs="fLaC" bandwidth="1000000" /></MPD>'
            ).decode(),
        }
        self.assertTrue(streamrip_runner._lossless_manifest(response, "HI_RES_LOSSLESS"))
        kind, manifest = streamrip_runner._decode_manifest(response)
        self.assertEqual(kind, "dash")
        self.assertEqual(streamrip_runner._manifest_codecs(kind, manifest), "fLaC")
        client = self._client()
        downloadable = await streamrip_runner._downloadable_from_manifest(client, response)
        self.assertEqual(downloadable.extension, "flac")
        self.assertEqual(await downloadable.size(), 1250000)

    async def test_playback_substatus_4005_retries_with_jwt_country(self):
        from dl_utils import streamrip_runner

        client = self._client()
        client.config.country_code = "CA"
        client.session.headers["X-Tidal-Token"] = "conflicting-client"
        calls = []

        class FakeClient:
            async def _login_by_access_token(self, token, user_id):
                return None

            async def _api_request(self, path, params=None, base="https://api.tidalhifi.com/v1"):
                calls.append((base, params.copy(), dict(self.session.headers)))
                if params["audioquality"] == "HI_RES_LOSSLESS":
                    return {"status": 200, "subStatus": 4005}
                return {
                    "audioQuality": "HI_RES_LOSSLESS", "bitDepth": 24,
                    "manifestMimeType": "application/dash+xml",
                    "manifest": base64.b64encode(b'<MPD><Representation codecs="flac" /></MPD>').decode(),
                }

        streamrip_runner.patch_tidal_client(FakeClient)
        result = await FakeClient._api_request(
            client, "tracks/1/playbackinfopostpaywall", {"audioquality": "HI_RES", "countryCode": "US"}
        )
        self.assertEqual(result["audioQuality"], "HI_RES_LOSSLESS")
        self.assertEqual(
            [call[1]["audioquality"] for call in calls],
            ["HI_RES_LOSSLESS", "HI_RES_LOSSLESS", "HI_RES_LOSSLESS", "HI_RES"],
        )
        self.assertTrue(all(call[1]["countryCode"] == "CA" for call in calls))
        self.assertTrue(all(
            not any(key.casefold() == "x-tidal-token" for key in headers)
            for _, _, headers in calls
        ))

    async def test_playback_falls_back_to_playbackinfo(self):
        from dl_utils import streamrip_runner

        client = self._client()
        calls = []

        class FakeClient:
            async def _login_by_access_token(self, token, user_id):
                return None

            async def _api_request(self, path, params=None, base="https://api.tidalhifi.com/v1"):
                calls.append((path, base, params.copy()))
                raise TestStreamripTidalRunner._error(403)

        streamrip_runner.patch_tidal_client(FakeClient)
        with self.assertRaises(Exception):
            await FakeClient._api_request(
                client, "tracks/1/playbackinfopostpaywall", {"audioquality": "HI_RES"}
            )
        self.assertEqual(len(calls), 18)
        self.assertTrue(calls[-1][0].endswith("/playbackinfo"))
        self.assertEqual([call[2]["audioquality"] for call in calls[-3:]], ["LOSSLESS"] * 3)

    async def test_get_downloadable_retries_nonstreamable_manifest(self):
        from dl_utils import streamrip_runner

        client = self._client()
        client.config.country_code = "US"
        calls = []

        class FakeClient:
            async def _login_by_access_token(self, token, user_id):
                return None

            async def _api_request(self, path, params=None, base="https://api.tidalhifi.com/v1"):
                calls.append(params["audioquality"])
                return {
                    "audioQuality": "HI_RES_LOSSLESS", "bitDepth": 24,
                    "manifestMimeType": "application/dash+xml",
                    "manifest": base64.b64encode(
                        b'<MPD mediaPresentationDuration="PT2S"><Representation codecs="flac" bandwidth="8000" /></MPD>'
                    ).decode(),
                }

            async def get_downloadable(self, track_id, quality):
                raise AssertionError("upstream parser must not receive a DASH manifest")

        streamrip_runner.patch_tidal_client(FakeClient)
        downloadable = await FakeClient.get_downloadable(client, "1", 3)
        self.assertEqual(downloadable.extension, "flac")
        self.assertEqual(calls, ["HI_RES_LOSSLESS"])

    async def test_api_post_enforces_bearer(self):
        from dl_utils import streamrip_runner

        client = self._client()
        seen = []

        class FakeClient:
            async def _login_by_access_token(self, token, user_id):
                return None

            async def _api_request(self, path, params=None, base="https://api.tidal.com/v1"):
                return {}

            async def _api_post(self, url, data, auth=None):
                seen.append((url, data, auth, self.session.headers["Authorization"]))
                return {"ok": True}

        streamrip_runner.patch_tidal_client(FakeClient)
        result = await FakeClient._api_post(client, "https://auth.example/token", {"grant": "refresh"}, "auth")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen[0][3], "Bearer secret-token")

    def test_jwt_country_overrides_conflicting_inline_country(self):
        from unittest import mock
        from dl_utils import platform_config as pcfg

        import base64
        import json

        payload = base64.urlsafe_b64encode(
            json.dumps({"uid": 209656887, "exp": 2000000000, "cc": "CA"}).encode()
        ).decode().rstrip("=")
        token = f"header.{payload}.signature"
        fields = {
            "user_id": "209656887", "access_token": token, "refresh_token": "refresh",
            "country_code": "US", "token_expiry": "2000000000", "token_type": "not-bearer",
        }
        with mock.patch.object(pcfg, "_toml_credentials", return_value=fields), mock.patch.object(
            pcfg, "get_env_or_token_file", side_effect=lambda key, default="": "US" if key == "TIDAL_COUNTRY_CODE" else default
        ):
            resolved = pcfg.tidal_oauth_fields()
        self.assertEqual(resolved["country_code"], "CA")
        self.assertEqual(resolved["token_type"], "Bearer")


def _cli(name: str) -> str | None:
    """Resolve an engine CLI the same way dl_utils.provider_engines._which does."""
    found = shutil.which(name)
    if found:
        return found
    local = Path(__file__).resolve().parent.parent / "bin" / name
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    return None


def _runs(cli: str) -> tuple[bool, str]:
    """`--help` exits 0 only if the whole entry-point import chain loads."""
    try:
        proc = subprocess.run(
            [cli, "--help"],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as e:
        return False, str(e)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return False, f"exit {proc.returncode}: {' / '.join(tail)}"
    return True, ""


class TestEngineEntryPoints(unittest.TestCase):
    """Every engine the bot can dispatch to must actually start."""

    ENGINES = ("rip", "gamdl", "dotify")

    def test_installed_engines_start(self):
        missing = [n for n in self.ENGINES if _cli(n) is None]
        if len(missing) == len(self.ENGINES):
            self.skipTest("no direct-engine engines installed in this environment")
        for name in self.ENGINES:
            if name in missing:
                continue
            with self.subTest(engine=name):
                ok, err = _runs(_cli(name))
                # A half-installed engine is worse than an absent one: the
                # dispatcher picks it and every request for that platform dies
                # at exec time instead of falling back.
                self.assertTrue(ok, f"{name} --help failed: {err}")

    def test_streamrip_config_api(self):
        if not _have("streamrip"):
            self.skipTest("streamrip is not installed in this environment")
        from streamrip.config import Config, set_user_defaults  # noqa: F401


@unittest.skipUnless(_have("streamrip"), "streamrip is not installed")
class TestStreamripOverrides(unittest.TestCase):
    """streamrip caps Pillow<11 and tomlkit<0.8; the bot runs newer. Prove it."""

    def test_artwork_downscale_on_modern_pillow(self):
        from PIL import Image

        from streamrip.media.artwork import downscale_image

        path = Path(self.tmp.name) / "cover.jpg"  # type: ignore[attr-defined]
        Image.new("RGB", (1400, 1400), (10, 20, 30)).save(path, "JPEG")
        downscale_image(str(path), 1000)
        with Image.open(path) as im:
            self.assertEqual(max(im.size), 1000)

    def test_config_written_by_tomlkit_13_round_trips(self):
        """provider_engines._streamrip_config writes with tomlkit >=0.13."""
        import tempfile

        import tomlkit
        from streamrip.config import Config, set_user_defaults

        d = Path(tempfile.mkdtemp(prefix="deadmoon-sr-"))
        cfg_path = d / "config.toml"
        set_user_defaults(str(cfg_path))

        doc = tomlkit.parse(cfg_path.read_text())
        doc["downloads"]["folder"] = str(d)
        doc["downloads"]["requests_per_minute"] = 30
        doc["qobuz"]["quality"] = 3
        doc["qobuz"]["use_auth_token"] = True
        doc["qobuz"]["email_or_userid"] = "12345"
        doc["qobuz"]["password_or_token"] = "secret"
        doc["qobuz"]["app_id"] = "9999"
        doc["qobuz"]["secrets"] = ["aaa", "bbb"]
        cfg_path.write_text(doc.as_string())

        loaded = Config(str(cfg_path)).session
        self.assertEqual(loaded.qobuz.quality, 3)
        self.assertTrue(loaded.qobuz.use_auth_token)
        self.assertEqual(loaded.qobuz.app_id, "9999")
        self.assertEqual(loaded.qobuz.secrets, ["aaa", "bbb"])
        self.assertEqual(loaded.downloads.requests_per_minute, 30)

    def test_generated_tidal_config_matches_real_schema(self):
        import dataclasses

        from streamrip.config import Config, TidalConfig

        from dl_utils import platform_config as pcfg
        from dl_utils import provider_engines as pe

        fields = {
            "user_id": 123,
            "country_code": "US",
            "access_token": "test-access-token",
            "refresh_token": "test-refresh-token",
            "token_expiry": 2000000000,
            "token_type": "Bearer",
        }
        with unittest.mock.patch.object(pcfg, "has_tidal_credentials", return_value=True), unittest.mock.patch.object(
            pcfg, "tidal_oauth_fields", return_value=fields
        ):
            path = pe._streamrip_config(pcfg.TIDAL, Path(self.tmp.name))
        accepted = {item.name for item in dataclasses.fields(TidalConfig)}
        import tomlkit
        document = tomlkit.parse(path.read_text())
        self.assertEqual(document["tidal"]["token_type"], "Bearer")
        self.assertLessEqual(set(document["tidal"]) - {"token_type"}, accepted)
        if "token_type" in accepted:
            loaded = Config(str(path)).session.tidal
            self.assertEqual(loaded.token_type, "Bearer")
            self.assertEqual(loaded.quality, 3)
        else:
            from unittest import mock
            from dl_utils import streamrip_runner

            argv = ["rip", "--config-path", str(path)]
            with mock.patch.object(sys, "argv", argv):
                runtime_path, original_path, temporary = streamrip_runner._runtime_config_path()
            try:
                loaded = Config(runtime_path).session.tidal
                self.assertEqual(loaded.quality, 3)
                self.assertEqual(original_path, str(path))
                self.assertNotIn("token_type", tomlkit.parse(Path(runtime_path).read_text())["tidal"])
            finally:
                temporary.cleanup()
        for key in ("user_id", "country_code", "access_token", "refresh_token", "token_expiry"):
            self.assertIsInstance(document["tidal"][key], str)

    def test_real_config_loads_bearer_with_token_type_schema(self):
        import dataclasses
        from unittest import mock

        import streamrip.config as streamrip_config

        from dl_utils import platform_config as pcfg
        from dl_utils import provider_engines as pe

        @dataclasses.dataclass(slots=True)
        class TokenTypeTidalConfig(streamrip_config.TidalConfig):
            token_type: str = ""

        fields = {
            "user_id": "123", "country_code": "US", "access_token": "access",
            "refresh_token": "refresh", "token_expiry": "2000000000",
        }
        with mock.patch.object(pcfg, "has_tidal_credentials", return_value=True), mock.patch.object(
            pcfg, "tidal_oauth_fields", return_value=fields
        ):
            path = pe._streamrip_config(pcfg.TIDAL, Path(self.tmp.name))
        with mock.patch.object(streamrip_config, "TidalConfig", TokenTypeTidalConfig):
            loaded = streamrip_config.Config(str(path)).session.tidal
        self.assertEqual(loaded.token_type, "Bearer")

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory(prefix="deadmoon-sr-art-")
        self.addCleanup(self.tmp.cleanup)


@unittest.skipUnless(_have("pyplayready"), "pyplayready is not installed")
class TestPlayReadyOverrides(unittest.TestCase):
    """Verify pyplayready with the explicitly pinned cryptography runtime."""

    def test_wrapping_key_and_aes_keywrap_round_trip(self):
        from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, aes_key_wrap
        from pyplayready.crypto.key_wrap import derive_wrapping_key

        root = derive_wrapping_key()
        self.assertEqual(len(root), 16)

        content_key = b"0123456789abcdef0123456789abcdef"
        wrapped = aes_key_wrap(root, content_key)
        self.assertNotEqual(wrapped, content_key)
        self.assertEqual(aes_key_unwrap(root, wrapped), content_key)

    def test_revocation_list_module_loads(self):
        """The other cryptography consumer in pyplayready (rsa/hashes)."""
        from pyplayready.misc import revocation_list  # noqa: F401


@unittest.skipUnless(_have("pywidevine"), "pywidevine is not installed")
class TestWidevineProtobuf(unittest.TestCase):
    """Dotify and Widevine share the modern protobuf runtime."""

    def test_license_protocol_messages_serialise(self):
        from pywidevine.license_protocol_pb2 import License, LicenseRequest, SignedMessage

        req = LicenseRequest()
        req.type = 1
        req.request_time = 1730000000
        blob = req.SerializeToString()
        back = LicenseRequest()
        back.ParseFromString(blob)
        self.assertEqual(back.request_time, 1730000000)

        for msg in (SignedMessage(), License()):
            self.assertTrue(msg.DESCRIPTOR.fields)


class TestDotifyWidevineRuntime(unittest.TestCase):
    def test_modern_protobuf_and_quality_priority(self):
        import google.protobuf as protobuf
        from dotify.interface.enums import AudioQuality

        self.assertTrue(protobuf.__version__.startswith("6.33."))
        self.assertEqual(AudioQuality.FLAC_MP4_24.value, "flac-mp4-24")
        self.assertEqual(AudioQuality.AAC_HIGH.actual_file_format, "m4a")
        self.assertEqual(AudioQuality.VORBIS_HIGH.actual_file_format, "ogg")

    def test_ffmpeg_remux_is_stream_copy(self):
        import inspect
        from dotify.downloader.audio import SpotifyAudioDownloader

        remux = inspect.getsource(SpotifyAudioDownloader._ffmpeg_remux)
        self.assertIn('"copy"', remux)
        self.assertNotIn('"-c:a"', remux)


class TestRequirementsPinsAreConsistent(unittest.TestCase):
    """requirements.txt must not list a package the Dockerfile installs --no-deps.

    Duplicating an engine wheel here would put its stale caps back in front of
    the resolver and make `pip install -r requirements.txt` unsatisfiable again.
    """

    NO_DEPS = ("streamrip", "gamdl", "pyplayready", "dotify-cli")

    def test_engines_absent_from_requirements(self):
        text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = line.split("==")[0].split(">=")[0].split("<")[0].strip().lower()
            with self.subTest(requirement=line):
                self.assertNotIn(
                    name,
                    [n.lower() for n in self.NO_DEPS],
                    f"{name} is installed --no-deps by the Dockerfile and must "
                    "not also be resolved from requirements.txt",
                )

    def test_modern_protobuf_is_pinned_in_requirements(self):
        text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
        self.assertIn("protobuf==6.33.6", text)
        self.assertNotIn("protobuf==3.20.1", text)


if __name__ == "__main__":
    unittest.main()
