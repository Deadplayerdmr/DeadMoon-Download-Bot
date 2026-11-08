"""Admin-only credential commands with no real provider network calls."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import utils
from dl_utils import credential_migrator as cm


class AdminCommandTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")
        import main

        self.main = main
        self.temp = tempfile.TemporaryDirectory(prefix="deadmoon-auth-")
        self.root = Path(self.temp.name)
        self.old_token = utils.TOKEN_ENV_PATH
        self.old_main_token = main.TOKEN_ENV_PATH
        self.old_env = os.environ.copy()
        utils.TOKEN_ENV_PATH = self.root / "token.env"
        main.TOKEN_ENV_PATH = utils.TOKEN_ENV_PATH
        utils.TOKEN_ENV_PATH.write_text("# private\n")
        self.message = mock.AsyncMock()
        self.message.reply = mock.AsyncMock(return_value=mock.AsyncMock())

    def tearDown(self):
        for task in list(self.main._TIDAL_AUTH_TASKS.values()):
            task.cancel()
        self.main._TIDAL_AUTH_TASKS.clear()
        utils.TOKEN_ENV_PATH = self.old_token
        self.main.TOKEN_ENV_PATH = self.old_main_token
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def test_deezer_saves_and_refreshes(self):
        arl = "a" * 60
        with mock.patch.object(self.main, "refresh_deezer_session", new=mock.AsyncMock()):
            self.message.text = "/authenticatedeezer " + arl
            asyncio.run(self.main.authenticate_deezer(self.message))
        self.assertEqual(os.environ["DEEZER_TOKEN"], arl)
        self.assertEqual(os.environ["DEEZER_ENABLED"], "1")
        self.assertTrue(utils.TOKEN_ENV_PATH.read_text().startswith("# private\n"))
        self.assertEqual(utils.TOKEN_ENV_PATH.stat().st_mode & 0o777, 0o644)
        fresh = self.root / "fresh.env"
        with mock.patch.object(self.main, "TOKEN_ENV_PATH", fresh), mock.patch.object(utils, "TOKEN_ENV_PATH", fresh):
            self.assertTrue(self.main.update_token_env_keys({"DEEZER_TOKEN": arl}))
            self.assertEqual(fresh.stat().st_mode & 0o777, 0o600)

    def test_tidal_device_flow_saves_fields_and_cancel_clears_task(self):
        device = {"device_code": "device", "user_code": "CODE", "expires_in": 60, "interval": 1}
        fields = {"user_id": "1", "access_token": "access", "refresh_token": "refresh", "country_code": "US"}
        destination = self.root / "tidal.toml"
        with mock.patch.object(self.main.admin_auth, "request_tidal_device_code", new=mock.AsyncMock(return_value=device)):
            with mock.patch.object(self.main.admin_auth, "poll_tidal_device_token", new=mock.AsyncMock(return_value=fields)):
                with mock.patch.object(cm, "write_tidal_credentials", return_value=destination) as write:
                    asyncio.run(self.main.authenticate_tidal(self.message, SimpleNamespace(args=None)))
        self.assertIn("link.tidal.com/CODE", self.message.reply.await_args.args[0])
        write.assert_called_once()
        self.assertEqual(os.environ["TIDAL_ENABLED"], "1")

        sleeper = asyncio.run(self._pending_auth())
        self.main._TIDAL_AUTH_TASKS[self.main.ADMIN_ID] = sleeper
        asyncio.run(self.main.cancel_auth(self.message))
        self.assertTrue(sleeper.cancelled())
        self.assertNotIn(self.main.ADMIN_ID, self.main._TIDAL_AUTH_TASKS)

    @staticmethod
    async def _pending_auth():
        return asyncio.create_task(asyncio.sleep(60))

    def test_tidal_paste_mode_warns_about_web_player(self):
        token = pcfg_jwt(uid="1", exp=2000000000, cid=3235)
        raw = f"access_token={token};refresh_token=refresh"
        with mock.patch.object(cm, "write_tidal_credentials", return_value=self.root / "tidal.toml"):
            asyncio.run(self.main.authenticate_tidal(self.message, SimpleNamespace(args=raw)))
        self.assertIn("cid=3235", self.message.reply.await_args.args[0])

    def test_qobuz_hashes_password_and_apple_writes_three_cookie_lines(self):
        with mock.patch.object(self.main.admin_auth, "discover_qobuz_app_credentials_sync", return_value=("app", ["secret"])):
            with mock.patch.object(cm, "write_qobuz_credentials", return_value=self.root / "qobuz.toml") as write:
                asyncio.run(self.main.authenticate_qobuz(self.message, SimpleNamespace(args="user@example.test password")))
        self.assertEqual(write.call_args.args[0]["password_or_token"], "5f4dcc3b5aa765d61d8327deb882cf99")
        self.assertFalse(write.call_args.args[0]["use_auth_token"])
        self.assertEqual(os.environ["QOBUZ_ENABLED"], "1")
        notice = mock.AsyncMock()
        notice.delete = mock.AsyncMock()
        self.message.reply.return_value = notice

        cookies = self.root / "apple.txt"
        with mock.patch.object(cm, "configured_resource_path", return_value=cookies):
            asyncio.run(self.main.authenticate_apple(self.message, SimpleNamespace(args="media-token")))
        self.assertEqual(cookies.read_text().count("media-user-token"), 3)
        self.assertEqual(cookies.stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.environ["APPLE_ENABLED"], "1")

    def test_soundcloud_saves_token(self):
        asyncio.run(self.main.authenticate_soundcloud(self.message, SimpleNamespace(args="oauth")))
        self.assertEqual(os.environ["SOUNDCLOUD_OAUTH_TOKEN"], "oauth")
        self.assertEqual(os.environ["SOUNDCLOUD_ENABLED"], "1")


def pcfg_jwt(**claims):
    import base64
    import json
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


if __name__ == "__main__":
    unittest.main()
