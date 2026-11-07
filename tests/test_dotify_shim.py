import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")

from dl_utils import provider_engines as pe  # noqa: E402
from dl_utils import dotify_shim as shim  # noqa: E402
from dl_utils import platform_config as pcfg  # noqa: E402


def public_album(album_id):
    return {
        "id": album_id,
        "title": "Public Album",
        "subtitle": "Public Artist",
        "releaseDate": "2020-01-02",
        "visualIdentity": {"image": [{"url": "https://img/cover", "maxWidth": 640}]},
        "trackList": [
            {"uri": "spotify:track:firsttrack", "title": "One", "subtitle": "Public Artist", "duration": 1000},
            {"uri": "spotify:track:secondtrack", "title": "Two", "subtitle": "Public Artist", "duration": 2000},
        ],
    }


class TestDotifyMetadataShim(unittest.TestCase):
    def setUp(self):
        shim.album_metadata.cache_clear()
        shim._TRACK_CONTEXT.clear()

    def test_album_playability_and_track_numbers_reach_issue_summary(self):
        entity = public_album("album-id")
        entity["trackList"][1]["isPlayable"] = False
        with mock.patch.object(shim, "embed_entity", return_value=entity):
            album = shim.album_metadata("album-id")
        expected = shim.expected_tracks(album)
        self.assertEqual([track["TRACK_NUMBER"] for track in expected], ["1", "2"])
        self.assertTrue(expected[0]["is_playable"])
        self.assertFalse(expected[1]["is_playable"])
        self.assertEqual(expected[0]["SNG_ID"], "firsttrack")
        self.assertEqual(album["release_date"], "2020-01-02")

    def test_standalone_track_uses_public_album_context_and_embed_cover(self):
        entity = {
            "title": "Single", "subtitle": "Singer", "duration": 1234,
            "releaseDate": {"isoString": "2024-03-04T00:00:00Z"},
            "relatedEntityUri": "spotify:artist:artist-id",
            "visualIdentity": {"image": [{"url": "https://img/single", "maxWidth": 640}]},
        }
        with mock.patch.object(shim, "embed_entity", return_value=entity), mock.patch.object(
            shim, "_public_track_context",
            return_value={"name": "Single Album", "release_date": "2024-03-04"},
        ):
            track = shim.track_metadata("single-id")
        self.assertEqual(track["album"]["name"], "Single Album")
        self.assertEqual(track["album"]["release_date"], "2024-03-04")
        self.assertEqual(track["album"]["images"][0]["width"], 640)

    def test_public_track_page_parser_reads_album_and_release_date(self):
        body = (
            '<meta property="og:description" content="Singer &middot; Album &middot; Song &middot; 2024">'
            '<meta name="music:release_date" content="2024-03-04">'
        )
        with mock.patch.object(
            shim.requests, "get",
            return_value=types.SimpleNamespace(text=body, raise_for_status=lambda: None),
        ) as request:
            context = shim._public_track_context("single-id")
        self.assertEqual(context, {"name": "Album", "release_date": "2024-03-04"})
        self.assertEqual(request.call_args.kwargs["timeout"], 20)

    def test_embed_parser_reads_real_next_data_shape(self):
        entity = public_album("album-id")
        page = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
        body = '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(page) + "</script></html>"
        with mock.patch.object(shim.requests, "get", return_value=types.SimpleNamespace(text=body, raise_for_status=lambda: None)):
            self.assertEqual(shim.embed_entity("album", "album-id")["title"], "Public Album")


class TestDotifyCredentialAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved_env = os.environ.copy()
        import utils
        self.utils = utils
        self.old_token_path = utils.TOKEN_ENV_PATH
        utils.TOKEN_ENV_PATH = self.root / "absent.env"

    def tearDown(self):
        self.utils.TOKEN_ENV_PATH = self.old_token_path
        os.environ.clear()
        os.environ.update(self.saved_env)
        self.tmp.cleanup()

    def test_cookie_copy_is_private(self):
        source = self.root / "cookies.txt"
        source.write_text(
            "# Netscape HTTP Cookie File\n"
            ".spotify.com\tTRUE\t/\tTRUE\t0\tsp_dc\tweb-session\n"
        )
        os.environ["SPOTIFY_COOKIES_PATH"] = str(source)
        staged = shim.stage_cookies(self.root)
        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
        self.assertEqual(staged.read_bytes(), source.read_bytes())

    def test_json_sp_dc_adapter_is_private(self):
        source = self.root / "credentials.json"
        source.write_text(json.dumps({"sp_dc": "web-session"}))
        os.environ["SPOTIFY_COOKIES_PATH"] = ""
        os.environ["SPOTIFY_CREDENTIALS_PATH"] = str(source)
        staged = shim.stage_cookies(self.root)
        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
        self.assertEqual(pcfg.spotify_cookie_values(staged)["sp_dc"], "web-session")

    def test_legacy_ap_json_is_rejected(self):
        source = self.root / "credentials.json"
        source.write_text(json.dumps({"username": "u", "auth_type": 1, "auth_data": "legacy"}))
        os.environ["SPOTIFY_COOKIES_PATH"] = ""
        os.environ["SPOTIFY_CREDENTIALS_PATH"] = str(source)
        with self.assertRaisesRegex(ValueError, "SPOTIFY_COOKIES_PATH"):
            shim.stage_cookies(self.root)


class TestLiveSubprocessProgress(unittest.TestCase):
    def test_engine_subprocess_never_reads_interactive_stdin(self):
        captured = {}

        class Process:
            returncode = 0

            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stdout.feed_eof()

            async def wait(self):
                return 0

        async def spawn(*args, **kwargs):
            captured.update(kwargs)
            return Process()

        with mock.patch.object(pe.asyncio, "create_subprocess_exec", spawn):
            rc, _ = asyncio.run(pe._run_subprocess(["engine"], Path.cwd()))
        self.assertEqual(rc, 0)
        self.assertIs(captured["stdin"], pe.asyncio.subprocess.DEVNULL)

    def test_stdout_is_reported_before_process_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            updates = []

            async def progress(text):
                updates.append(text)

            rc, tail = asyncio.run(pe._run_subprocess(
                ["sh", "-c", "echo 'Downloading: Artist - Song'; sleep 0.1"],
                Path(tmp), progress_cb=progress,
            ))
            self.assertEqual(rc, 0)
            self.assertIn("Artist - Song", tail)
            self.assertTrue(any("Artist - Song" in text for text in updates))


if __name__ == "__main__":
    unittest.main()
