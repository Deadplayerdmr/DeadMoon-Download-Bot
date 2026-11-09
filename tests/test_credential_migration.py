"""Offline credential offloading and path resolution tests."""

import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import utils
from dl_utils import credential_migrator as cm
from dl_utils import platform_config as pcfg


class CredentialIsolation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deadmoon-migration-")
        self.root = Path(self.temp.name)
        self.resources = self.root / "local_resources"
        self.resources.mkdir()
        self.token = self.root / "token.env"
        self.old_path = utils.TOKEN_ENV_PATH
        self.old_resources = cm.LOCAL_RESOURCES
        self.old_env = os.environ.copy()
        utils.TOKEN_ENV_PATH = self.token
        cm.LOCAL_RESOURCES = self.resources
        for key in tuple(os.environ):
            if key.startswith(("TIDAL_", "QOBUZ_", "APPLE_")):
                os.environ.pop(key, None)

    def tearDown(self):
        utils.TOKEN_ENV_PATH = self.old_path
        cm.LOCAL_RESOURCES = self.old_resources
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()


class TestMigration(CredentialIsolation):
    def test_inline_values_are_offloaded_atomically_and_read_back(self):
        self.token.write_text(
            "# retained comment\nTIDAL_ACCESS_TOKEN=access\nTIDAL_REFRESH_TOKEN=refresh\n"
            "TIDAL_USER_ID=12\nQOBUZ_EMAIL=user@example.test\nQOBUZ_PASSWORD=secret\n"
            "QOBUZ_APP_ID=app\nQOBUZ_SECRETS=one,two\nAPPLE_MEDIA_USER_TOKEN=apple-token\n"
        )
        migrated = cm.migrate_inline_credentials_to_local_resources()
        self.assertEqual(set(migrated), {"tidal", "qobuz", "apple"})
        for path in migrated.values():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for path_key, name in (
            ("TIDAL_CONFIG_PATH", "tidal_config.toml"),
            ("QOBUZ_CONFIG_PATH", "qobuz_config.toml"),
            ("APPLE_COOKIES_PATH", "apple_cookies.txt"),
        ):
            self.assertEqual(os.environ[path_key], f"/usr/src/app/local_resources/{name}")
        self.assertTrue(self.token.read_text().startswith("# retained comment\n"))
        self.assertNotIn("TIDAL_ACCESS_TOKEN=access", self.token.read_text())
        self.assertNotIn("QOBUZ_PASSWORD=secret", self.token.read_text())
        self.assertEqual(pcfg.tidal_oauth_fields()["access_token"], "access")
        self.assertEqual(pcfg.tidal_oauth_fields()["token_type"], "Bearer")
        self.assertTrue(pcfg.has_tidal_credentials())
        self.assertEqual(pcfg.qobuz_fields()["app_id"], "app")
        self.assertEqual(pcfg.qobuz_fields()["secrets"], ["one", "two"])
        self.assertTrue(pcfg.has_qobuz_credentials())
        self.assertTrue(pcfg.has_apple_credentials())
        self.assertEqual((self.resources / "apple_cookies.txt").read_text().count("media-user-token"), 3)
        # Idempotent: no secret content is resurrected or changed on the next run.
        before = {path.name: path.read_bytes() for path in migrated.values()}
        self.assertEqual(cm.migrate_inline_credentials_to_local_resources(), {})
        self.assertEqual(before, {path.name: path.read_bytes() for path in migrated.values()})

    def test_migration_succeeds_without_tomlkit_on_host_python(self):
        self.token.write_text(
            "TIDAL_OAUTH_TOKEN={\"user_id\": \"12\", \"access_token\": \"access\", "
            "\"refresh_token\": \"refresh;with spaces\"}\n"
            "QOBUZ_EMAIL=user@example.test\nQOBUZ_PASSWORD=p@ss; word\n"
            "QOBUZ_APP_ID=app\nQOBUZ_SECRETS=one,two\n"
            "APPLE_MEDIA_USER_TOKEN=apple-token\n"
        )
        with mock.patch.dict(sys.modules, {"tomlkit": None}):
            migrated = cm.migrate_inline_credentials_to_local_resources()
            self.assertEqual(set(migrated), {"tidal", "qobuz", "apple"})
            self.assertEqual(pcfg.tidal_oauth_fields()["refresh_token"], "refresh;with spaces")
            self.assertEqual(pcfg.qobuz_fields()["password_or_token"], "p@ss; word")
        for name, section in (("tidal_config.toml", "tidal"), ("qobuz_config.toml", "qobuz")):
            with (self.resources / name).open("rb") as handle:
                self.assertIn(section, tomllib.load(handle))
        for path in migrated.values():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("refresh;with spaces", self.token.read_text())
        self.assertNotIn("p@ss; word", self.token.read_text())

    def test_toml_credentials_override_stale_inline_values(self):
        tidal = self.root / "custom-tidal.toml"
        cm.write_tidal_credentials({"user_id": "1", "access_token": "file", "refresh_token": "r"}, tidal)
        os.environ["TIDAL_CONFIG_PATH"] = str(tidal)
        os.environ["TIDAL_ACCESS_TOKEN"] = "environment"
        os.environ["TIDAL_OAUTH_TOKEN"] = '{"access_token": "stale-json", "refresh_token": "stale-refresh"}'
        self.assertEqual(pcfg.tidal_oauth_fields()["access_token"], "file")
        self.assertEqual(pcfg.tidal_oauth_fields()["refresh_token"], "r")
        os.environ.pop("TIDAL_OAUTH_TOKEN")
        self.assertEqual(pcfg._credential_file_path("TIDAL_CONFIG_PATH"), tidal)
        os.environ.pop("TIDAL_ACCESS_TOKEN")
        self.assertEqual(pcfg.tidal_oauth_fields()["access_token"], "file")

    def test_container_resource_path_falls_back_to_host_checkout(self):
        configured = Path("/usr/src/app/local_resources") / "custom.toml"
        local = self.resources / "custom.toml"
        local.write_text("[tidal]\nuser_id = '1'\n")
        self.assertEqual(cm.resolve_resource_path(configured), local)


if __name__ == "__main__":
    unittest.main()
