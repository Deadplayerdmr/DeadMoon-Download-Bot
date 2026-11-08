"""Offline checks for direct-engine platform configuration and credentials."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")

import utils  # noqa: E402
from dl_utils import platform_config as pcfg  # noqa: E402
from utils import get_bool_env, get_float_env, get_int_env  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigIsolationMixin:
    def setUp(self):
        super().setUp()
        self._saved_token_path = utils.TOKEN_ENV_PATH
        self._saved_environment = os.environ.copy()
        self._temp = tempfile.TemporaryDirectory(prefix="deadmoon-test-env-")
        isolated_token = Path(self._temp.name) / "token.env"
        utils.TOKEN_ENV_PATH = isolated_token
        from unittest import mock

        self._token_reader = mock.patch.object(
            utils, "read_token_env_value", side_effect=lambda key: next(
                (
                    line.split("=", 1)[1].strip()
                    for line in isolated_token.read_text().splitlines()
                    if line.startswith(f"{key}=")
                ),
                "",
            ) if isolated_token.exists() else ""
        )
        self._token_reader.start()
        self._config_reader = mock.patch.object(
            pcfg, "get_env_or_token_file", utils.get_env_or_token_file
        )
        self._config_reader.start()
        for key in tuple(os.environ):
            if key in ("MAX_ARTIST_RELEASES", "COPY_FILES_PATH", "FILE_LINK_TEMPLATE", "FORMAT") or key.endswith(("_ENABLED", "_MODE")) or key.startswith(
                ("DEEZER_", "QOBUZ_", "TIDAL_", "AMAZON_", "SPOTIFY_", "APPLE_")
            ):
                os.environ.pop(key, None)
        os.environ["TELEGRAM_TOKEN"] = "123456:TEST"

    def tearDown(self):
        self._config_reader.stop()
        self._token_reader.stop()
        utils.TOKEN_ENV_PATH = self._saved_token_path
        os.environ.clear()
        os.environ.update(self._saved_environment)
        self._temp.cleanup()
        super().tearDown()


class TestEnvKeys(unittest.TestCase):
    @staticmethod
    def keys(path):
        return {
            line.split("=", 1)[0]
            for line in path.read_text().splitlines()
            if line and not line.startswith("#") and "=" in line
        }

    def test_platform_enable_keys_are_in_template(self):
        keys = self.keys(REPO_ROOT / "token.env.example")
        for platform in pcfg.PLATFORMS:
            self.assertIn(pcfg._env_key(platform, "ENABLED"), keys)
        self.assertIn("DEEZER_ARTIST_DOWNLOAD_ENABLED", keys)
        self.assertIn("FALLBACK_ENABLED", keys)
        self.assertIn("APPLE_CODEC_PRIORITY", keys)

    def test_template_has_only_direct_engine_controls(self):
        keys = self.keys(REPO_ROOT / "token.env.example")
        self.assertFalse(any(key.endswith("_MODE") for key in keys))
        self.assertFalse(any(key.startswith("MOR" + "NYE_") for key in keys))

    def test_live_and_example_have_same_key_sets(self):
        live = REPO_ROOT / "token.env"
        if not live.exists():
            self.skipTest("no local token.env")
        self.assertEqual(self.keys(live), self.keys(REPO_ROOT / "token.env.example"))


class TestDefaults(ConfigIsolationMixin, unittest.TestCase):
    def test_every_platform_is_enabled_by_default(self):
        for platform in pcfg.PLATFORMS:
            self.assertTrue(pcfg.platform_enabled(platform), platform)

    def test_artist_downloads_default_off_and_are_opt_in_for_every_platform(self):
        for platform in pcfg.PLATFORMS:
            with self.subTest(platform=platform, enabled=False):
                self.assertFalse(pcfg.artist_download_enabled(platform))
            os.environ[pcfg._env_key(platform, "ARTIST_DOWNLOAD_ENABLED")] = "1"
            with self.subTest(platform=platform, enabled=True):
                self.assertTrue(pcfg.artist_download_enabled(platform))

    def test_fallback_is_opt_in(self):
        self.assertFalse(pcfg.fallback_enabled())
        for value in ("0", "false", "off", "garbage"):
            os.environ["FALLBACK_ENABLED"] = value
            self.assertFalse(pcfg.fallback_enabled(), value)
        os.environ["FALLBACK_ENABLED"] = "1"
        self.assertTrue(pcfg.fallback_enabled())

    def test_platform_status_shape_and_engines(self):
        expected = {
            pcfg.DEEZER: "deezer",
            pcfg.QOBUZ: "streamrip",
            pcfg.TIDAL: "streamrip",
            pcfg.AMAZON: "amazon-cli",
            pcfg.SPOTIFY: "dotify-cli",
            pcfg.APPLE: "gamdl",
            pcfg.YOUTUBE: "yt-dlp",
            pcfg.SOUNDCLOUD: "yt-dlp",
        }
        for platform, engine in expected.items():
            with self.subTest(platform=platform):
                state = pcfg.platform_status(platform)
                self.assertEqual(
                    set(state),
                    {"platform", "label", "enabled", "has_credentials", "engine", "artist_download"},
                )
                self.assertEqual(state["engine"], engine)
                self.assertFalse(state["artist_download"])

    def test_disabled_platform_still_reports_its_engine(self):
        os.environ["TIDAL_ENABLED"] = "0"
        state = pcfg.platform_status(pcfg.TIDAL)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["engine"], "streamrip")

    def test_missing_credential_errors_name_key_and_fallback_flag(self):
        for platform, key in (
            (pcfg.QOBUZ, "QOBUZ_USER_AUTH_TOKEN"),
            (pcfg.TIDAL, "TIDAL_OAUTH_TOKEN"),
            (pcfg.AMAZON, "AMAZON_WVD_PATH"),
            (pcfg.SPOTIFY, "SPOTIFY_COOKIES_PATH"),
            (pcfg.APPLE, "APPLE_COOKIES_PATH"),
        ):
            with self.subTest(platform=platform):
                message = pcfg.missing_credentials_message(platform)
                self.assertIn(key, message)
                self.assertIn("token.env", message)
                if platform == pcfg.SPOTIFY:
                    self.assertIn("direct-only", message)
                else:
                    self.assertIn("FALLBACK_ENABLED=0", message)
                self.assertIn(pcfg.DIRECT_ENGINES[platform], message)

    def test_legacy_spotify_ap_file_gets_specific_direct_only_error(self):
        path = Path(self._temp.name) / "spotify_credentials.json"
        path.write_text(json.dumps({
            "username": "listener", "auth_type": 1, "auth_data": "legacy-ap-session",
        }))
        os.environ["SPOTIFY_CREDENTIALS_PATH"] = str(path)
        message = pcfg.missing_credentials_message(pcfg.SPOTIFY)
        self.assertIn(str(path), message)
        self.assertIn("SPOTIFY_COOKIES_PATH", message)
        self.assertIn("SPOTIFY_WVD_PATH", message)
        self.assertIn("direct-only", message)
        self.assertNotIn("Deezer ARL", message)

    def test_spotify_web_bundle_still_requires_a_wvd(self):
        path = Path(self._temp.name) / "spotify_credentials.json"
        path.write_text(json.dumps({"sp_dc": "web-session"}))
        os.environ["SPOTIFY_CREDENTIALS_PATH"] = str(path)
        self.assertFalse(pcfg.has_spotify_credentials())
        message = pcfg.missing_credentials_message(pcfg.SPOTIFY)
        self.assertIn("dotify-cli", message)
        self.assertIn("provisioned Widevine", message)

    def test_platform_messages(self):
        self.assertIn("Qobuz", pcfg.artist_disabled_message(pcfg.QOBUZ))
        self.assertIn("QOBUZ_ARTIST_DOWNLOAD_ENABLED", pcfg.artist_disabled_message(pcfg.QOBUZ))
        self.assertIn("Amazon Music", pcfg.platform_disabled_message(pcfg.AMAZON))


class TestBoolAndNumberParsing(unittest.TestCase):
    def test_bool_parsing(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            os.environ["TEST_BOOL"] = value
            self.assertTrue(get_bool_env("TEST_BOOL", False))
        for value in ("0", "false", "off", "junk", ""):
            os.environ["TEST_BOOL"] = value
            self.assertFalse(get_bool_env("TEST_BOOL", False))

    def test_numeric_parsing(self):
        os.environ["TEST_INT"] = "junk"
        self.assertEqual(get_int_env("TEST_INT", 4), 4)
        os.environ["TEST_FLOAT"] = "1.25"
        self.assertEqual(get_float_env("TEST_FLOAT", 0), 1.25)
        os.environ.pop("TEST_INT", None)
        self.assertEqual(get_int_env("TEST_INT", 4), 4)


class TestCredentialPresence(ConfigIsolationMixin, unittest.TestCase):
    def test_deezer_requires_a_full_length_arl(self):
        os.environ["DEEZER_TOKEN"] = "tooshort"
        self.assertFalse(pcfg.has_deezer_credentials())
        os.environ["DEEZER_TOKEN"] = "a" * 50
        self.assertTrue(pcfg.has_deezer_credentials())

    def test_qobuz_accepts_complete_credential_pairs(self):
        for pair in (
            ("QOBUZ_USER_AUTH_TOKEN", "QOBUZ_APP_ID"),
            ("QOBUZ_USER_ID", "QOBUZ_SECRETS"),
            ("QOBUZ_EMAIL", "QOBUZ_PASSWORD"),
            ("QOBUZ_EMAIL_OR_USER_ID", "QOBUZ_PASSWORD_OR_TOKEN"),
        ):
            with self.subTest(pair=pair):
                for key in pair:
                    os.environ[key] = "x"
                self.assertTrue(pcfg.has_qobuz_credentials())
                for key in pair:
                    del os.environ[key]

    def test_qobuz_rejects_a_half_pair(self):
        os.environ["QOBUZ_APP_ID"] = "123"
        self.assertFalse(pcfg.has_qobuz_credentials())

    def test_tidal_requires_refreshable_credentials(self):
        os.environ["TIDAL_ACCESS_TOKEN"] = "a"
        os.environ["TIDAL_REFRESH_TOKEN"] = "b"
        self.assertFalse(pcfg.has_tidal_credentials())
        os.environ["TIDAL_USER_ID"] = "c"
        self.assertTrue(pcfg.has_tidal_credentials())

    def test_tidal_json_shapes_and_individual_overrides(self):
        for blob in (
            {"user_id": "1", "access_token": "a", "refresh_token": "b", "expiryTimestamp": 123},
            {"tokens": {"userId": "1", "accessToken": "a", "refreshToken": "b"}},
            {"oauth": {"user_id": "1", "access_token": "a", "refresh_token": "b"}},
        ):
            with self.subTest(blob=blob):
                os.environ["TIDAL_OAUTH_TOKEN"] = json.dumps(blob)
                self.assertTrue(pcfg.has_tidal_credentials())
        os.environ["TIDAL_ACCESS_TOKEN"] = "fresh"
        self.assertEqual(pcfg.tidal_oauth_fields()["access_token"], "fresh")

    def test_invalid_tidal_blobs_do_not_count(self):
        for blob in ("whole-blob", "{not json", '["a"]', "42", "{}"):
            os.environ["TIDAL_OAUTH_TOKEN"] = blob
            self.assertFalse(pcfg.has_tidal_credentials(), blob)

    def setUp(self):
        super().setUp()
        self.cookies = Path(self._temp.name) / "spotify_cookies.txt"
        self.wvd = Path(self._temp.name) / "device.wvd"
        os.environ["SPOTIFY_COOKIES_PATH"] = str(self.cookies)
        os.environ["SPOTIFY_WVD_PATH"] = str(self.wvd)

    def write_web_session(self):
        self.cookies.write_text(
            "# Netscape HTTP Cookie File\n"
            ".spotify.com\tTRUE\t/\tTRUE\t0\tsp_dc\tweb-session\n"
        )
        self.wvd.write_bytes(b"provisioned-test-device")

    def test_spotify_requires_web_cookie_and_real_wvd(self):
        self.assertFalse(pcfg.has_spotify_credentials())
        self.cookies.write_bytes(b"")
        self.wvd.write_bytes(b"provisioned-test-device")
        self.assertFalse(pcfg.has_spotify_credentials())
        self.write_web_session()
        self.assertTrue(pcfg.has_spotify_credentials())
        self.wvd.write_bytes(b"")
        self.assertFalse(pcfg.has_spotify_credentials())

    def test_spotify_rejects_oauth_and_legacy_ap_credentials(self):
        os.environ["SPOTIFY_OAUTH_TOKEN"] = "bearer-token"
        self.assertFalse(pcfg.has_spotify_credentials())
        legacy = Path(self._temp.name) / "spotify_credentials.json"
        os.environ["SPOTIFY_CREDENTIALS_PATH"] = str(legacy)
        legacy.write_text(json.dumps({
            "username": "listener", "auth_type": 1, "auth_data": "stored-ap-credentials",
        }))
        self.assertFalse(pcfg.has_spotify_credentials())
        self.write_web_session()
        self.assertTrue(pcfg.has_spotify_credentials())

    def test_legacy_path_json_web_bundle_is_supported(self):
        legacy = Path(self._temp.name) / "spotify_credentials.json"
        os.environ["SPOTIFY_COOKIES_PATH"] = ""
        os.environ["SPOTIFY_CREDENTIALS_PATH"] = str(legacy)
        for document in ({"sp_dc": "web-session"}, {"cookies": {"sp_dc": "web-session"}}):
            with self.subTest(document=document):
                legacy.write_text(json.dumps(document))
                self.wvd.write_bytes(b"provisioned-test-device")
                self.assertTrue(pcfg.has_spotify_credentials())

    def test_invalid_cookie_domains_and_examples_do_not_count(self):
        self.cookies.write_text(".example.com\tTRUE\t/\tTRUE\t0\tsp_dc\twrong-domain\n")
        self.wvd.write_bytes(b"provisioned-test-device")
        self.assertFalse(pcfg.has_spotify_credentials())
        example = REPO_ROOT / "local_resources" / "spotify_cookies.txt.example"
        os.environ["SPOTIFY_COOKIES_PATH"] = str(example)
        self.assertFalse(pcfg.has_spotify_credentials())
        copy = Path(self._temp.name) / "copied-cookies.txt"
        copy.write_bytes(example.read_bytes())
        os.environ["SPOTIFY_COOKIES_PATH"] = str(copy)
        self.assertFalse(pcfg.has_spotify_credentials())

    def test_all_credential_examples_exist_and_unchanged_copies_are_unconfigured(self):
        expected = {
            "SPOTIFY_COOKIES_PATH": "spotify_cookies.txt",
            "SPOTIFY_CREDENTIALS_PATH": "spotify_credentials.json",
            "SPOTIFY_WVD_PATH": "device.wvd",
            "APPLE_COOKIES_PATH": "apple_cookies.txt",
            "AMAZON_WVD_PATH": "device.wvd",
            "COOKIES_PATH": "cookies.txt",
            "QOBUZ_CONFIG_PATH": "qobuz_config.toml",
            "TIDAL_CONFIG_PATH": "tidal_config.toml",
        }
        for key, filename in expected.items():
            with self.subTest(filename=filename):
                example = REPO_ROOT / "local_resources" / f"{filename}.example"
                self.assertTrue(example.is_file())
                self.assertGreater(example.stat().st_size, 0)
                copy = Path(self._temp.name) / filename
                copy.write_bytes(example.read_bytes())
                os.environ[key] = str(copy)
                self.assertFalse(pcfg._credential_file_ready(key))

    def test_apple_file_is_checked_but_media_token_can_replace_it(self):
        path = Path(self._temp.name) / "apple_cookies.txt"
        os.environ["APPLE_COOKIES_PATH"] = str(path)
        self.assertFalse(pcfg.has_apple_credentials())
        path.write_text(".apple.com\tTRUE\t/\tTRUE\t0\tmedia-user-token\treal\n")
        self.assertTrue(pcfg.has_apple_credentials())
        example = REPO_ROOT / "local_resources" / "apple_cookies.txt.example"
        os.environ["APPLE_COOKIES_PATH"] = str(example)
        self.assertFalse(pcfg.has_apple_credentials())
        os.environ["APPLE_MEDIA_USER_TOKEN"] = "real-token"
        self.assertTrue(pcfg.has_apple_credentials())

    def test_amazon_needs_token_and_real_wvd_file(self):
        path = Path(self._temp.name) / "device.wvd"
        os.environ["AMAZON_DEVICE_TOKEN"] = "device-token"
        os.environ["AMAZON_WVD_PATH"] = str(path)
        self.assertFalse(pcfg.has_amazon_credentials())
        path.write_bytes(b"provisioned-device")
        self.assertTrue(pcfg.has_amazon_credentials())
        os.environ["AMAZON_DEVICE_TOKEN"] = " "
        self.assertFalse(pcfg.has_amazon_credentials())

    def test_youtube_and_soundcloud_need_no_account(self):
        self.assertTrue(pcfg.has_credentials(pcfg.YOUTUBE))
        self.assertTrue(pcfg.has_credentials(pcfg.SOUNDCLOUD))


class TestTunables(ConfigIsolationMixin, unittest.TestCase):
    def test_defaults_and_parsing(self):
        self.assertEqual(pcfg.max_artist_releases(), 100)
        os.environ["MAX_ARTIST_RELEASES"] = "0"
        self.assertEqual(pcfg.max_artist_releases(), 100)
        self.assertTrue(pcfg.auto_delete_enabled())
        self.assertEqual(pcfg.auto_delete_seconds(), 1800)
        self.assertEqual(pcfg.copy_files_path(), "")
        self.assertEqual(pcfg.output_format(), "")
        os.environ["FORMAT"] = "ZIP"
        self.assertEqual(pcfg.output_format(), "zip")
        self.assertTrue(pcfg.flac_enabled())
        os.environ["ENABLE_FLAC"] = "0"
        self.assertFalse(pcfg.flac_enabled())

    def test_templates_ship_flac_and_fallback_defaults(self):
        for path in (REPO_ROOT / "token.env.example", REPO_ROOT / "token.env"):
            if not path.exists():
                continue
            values = dict(
                line.split("=", 1)
                for line in path.read_text().splitlines()
                if line and not line.startswith("#") and "=" in line
            )
            self.assertEqual(values["ENABLE_FLAC"], "1")
            self.assertEqual(values["FALLBACK_ENABLED"], "0")


if __name__ == "__main__":
    unittest.main()
