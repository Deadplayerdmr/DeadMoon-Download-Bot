"""Regression checks for the six production runtime failures.

External HTTP and audio-byte boundaries are replaced, but the real formatting,
resolution, and dispatcher functions are exercised.
"""

import asyncio
import contextlib
import os
import shutil
import subprocess
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

    async def test_authenticatedeezer_reload_error_escapes_exception_text(self):
        with isolated_main() as (main, namespace):
            class Event:
                text = "/authenticatedeezer " + "x" * 50

                async def reply(self, text, **kwargs):
                    self.reply_text = text
                    self.reply_kwargs = kwargs

            event = Event()
            with mock.patch.object(main, "set_deezer_token", return_value=True), mock.patch.object(
                main, "refresh_deezer_session",
                new=mock.AsyncMock(side_effect=RuntimeError("reload_* <failed & unsafe>")),
            ):
                await main.authenticate_deezer(event)

        self.assertEqual(event.reply_kwargs["parse_mode"], "HTML")
        self.assertIn("<code>reload_* &lt;failed &amp; unsafe&gt;</code>", event.reply_text)
        self.assertNotIn("reload_* <failed & unsafe>", event.reply_text)

    async def test_legacy_arl_commands_are_not_registered(self):
        with isolated_main() as (main, _):
            bot = mock.Mock()
            bot.set_my_commands = mock.AsyncMock()
            await main.set_admin_commands(bot)
            menu = {
                command.command
                for call in bot.set_my_commands.await_args_list
                for command in call.kwargs["commands"]
            }
            self.assertIn("authenticatedeezer", menu)
            self.assertFalse({"arl", "newarl"} & menu)
            self.assertFalse(hasattr(main, "show_arl"))
            self.assertFalse(hasattr(main, "update_arl"))

            legacy_handlers = []
            for handler in main.dp.message.handlers:
                if handler.callback in (getattr(main, "show_arl", None), getattr(main, "update_arl", None)):
                    legacy_handlers.append(handler)
                    continue
                for filter_ in handler.filters:
                    callback = getattr(filter_, "callback", None)
                    commands = getattr(callback, "commands", ()) if callback else ()
                    if commands and {"arl", "newarl"} & set(commands):
                        legacy_handlers.append(handler)
            self.assertEqual(legacy_handlers, [])

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

    def test_qobuz_api_identity_precedes_conflicting_odesli_metadata(self):
        direct_info = mb.ReleaseInfo(
            platform=pcfg.QOBUZ, kind="album", object_id="gz6zuz3ypmamc",
            artist="Fred V & Grafix", album_title="Recognise", year="2014",
            album_link="https://open.qobuz.com/album/gz6zuz3ypmamc",
            source="qobuz-api",
        )

        async def no_siblings(url, *, params=None, headers=None):
            return None

        odesli_info = mb.ReleaseInfo(
            platform=pcfg.QOBUZ, kind="album", object_id="gz6zuz3ypmamc",
            artist="FRESCEN PHANOR", album_title="Winer Christmas",
            album_link="https://music.apple.com/us/album/spam/1754153655",
            source="itunes-search",
            siblings={"apple": "https://music.apple.com/us/album/spam/1754153655"},
        )

        async def odesli(url, kind, object_id, platform):
            return odesli_info

        with mock.patch.object(mb, "_from_odesli", side_effect=odesli), mock.patch.object(
            mb, "qobuz_public", new=mock.AsyncMock(return_value=direct_info)
        ) as direct, mock.patch.object(mb, "_fetch_json", side_effect=no_siblings), mock.patch.object(
            mb, "_attach_cover", new=mock.AsyncMock()
        ):
            info = asyncio.run(mb.release_info(
                pcfg.QOBUZ, "album", "gz6zuz3ypmamc",
                url="https://play.qobuz.com/album/gz6zuz3ypmamc",
            ))
        self.assertIs(info, direct_info)
        self.assertEqual(info.artist, "Fred V & Grafix")
        self.assertEqual(info.album_title, "Recognise")
        self.assertEqual(info.year, "2014")
        direct.assert_awaited_once_with("album", "gz6zuz3ypmamc")

    def test_generic_platform_titles_are_rejected_for_html_and_json_ld(self):
        for name in (
            "Qobuz", "Play Qobuz", "Open Qobuz", "Tidal", "Spotify",
            "Apple Music", "Deezer", "SoundCloud", "Amazon Music",
        ):
            with self.subTest(name=name):
                async def generic_page(url, *, params=None, headers=None):
                    return f'<title>{name}</title><script type="application/ld+json">{{"@type":"MusicAlbum","name":"{name}","byArtist":{{"name":"{name}"}}}}</script>'

                with mock.patch.object(mb, "_fetch_text", side_effect=generic_page):
                    info = asyncio.run(mb.page_metadata(
                        "https://example.test/album/rawid123", pcfg.QOBUZ, "album", "rawid123"
                    ))
                self.assertIsNone(info)
                self.assertEqual(mb._search_metadata_hint(None, "album", "https://open.qobuz.com/album/rawid123"), "")

    def test_qobuz_api_uses_configured_authentication(self):
        fields = {
            "app_id": "configured-app-id",
            "password_or_token": "configured-user-token",
            "use_auth_token": True,
        }

        async def fetch(url, *, params=None, headers=None):
            self.assertEqual(url, f"{mb._QOBUZ_API}/album/get")
            self.assertEqual(params, {"album_id": "123456", "app_id": "configured-app-id"})
            self.assertEqual(headers, {
                "X-App-Id": "configured-app-id",
                "X-User-Auth-Token": "configured-user-token",
            })
            return {
                "id": 123456,
                "title": "Recognise",
                "release_date_original": "2014-06-02",
                "artist": {"name": "Fred V & Grafix"},
            }

        with mock.patch.object(mb.pcfg, "qobuz_fields", return_value=fields), mock.patch.object(
            mb, "_fetch_json", side_effect=fetch
        ):
            info = asyncio.run(mb.qobuz_public("album", "123456"))
        self.assertEqual(info.album_title, "Recognise")
        self.assertEqual(info.artist, "Fred V & Grafix")
        self.assertEqual(info.album_link, "https://open.qobuz.com/album/123456")

    def test_generic_qobuz_shell_never_searches_itunes_or_returns_spam(self):
        spam_url = "https://music.apple.com/us/album/winer-christmas-and-holiday-season-dj-viral-qobuz-i/1754153655?uo=4"
        spam_title = "Winer Christmas and Holiday season / DJ Viral / Qobuz / I tune / 7 Digital / Tik tok / Youtube / (Special Version) - Single"
        spam = mb.ReleaseInfo(
            platform=pcfg.APPLE,
            artist="FRESCEN PHANOR",
            album_title=spam_title,
            album_link=spam_url,
            source="itunes-search",
        )

        for object_id in ("gz6zuz3ypmamc", "jnhnqku4red9b"):
            with self.subTest(object_id=object_id):
                requested = f"https://play.qobuz.com/album/{object_id}"
                link = st.parse_link(requested)
                self.assertEqual(link.url, f"https://open.qobuz.com/album/{object_id}")

                async def no_odesli(url):
                    return None

                async def empty_json(url, *, params=None, headers=None):
                    self.assertNotEqual(url, mb._ITUNES_SEARCH)
                    return None

                async def generic_page(url, *, params=None, headers=None):
                    return "<html><head><title>Qobuz</title><meta property=\"og:title\" content=\"Qobuz\"></head></html>"

                async def poisoned_search(*args, **kwargs):
                    self.fail("a generic Qobuz shell must not trigger a blind iTunes search")

                with mock.patch.object(mb, "odesli_links", no_odesli), mock.patch.object(
                    mb, "_fetch_json", side_effect=empty_json
                ), mock.patch.object(mb, "_fetch_text", side_effect=generic_page), mock.patch.object(
                    mb, "itunes_search", side_effect=poisoned_search
                ):
                    info = asyncio.run(mb.release_info(
                        link.platform, link.kind, link.object_id, url=link.original_url
                    ))
                self.assertIsNone(info)
                self.assertNotIn("FRESCEN PHANOR", str(info))
                self.assertNotIn(spam_url, str(info))

                engine = pe.ReleaseMetadata(
                    artist="Verified Artist",
                    title="Verified Album",
                    album_title="Verified Album",
                    year="2020",
                    source_label="Qobuz (Direct · streamrip)",
                    album_link=link.url,
                )
                merged = mb.merge_info(engine, spam)
                self.assertEqual(merged["artist"], "Verified Artist")
                self.assertEqual(merged["album_title"], "Verified Album")
                self.assertEqual(merged["album_link"], link.url)
                self.assertNotEqual(merged["album_link"], spam_url)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for real FLAC tags")
    def test_downloaded_flac_tags_win_and_name_release_archives(self):
        import hashlib
        from dl_utils.archive_delivery import hashed_archive_basename, release_zip_names

        cases = (
            (
                "gz6zuz3ypmamc", "Fred V & Grafix", "Recognise", "2014", "Hydra",
                "Fred V & Grafix - Recognise (2014)/01. Fred V & Grafix - Hydra.flac",
                "Fred_V_&_Grafix_-_Recognise_",
            ),
            (
                "jnhnqku4red9b", "Mefjus, Camo & Krooked", "Sientelo", "2021", "Sientelo",
                "Mefjus, Camo & Krooked - Sientelo (2021)/01. Mefjus - Sientelo.flac",
                "Mefjus,_Camo_&_Krooked_-_Sientelo_",
            ),
        )
        spam = mb.ReleaseInfo(
            platform=pcfg.APPLE,
            artist="FRESCEN PHANOR",
            album_title="Winer Christmas and Holiday season / Qobuz - Single",
            year="2024",
            release_date="2024-06-24",
            album_link="https://music.apple.com/us/album/winer-christmas/1754153655?uo=4",
            source="itunes-search",
        )
        for object_id, artist, album, year, song, relative, zip_prefix in cases:
            with self.subTest(object_id=object_id):
                workdir = self.root / object_id
                audio_path = workdir / relative
                audio_path.parent.mkdir(parents=True)
                subprocess.run([
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=stereo", "-t", "0.05", "-c:a", "flac",
                    "-metadata", f"album={album}", "-metadata", f"albumartist={artist}",
                    "-metadata", f"artist={artist}", "-metadata", f"title={song}",
                    "-metadata", f"date={year}", "-metadata", "tracknumber=1",
                    str(audio_path), "-y",
                ], check=True, capture_output=True)

                audio, release_dir, _ = pe.discover_release(workdir)
                inferred = pe.infer_release_from_paths(audio, workdir=workdir)
                self.assertEqual(inferred, (artist, album, year))
                result = pe._result_from_workdir(
                    pcfg.QOBUZ, "direct", "streamrip",
                    f"https://open.qobuz.com/album/{object_id}", "album", workdir,
                )
                self.assertEqual(result.tracks[0]["song_name"], song)
                self.assertEqual(result.tracks[0]["artist_name"], artist)
                self.assertEqual(result.tracks[0]["TRACK_NUMBER"], "1")
                merged = mb.merge_info(result.metadata, spam)
                self.assertEqual(merged["artist"], artist)
                self.assertEqual(merged["album_title"], album)
                self.assertEqual(merged["title"], album)
                self.assertEqual(merged["year"], year)
                self.assertEqual(merged["release_date"], year)
                self.assertEqual(merged["album_link"], f"https://open.qobuz.com/album/{object_id}")

                hash_title, copy_name = release_zip_names(merged, True)
                archive_name = hashed_archive_basename(hash_title, copy_name) + ".zip"
                expected_hash = hashlib.md5(hash_title.encode()).hexdigest()[:8]
                self.assertEqual(archive_name, f"{zip_prefix}({expected_hash}).zip")
                self.assertRegex(archive_name, rf"^{__import__('re').escape(zip_prefix)}\([0-9a-f]{{8}}\)\.zip$")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for real audio tags")
    def test_m4a_and_mp3_tags_enrich_release_and_track_metadata(self):
        for extension, codec in (("m4a", "aac"), ("mp3", "libmp3lame")):
            with self.subTest(extension=extension):
                workdir = self.root / f"tagged-{extension}"
                release_dir = workdir / "Wrong Folder Name"
                release_dir.mkdir(parents=True)
                path = release_dir / f"07. Wrong Artist - Wrong Title.{extension}"
                subprocess.run([
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=stereo", "-t", "0.05", "-c:a", codec,
                    "-metadata", "album=Tagged Album", "-metadata", "albumartist=Tagged Album Artist",
                    "-metadata", "artist=Tagged Track Artist", "-metadata", "title=Tagged Song",
                    "-metadata", "date=2022-03-04", "-metadata", "tracknumber=7/12",
                    str(path), "-y",
                ], check=True, capture_output=True)
                if extension == "m4a":
                    from mutagen.mp4 import MP4
                    tagged = MP4(path)
                    tagged["aART"] = ["Tagged Album Artist"]
                    tagged["trkn"] = [(7, 12)]
                    tagged.save()
                result = pe._result_from_workdir(
                    pcfg.QOBUZ, "direct", "streamrip",
                    "https://play.qobuz.com/album/numeric123", "album", workdir,
                )
                self.assertEqual(result.metadata.artist, "Tagged Album Artist")
                self.assertEqual(result.metadata.album_title, "Tagged Album")
                self.assertEqual(result.metadata.year, "2022")
                self.assertEqual(result.metadata.release_date, "2022-03-04")
                self.assertEqual(result.tracks[0]["artist_name"], "Tagged Track Artist")
                self.assertEqual(result.tracks[0]["song_name"], "Tagged Song")
                self.assertEqual(result.tracks[0]["TRACK_NUMBER"], "7")
                self.assertEqual(result.metadata.album_link, "https://open.qobuz.com/album/numeric123")


class TestMetadataLabelMerging(unittest.TestCase):
    @staticmethod
    def _image(size):
        from io import BytesIO
        from PIL import Image

        output = BytesIO()
        Image.new("RGB", (size, size), (20, 30, 40)).save(output, "JPEG")
        return output.getvalue()

    def test_cover_urls_request_provider_maximum_sizes(self):
        cases = {
            "https://i.scdn.co/image/ab67616d00001e02abc": "https://i.scdn.co/image/ab67616d0000b273abc",
            "https://e-cdns-images.dzcdn.net/images/cover/hash/500x500-000000-100-0-0.jpg": "https://e-cdns-images.dzcdn.net/images/cover/hash/1200x1200-000000-100-0-0.jpg",
            "https://is1-ssl.mzstatic.com/image/thumb/Music/abc/100x100bb.jpg": "https://is1-ssl.mzstatic.com/image/thumb/Music/abc/1200x1200bb.jpg",
            "https://resources.tidal.com/images/a/b/c/640x640.jpg": "https://resources.tidal.com/images/a/b/c/1280x1280.jpg",
            "https://static.qobuz.com/images/covers/a/b_600.jpg": "https://static.qobuz.com/images/covers/a/b_max.jpg",
            "https://i1.sndcdn.com/artworks-a-large.jpg": "https://i1.sndcdn.com/artworks-a-t500x500.jpg",
        }
        for original, upgraded in cases.items():
            with self.subTest(original=original):
                self.assertEqual(mb.upgrade_cover_url(original), upgraded)

    def test_spotify_oembed_artist_fallback_does_not_leave_unknown(self):
        from dl_utils import dotify_shim

        entity = {"trackList": [{"subtitle": "Coasting"}]}
        with mock.patch.object(mb, "_fetch_json", new=mock.AsyncMock(return_value={
            "title": "Album Title", "thumbnail_url": "https://i.scdn.co/image/ab67616d00001e02abc"
        })), mock.patch.object(mb, "page_metadata", new=mock.AsyncMock(return_value=None)), mock.patch.object(
            dotify_shim, "embed_entity", return_value=entity
        ):
            result = asyncio.run(mb.spotify_oembed("https://open.spotify.com/album/abc"))
        self.assertEqual(result["artist"], "Coasting")
        self.assertIn("0000b273", result["thumbnail"])

    def test_merge_keeps_higher_resolution_cover_and_valid_artist(self):
        engine = pe.ReleaseMetadata(artist="Engine Artist", cover_data=self._image(1200))
        bridge = mb.ReleaseInfo(artist="Unknown Artist", cover_data=self._image(300))
        merged = mb.merge_info(engine, bridge)
        self.assertEqual(merged["artist"], "Engine Artist")
        self.assertEqual(merged["cover_data"], engine.cover_data)

    def test_cover_preparation_preserves_large_photo_dimensions(self):
        from io import BytesIO
        from PIL import Image
        from dl_utils import archive_delivery as ad

        prepared = ad.prepare_cover_bytes(self._image(1200))
        with Image.open(BytesIO(prepared)) as image:
            self.assertEqual(image.size, (1200, 1200))

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
            artist="Engine Artist",
            album_title="Engine Album",
        )
        merged = mb.merge_info(engine, bridge)
        self.assertEqual(merged["artist"], "Engine Artist")
        self.assertEqual(merged["album_title"], "Engine Album")
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

    def test_deezer_fallback_never_searches_generic_or_raw_qobuz_ids(self):
        for object_id in ("gz6zuz3ypmamc", "123456789"):
            with self.subTest(object_id=object_id):
                async def no_odesli(url):
                    return None

                async def no_release_info(*args, **kwargs):
                    return None

                async def forbidden_search(url, *, params=None, headers=None):
                    self.fail("fallback search must not use a raw Qobuz ID or platform name")

                with mock.patch.object(mb, "odesli_links", no_odesli), mock.patch.object(
                    mb, "release_info", side_effect=no_release_info
                ), mock.patch.object(mb, "_fetch_json", side_effect=forbidden_search):
                    matched = asyncio.run(pe._search_deezer_url(
                        f"https://play.qobuz.com/album/{object_id}", "album"
                    ))
                self.assertEqual(matched, "")

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
