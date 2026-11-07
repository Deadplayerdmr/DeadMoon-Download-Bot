"""Regression checks for the six production runtime failures.

External HTTP and audio-byte boundaries are replaced, but the real formatting,
resolution, and dispatcher functions are exercised.
"""

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["TELEGRAM_TOKEN"] = "123456:TEST"
os.environ.pop("DEEZER_TOKEN", None)

import utils  # noqa: E402
from dl_utils import metadata_bridge as mb  # noqa: E402


@contextlib.contextmanager
def isolated_main():
    """Import main with an isolated cwd/config, without starting Telegram polling."""
    from types import SimpleNamespace

    os.environ["TELEGRAM_TOKEN"] = "123456:TEST"
    previous_cwd = os.getcwd()
    previous_path = utils.TOKEN_ENV_PATH
    previous_admin = os.environ.get("ADMIN_ID")
    with tempfile.TemporaryDirectory(prefix="deadmoon-main-") as tmp:
        try:
            os.chdir(tmp)
            utils.TOKEN_ENV_PATH = Path(tmp) / "absent-token.env"
            os.environ["ADMIN_ID"] = "4242"
            sys.modules.pop("main", None)
            import main

            yield main, SimpleNamespace
        finally:
            sys.modules.pop("main", None)
            os.chdir(previous_cwd)
            utils.TOKEN_ENV_PATH = previous_path
            if previous_admin is None:
                os.environ.pop("ADMIN_ID", None)
            else:
                os.environ["ADMIN_ID"] = previous_admin

from dl_utils import platform_config as pcfg  # noqa: E402
from dl_utils import provider_engines as pe  # noqa: E402
from handlers import streaming as st  # noqa: E402
from handlers import yt_dlp as yt  # noqa: E402


class TestTelegramCommands(unittest.IsolatedAsyncioTestCase):
    async def test_help_start_uses_html_and_escapes_dynamic_text(self):
        with isolated_main() as (main, namespace):
            class Event:
                def __init__(self):
                    self.answer_kwargs = None

                async def answer(self, text, **kwargs):
                    self.answer_kwargs = (text, kwargs)

            async def get_me():
                return namespace(username="music_bot<&")

            with mock.patch.object(main.bot, "get_me", get_me), mock.patch.object(
                main, "supported_platforms", return_value=["Deezer", "A&B <Live>"]
            ):
                event = Event()
                await main.help_start(event, namespace(command="help", args=None))

        text, kwargs = event.answer_kwargs
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("@music_bot&lt;&amp;", text)
        self.assertIn("A&amp;B &lt;Live&gt;", text)
        self.assertIn("<b>Send me a link</b>", text)
        self.assertNotIn("Markdown", kwargs["parse_mode"])

    async def test_whitelist_escapes_user_and_group_names(self):
        with isolated_main() as (main, namespace):
            main.ALLOWED_IDS.update({7, -9})

            class Event:
                def __init__(self):
                    self.answers = []

                async def answer(self, text, **kwargs):
                    self.answers.append((text, kwargs))

            async def get_chat(chat_id):
                if chat_id == 7:
                    return namespace(title=None, full_name="User_* <Admin & Co>")
                if chat_id == -9:
                    return namespace(title="Group_* <Listeners & Friends>", full_name=None)
                return namespace(title=None, full_name="Owner")

            event = Event()
            with mock.patch.object(main.bot, "get_chat", get_chat):
                await main.show_whitelist(event)

        text, kwargs = event.answers[-1]
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("<b>Current Whitelist</b>", text)
        self.assertIn("User_* &lt;Admin &amp; Co&gt;", text)
        self.assertIn("Group_* &lt;Listeners &amp; Friends&gt;", text)
        self.assertNotIn("User_* <Admin & Co>", text)

    async def test_newarl_reload_error_escapes_exception_text(self):
        with isolated_main() as (main, namespace):
            class Event:
                text = "/newarl " + "x" * 50

                async def reply(self, text, **kwargs):
                    self.reply_text = text
                    self.reply_kwargs = kwargs

            event = Event()
            with mock.patch.object(main, "set_deezer_token", return_value=True), mock.patch.object(
                main, "refresh_deezer_session",
                new=mock.AsyncMock(side_effect=RuntimeError("reload_* <failed & unsafe>")),
            ):
                await main.update_arl(event)

        self.assertEqual(event.reply_kwargs["parse_mode"], "HTML")
        self.assertIn("<code>reload_* &lt;failed &amp; unsafe&gt;</code>", event.reply_text)
        self.assertNotIn("reload_* <failed & unsafe>", event.reply_text)

    async def test_status_handler_replies_with_html(self):
        with isolated_main() as (main, _):
            class Event:
                async def reply(self, text, **kwargs):
                    self.text = text
                    self.kwargs = kwargs

            event = Event()
            with mock.patch.object(main, "status_report", return_value="<b>Status</b>"):
                await main.admin_status(event)
        self.assertEqual(event.text, "<b>Status</b>")
        self.assertEqual(event.kwargs["parse_mode"], "HTML")

    async def test_status_filter_rejects_whitelisted_non_admin(self):
        from aiogram import types

        with isolated_main() as (main, namespace):
            main.ALLOWED_IDS.add(777)
            update = types.Update(
                update_id=1,
                message=types.Message(
                    message_id=1,
                    date=0,
                    chat=types.Chat(id=777, type="private"),
                    from_user=types.User(id=777, is_bot=False, first_name="User"),
                    text="/status",
                    entities=[types.MessageEntity(type="bot_command", offset=0, length=7)],
                ),
            )
            message = update.message
            called = []

            async def authorized(event, data):
                called.append(data["event_from_user"].id)
                return True

            allowed = await main.AccessControlMiddleware()(
                authorized,
                message,
                {"event_from_user": message.from_user, "event_chat": message.chat},
            )
            registered = next(
                handler for handler in main.dp.message.handlers
                if handler.callback is main.admin_status
            )
            checked = await registered.check(
                message,
                bot=main.bot,
                event_from_user=message.from_user,
                event_chat=message.chat,
                event_update=update,
            )
        self.assertTrue(allowed)  # whitelist middleware still admits the user
        self.assertEqual(called, [777])
        self.assertFalse(checked[0])  # the status filter alone enforces ADMIN_ID


class ProductionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="deadmoon-production-")
        self.root = Path(self.tmp.name)
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        self.saved_env = os.environ.copy()
        os.environ.pop("FALLBACK_ENABLED", None)
        self.saved_token_path = utils.TOKEN_ENV_PATH
        utils.TOKEN_ENV_PATH = self.root / "absent-token.env"

    def tearDown(self):
        utils.TOKEN_ENV_PATH = self.saved_token_path
        os.environ.clear()
        os.environ.update(self.saved_env)
        self.tmp.cleanup()


class TestSongLink(ProductionCase):
    def test_free_query_and_cross_platform_matches(self):
        calls = []

        async def fetch(url, *, params=None):
            calls.append((url, params))
            return {
                "entitiesByUniqueId": {
                    "DEEZER_SONG::1": {
                        "title": "Like Me",
                        "artistName": "T-Mass",
                        "thumbnailUrl": "https://img/cover.jpg",
                        "isrc": "USABC2600001",
                    }
                },
                "linksByPlatform": {
                    "deezer": {"url": "https://www.deezer.com/track/123"},
                    "spotify": {"url": "https://open.spotify.com/track/abc"},
                    "tidal": {"url": "https://tidal.com/track/456"},
                    "appleMusic": {"url": "https://music.apple.com/us/song/x/789"},
                    "itunes": {"url": "https://music.apple.com/us/album/x/789?i=789"},
                    "qobuz": {"url": "https://open.qobuz.com/track/abc"},
                },
            }

        with mock.patch.object(mb, "_fetch_json", fetch):
            result = asyncio.run(mb.odesli_links("https://tidal.com/track/456"))

        self.assertEqual(calls, [(mb._ODESLI, {"url": "https://tidal.com/track/456", "userCountry": "US"})])
        self.assertEqual(result["matched_urls"]["deezer"], "https://www.deezer.com/track/123")
        self.assertEqual(result["matched_urls"]["itunes"], "https://music.apple.com/us/album/x/789?i=789")
        self.assertEqual(result["platforms"]["apple"], "https://music.apple.com/us/song/x/789")
        self.assertEqual(result["isrc"], "USABC2600001")


class TestStatusFormatting(ProductionCase):

    def test_permanent_song_link_error_is_not_retried(self):
        class Response:
            status = 401
            history = ()
            request_info = object()

            async def text(self):
                return ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        calls = []

        class Session:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def get(self, url, params=None):
                calls.append((url, params))
                return Response()

        with mock.patch.object(mb.aiohttp, "ClientSession", Session), mock.patch.object(
            mb.asyncio, "sleep"
        ) as sleep:
            self.assertIsNone(asyncio.run(mb._fetch_text(mb._ODESLI, params={"url": "x"})))
        self.assertEqual(len(calls), 1)
        sleep.assert_not_awaited()

    def test_deezer_isrc_uses_track_endpoint(self):
        async def fetch(url, *, params=None):
            self.assertEqual(url, f"{mb._DEEZER_API}/track/isrc:USABC2600001")
            return {"id": 3809252582, "isrc": "USABC2600001"}

        with mock.patch.object(mb, "_fetch_json", fetch):
            result = asyncio.run(mb.deezer_isrc_match("USABC2600001"))
        self.assertEqual(result["id"], 3809252582)

    def test_status_escapes_dynamic_values_and_has_no_markdown(self):
        states = [
            {
                "enabled": True,
                "label": "A&B <Live>",
                "engine": "streamrip<&",
                "has_credentials": False,
                "artist_download": True,
            }
        ]
        with mock.patch.object(pcfg, "PLATFORMS", ("test",)), mock.patch.object(
            pcfg, "platform_status", return_value=states[0]
        ):
            text = st.status_report()
        self.assertIn("<b>A&amp;B &lt;Live&gt;</b>", text)
        self.assertIn("<code>streamrip&lt;&amp;</code>", text)
        self.assertIn("enabled · engine", text)
        self.assertIn("no creds", text)
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)


class TestQobuzMetadata(ProductionCase):
    def test_original_www_url_drives_page_title_lookup(self):
        url = "https://www.qobuz.com/us-en/album/like-me-t-mass-britt-lari/jge3cs7viw6jb"

        async def fetch(url, *, params=None):
            if url.startswith(mb._QOBUZ_API) or url.startswith(mb._ITUNES_SEARCH) or url.startswith(mb._DEEZER_API) or url == mb._ODESLI:
                return None
            self.assertEqual(url, "https://www.qobuz.com/us-en/album/like-me-t-mass-britt-lari/jge3cs7viw6jb")
            return '<html><meta property="og:title" content="Like Me, T-Mass - Qobuz"></html>'

        # classify retains the original URL while normalizing the engine URL.
        link = st.parse_link(url)
        with mock.patch.object(mb, "_fetch_json", side_effect=fetch), mock.patch.object(
            mb, "_fetch_text", side_effect=fetch
        ):
            info = asyncio.run(
                mb.release_info(link.platform, link.kind, link.object_id, url=link.original_url)
            )
        self.assertEqual(info.album_title, "Like Me")
        self.assertEqual(info.artist, "T-Mass")

    def test_qobuz_json_ld_supplies_release_date_and_cover(self):
        async def fetch_text(_url):
            return '''<html><meta property="og:title" content="Like Me, T-Mass - Qobuz">
            <script type="application/ld+json">{"@type":"Product","name":"Like Me",
            "releaseDate":"2020-10-20","brand":{"name":"T-Mass"},
            "image":["https://static.qobuz.com/small.jpg","https://static.qobuz.com/large.jpg"]}</script></html>'''

        with mock.patch.object(mb, "_fetch_text", fetch_text):
            info = asyncio.run(mb.page_metadata(
                "https://www.qobuz.com/us-en/album/like-me-t-mass-britt-lari/jge3cs7viw6jb",
                pcfg.QOBUZ, "album", "jge3cs7viw6jb"
            ))
        self.assertEqual(info.artist, "T-Mass")
        self.assertEqual(info.album_title, "Like Me")
        self.assertEqual(info.release_date, "2020-10-20")
        self.assertEqual(info.cover_url, "https://static.qobuz.com/large.jpg")


class TestMetadataLabelMerging(unittest.TestCase):
    def test_enrichment_preserves_engine_source_and_measured_quality(self):
        engine = pe.ReleaseMetadata(
            artist="Engine Artist",
            album_title="Engine Album",
            source_label="Apple Music (Direct · gamdl)",
            quality_label="FLAC 24-bit/96 kHz",
        )
        bridge = mb.ReleaseInfo(
            platform=pcfg.APPLE,
            kind="album",
            object_id="123",
            artist="Canonical Artist",
            album_title="Canonical Album",
        )
        merged = mb.merge_info(engine, bridge)
        self.assertEqual(merged["artist"], "Canonical Artist")
        self.assertEqual(merged["album_title"], "Canonical Album")
        self.assertEqual(merged["source_label"], "Apple Music (Direct · gamdl)")
        self.assertEqual(merged["quality_label"], "FLAC 24-bit/96 kHz")

    def test_fallback_source_and_labels_survive_without_bridge_info(self):
        engine = pe.ReleaseMetadata(
            source_label="Deezer ARL (Fallback from Tidal)",
            quality_label="MP3 320 kbps",
        )
        merged = mb.merge_info(engine, None)
        self.assertEqual(merged["source_label"], "Deezer ARL (Fallback from Tidal)")
        self.assertEqual(merged["quality_label"], "MP3 320 kbps")



class TestDeezerFallback(ProductionCase):
    def test_dispatch_uses_arl_fallback_after_direct_engine_failure(self):
        os.environ["FALLBACK_ENABLED"] = "1"

        async def direct(*args):
            raise pe.EngineError("upstream unavailable")

        async def fallback(platform, url, kind, workdir, progress_cb=None):
            path = Path(workdir) / "01 - T-Mass - Like Me.flac"
            path.write_bytes(b"fLaC\x00\x00")
            return pe._result_from_workdir(platform, "direct", "deezer-arl", url, kind, Path(workdir))

        with mock.patch.dict(pe._TOKEN_ENGINES, {pcfg.TIDAL: direct}, clear=True), mock.patch.object(
            pe, "download_via_deezer_fallback", fallback
        ), mock.patch.object(pcfg, "has_credentials", return_value=True):
            result = asyncio.run(
                pe.download_release(
                    pcfg.TIDAL,
                    "https://tidal.com/browse/track/4uLU6hMCjMI75M1A2tKUQC",
                    "track",
                    self.workdir,
                )
            )
        self.assertEqual(result.provider, "deezer-arl")
        self.assertEqual(result.tracks[0]["song_name"], "Like Me")

    def test_real_fallback_downloads_matched_track_into_workdir(self):
        os.environ["FALLBACK_ENABLED"] = "1"
        from dl_utils import deezer_download as dz

        async def no_song_link(url):
            return None

        async def deezer_search(url, *, params=None):
            self.assertEqual(url, "https://api.deezer.com/search/track")
            self.assertEqual(params["q"], "T-Mass Like Me")
            return {"data": [{"id": 987654}]}

        info = mb.ReleaseInfo(
            platform=pcfg.QOBUZ,
            kind="track",
            title="Like Me",
            artist="T-Mass",
        )
        song = {"SNG_ID": "987654", "SNG_TITLE": "Like Me", "ART_NAME": "T-Mass", "TRACK_NUMBER": "1"}
        downloaded = []

        def download_song(received_song, audio_format, output_file):
            downloaded.append((received_song, audio_format, output_file))
            Path(output_file).write_bytes(b"fLaC\x00\x00")

        canonical = mb.ReleaseInfo(
            platform=pcfg.DEEZER, kind="track", object_id="987654",
            title="Like Me", artist="T-Mass", album_title="Like Me",
            release_date="2024-01-02", year="2024", cover_data=b"jpg",
        )
        with mock.patch.object(mb, "odesli_links", no_song_link), mock.patch.object(
            mb, "release_info", return_value=info
        ), mock.patch.object(mb, "_fetch_json", deezer_search), mock.patch.object(
            mb, "deezer_track", return_value=canonical
        ), mock.patch.object(
            pcfg, "has_deezer_credentials", return_value=True
        ), mock.patch.object(dz, "session", object()), mock.patch.object(
            dz, "get_song_infos_from_deezer_website", return_value=song
        ) as get_infos, mock.patch.object(dz, "get_file_format", return_value=(".flac", "FLAC")), mock.patch.object(
            dz, "download_song", download_song
        ):
            result = asyncio.run(
                pe.download_via_deezer_fallback(
                    pcfg.QOBUZ, "https://open.qobuz.com/track/abc", "track", self.workdir
                )
            )

        get_infos.assert_called_once_with("track", "987654")
        self.assertEqual(downloaded[0][1], "FLAC")
        self.assertTrue(Path(downloaded[0][2]).is_file())
        self.assertEqual(result.provider, "deezer-arl-fallback")
        self.assertEqual(result.tracks[0]["song_name"], "Like Me")
        self.assertEqual(result.metadata.artist, "T-Mass")
        self.assertEqual(result.metadata.release_date, "2024-01-02")
        self.assertEqual(result.metadata.cover_data, b"jpg")
        self.assertEqual(result.metadata.source_label, "Deezer ARL (Fallback from Qobuz)")

    def test_search_fallback_uses_qobuz_page_title(self):
        url = "https://www.qobuz.com/us-en/album/like-me-t-mass-britt-lari/jge3cs7viw6jb"

        async def fetch_json(url, *, params=None):
            if url == mb._ODESLI or url.startswith(mb._QOBUZ_API) or url == mb._ITUNES_SEARCH:
                return None
            self.assertEqual(url, "https://api.deezer.com/search/album")
            self.assertEqual(params["q"], "T-Mass Like Me")
            return {"data": [{"id": 903788892}]}

        async def fetch_text(url, *, params=None):
            return '<meta property="og:title" content="Like Me, T-Mass - Qobuz">'

        with mock.patch.object(mb, "_fetch_json", fetch_json), mock.patch.object(
            mb, "_fetch_text", fetch_text
        ):
            matched, _ = asyncio.run(pe._deezer_match(url, "album"))
        self.assertEqual(matched, "https://www.deezer.com/album/903788892")

    def test_direct_song_link_match_is_used_before_search(self):
        async def odesli(url):
            return {"matched_urls": {"deezer": "https://www.deezer.com/track/123"}}

        async def search_should_not_run(url):
            self.fail("metadata search must not run after a direct match")

        with mock.patch.object(mb, "odesli_links", odesli), mock.patch.object(
            mb, "release_info", search_should_not_run
        ):
            matched, _ = asyncio.run(pe._deezer_match("https://tidal.com/track/456", "track"))
        self.assertEqual(matched, "https://www.deezer.com/track/123")

    def test_album_match_is_not_accepted_for_track_request(self):
        for candidate in (
            "https://www.deezer.com/album/123",
            "https://www.deezer.com/album/123?track=456",
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(pe._matching_deezer_url(candidate, "track"), "")
        self.assertEqual(pe._matching_deezer_url("https://www.deezer.com/album/123", "album"), "https://www.deezer.com/album/123")

    def test_fallback_is_disabled_by_default(self):
        with self.assertRaisesRegex(pe.EngineError, "FALLBACK_ENABLED=0"):
            asyncio.run(
                pe.download_via_deezer_fallback(
                    pcfg.TIDAL, "https://tidal.com/track/456", "track", self.workdir
                )
            )

    def test_fallback_requires_credentials(self):
        os.environ["FALLBACK_ENABLED"] = "1"
        with mock.patch.object(pcfg, "has_deezer_credentials", return_value=False):
            with self.assertRaisesRegex(pe.EngineError, "DEEZER_TOKEN"):
                asyncio.run(
                    pe.download_via_deezer_fallback(
                        pcfg.TIDAL, "https://tidal.com/track/456", "track", self.workdir
                    )
                )


class TestProgressAndHeaders(ProductionCase):
    def test_delivered_quality_uses_audio_properties_not_requested_mode(self):
        if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg is not installed")
        import subprocess
        path = self.root / "sample.flac"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.05:sample_rate=96000",
            "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
            str(path), "-y",
        ], check=True, capture_output=True)
        label = pe._quality_label([{"song_path": str(path), "file_extension": ".flac"}])
        self.assertEqual(label, "FLAC 24-bit/96 kHz")

    def test_http_sessions_allow_amazon_sixty_four_kilobyte_headers(self):
        import inspect
        from dl_utils import link_resolver

        source = inspect.getsource(mb._Session.get)
        self.assertIn("max_line_size=65536", source)
        self.assertIn("max_field_size=65536", source)
        self.assertIn("max_line_size=65536", inspect.getsource(link_resolver.resolve_url))

    async def test_progress_editor_throttles_and_ignores_not_modified(self):
        message = mock.Mock()
        message.edit_text = mock.AsyncMock()
        progress = st._ProgressEditor(message, interval=2.0)
        with mock.patch.object(st.time, "monotonic", side_effect=[10.0, 10.5, 12.1]):
            await progress("⬇️ Downloading...")
            await progress("⬇️ Downloading...")
            await progress("📦 Packaging...")
        self.assertEqual(message.edit_text.await_count, 2)

    def test_structured_failure_names_platform_url_engine_and_fallback(self):
        url = "https://open.qobuz.com/album/abc"
        text = st._failure_text(pcfg.QOBUZ, "album", url, pe.EngineError("outage"))
        self.assertIn("Qobuz Album Download Failed", text)
        self.assertIn(f"Requested: {url}", text)
        self.assertIn("Engine: streamrip", text)
        self.assertIn("Fallback: disabled (FALLBACK_ENABLED=0)", text)
        self.assertIn("Reason: outage", text)

        os.environ["FALLBACK_ENABLED"] = "1"
        text = st._failure_text(pcfg.APPLE, "track", url, RuntimeError("network dropped"))
        self.assertIn("Apple Music Track Download Failed", text)
        self.assertIn("Engine: gamdl", text)
        self.assertIn("Fallback: enabled (FALLBACK_ENABLED=1)", text)

    def test_failure_reason_is_single_line_and_cleanly_truncated(self):
        text = st._failure_text(
            pcfg.QOBUZ,
            "track",
            "https://open.qobuz.com/track/abc",
            pe.EngineError("first line\nsecond line\n" + "x" * 700),
        )
        reason = next(line for line in text.splitlines() if line.startswith("• Reason: "))
        self.assertIn("first line second line", reason)
        self.assertLessEqual(len(reason), len("• Reason: ") + 601)
        self.assertTrue(reason.endswith("…"))



class TestMediaUrlFilters(ProductionCase):
    def test_modern_youtube_links_and_captions(self):
        urls = (
            "https://youtu.be/dQw4w9WgXcQ?si=abc123",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123",
            "https://www.youtube.com/watch?list=PL123&v=dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ&si=abc",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share",

        )
        for url in urls:
            with self.subTest(url=url):
                text = f"please get this {url} thanks"
                self.assertEqual(yt._matched_media_url(text, yt.YOUTUBE_URL_REGEX), url)

    def test_soundcloud_and_short_links(self):
        for url in (
            "https://soundcloud.com/artist/track?utm_source=share",
            "https://m.soundcloud.com/artist/track",
            "https://on.soundcloud.com/AbCdEf123?si=abc",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    yt._matched_media_url(f"listen: {url}", yt.SOUNDCLOUD_URL_REGEX), url
                )

    def test_unrelated_links_do_not_match(self):
        self.assertFalse(yt.YOUTUBE_URL_REGEX.search("https://example.com/watch?v=dQw4w9WgXcQ"))
        self.assertFalse(yt.SOUNDCLOUD_URL_REGEX.search("https://example.com/artist/track"))


if __name__ == "__main__":
    unittest.main()
