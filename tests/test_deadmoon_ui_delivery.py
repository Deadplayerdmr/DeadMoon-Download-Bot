"""Public UI, native audio, archive sizing and ASCII animation tests."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dl_utils import archive_delivery as ad
from dl_utils import ascii_art
from dl_utils import metadata_bridge as mb
from dl_utils import platform_config as pcfg
from handlers import streaming


class PublicBoardTests(unittest.TestCase):
    def test_board_has_no_engine_credentials_or_admin_statistics(self):
        board = streaming.public_platforms_board("test_bot")
        for platform in pcfg.PLATFORMS:
            self.assertIn(pcfg.PLATFORM_LABELS[platform], board)
        for required in ("<b>Send me a link</b>", "<b>Supported:</b>", "@test_bot", "▫️", "✅"):
            self.assertIn(required, board)
        for private in ("engine", "creds ✓", "no creds", "Downloads", "Active user IDs", "ENABLE_FLAC", "Cross-platform fallback", "Auto-delete", "Hosting", "Max artist releases"):
            self.assertNotIn(private, board)
        self.assertIn("currently disabled across all platforms", board)
        self.assertIn("DeadMoon Status Report", streaming.status_report())


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deadmoon-delivery-")
        self.root = Path(self.temp.name)
        self.old_env = os.environ.copy()
        os.environ["AUTO_DELETE_ENABLED"] = "0"
        self.audio = self.root / "song.flac"
        self.audio.write_bytes(b"x" * (8 * 1024 * 1024))
        self.metadata = {
            "artist": "Artist", "title": "Song", "album_title": "Album", "year": "2026",
            "clean_artist": "Artist", "clean_title": "Song", "cover_data": None,
            "tracks_api_data": [], "artists_list": ["Artist"],
        }
        self.track = {"song_path": str(self.audio), "song_name": "Song", "artist_name": "Artist", "file_extension": ".flac", "duration": 12}
        self.event = mock.AsyncMock()
        self.event.answer_audio = mock.AsyncMock(return_value=mock.AsyncMock())
        self.event.answer_document = mock.AsyncMock(return_value=mock.AsyncMock())
        self.event.answer = mock.AsyncMock(return_value=mock.AsyncMock())
        self.event.answer_photo = mock.AsyncMock(return_value=mock.AsyncMock())

    async def asyncTearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def test_file_size_is_human_readable(self):
        self.assertEqual(ad.format_file_size(8 * 1024 * 1024), "8.0 MB")
        self.assertEqual(ad.format_file_size(512 * 1024), "512.0 KB")

    async def test_audio_mode_sends_original_attachment_without_archiving(self):
        with mock.patch.object(ad.platform_config, "output_format", return_value="audio"):
            with mock.patch.object(ad, "deliver_zip", new=mock.AsyncMock()) as zipped:
                await ad.create_and_send_zip(self.event, self.metadata, [self.track], False)
        zipped.assert_not_awaited()
        self.assertEqual(self.event.answer_audio.call_args.kwargs["title"], "Song")
        self.assertIn("8.0 MB", self.event.answer_audio.call_args.kwargs["caption"])

    async def test_album_audio_reports_total_size_and_artist_reports_releases(self):
        with mock.patch.object(ad.platform_config, "output_format", return_value="audio"):
            await ad.create_and_send_zip(self.event, self.metadata, [self.track], True)
            await ad.create_and_send_artist_zip(self.event, {"name": "Artist"}, [(self.root, self.metadata, [self.track])])
        self.assertEqual(self.event.answer_audio.await_count, 2)
        self.assertIn("Album", self.event.answer.await_args_list[0].args[0])
        self.assertIn("Artist", self.event.answer.await_args_list[1].args[0])

    async def test_direct_zip_size_is_exact_on_disk_and_deliver_zip_always_archives(self):
        with mock.patch.object(ad.platform_config, "output_format", return_value="audio"):
            with mock.patch.object(ad.platform_config, "copy_files_path", return_value=""):
                await ad.deliver_zip(self.event, {str(self.audio): "Artist/Song.flac"}, "caption", "Song", "Song")
        caption = self.event.answer_document.call_args.kwargs["caption"]
        self.assertIn("Archive Size", caption)
        self.assertIn("(.zip)", caption)
        self.assertIn("KB", caption)


class MetadataFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_tidal_uses_api_then_public_search_when_odesli_is_blocked(self):
        url = "https://tidal.com/track/123"
        expected = mb.ReleaseInfo(title="Resolved")
        with mock.patch.object(mb, "odesli_links", new=mock.AsyncMock(return_value=None)):
            with mock.patch.object(mb, "tidal_api_metadata", new=mock.AsyncMock(return_value=expected)) as tidal:
                self.assertIs(await mb.release_info(pcfg.TIDAL, "track", "123", url=url), expected)
                tidal.assert_awaited_once_with("track", "123")
        with mock.patch.object(mb, "odesli_links", new=mock.AsyncMock(return_value=None)):
            with mock.patch.object(mb, "tidal_api_metadata", new=mock.AsyncMock(return_value=None)):
                with mock.patch.object(mb, "public_search_metadata", new=mock.AsyncMock(return_value=expected)) as public:
                    self.assertIs(await mb.release_info(pcfg.TIDAL, "track", "123", url=url), expected)
                    public.assert_awaited_once()


class YtDlpArtistTests(unittest.IsolatedAsyncioTestCase):
    async def test_artist_catalogue_is_capped_and_relative_urls_are_normalized(self):
        payload = {"uploader": "Artist", "entries": [{"id": "1", "title": "Song", "url": "/artist/song"}]}
        with mock.patch.object(mb, "_extract_artist_playlist", return_value=payload) as extract:
            artist, releases = await mb.artist_album_links(pcfg.SOUNDCLOUD, "artist")
        self.assertEqual(artist["name"], "Artist")
        self.assertEqual(releases[0]["url"], "https://soundcloud.com/artist/song")
        self.assertEqual(extract.call_args.args[1]["playlistend"], pcfg.max_artist_releases() + 1)

    async def test_streaming_router_owns_youtube_and_soundcloud_artist_links(self):
        from dl_utils.link_resolver import parse_link
        for url in ("https://music.youtube.com/channel/UC123", "https://soundcloud.com/artist"):
            link = parse_link(url)
            event = mock.AsyncMock()
            with mock.patch.object(streaming, "_handle_artist", new=mock.AsyncMock()) as handler:
                await streaming._dispatch(event, link)
            handler.assert_awaited_once_with(event, link)


class AsciiTests(unittest.TestCase):
    def test_four_distinct_moon_frames_and_log_safe_boot(self):
        frames = [ascii_art.get_moon_frame(index) for index in range(4)]
        self.assertEqual(len(set(frames)), 4)
        self.assertTrue(all(ascii_art.BANNER in frame for frame in frames))
        with mock.patch("sys.stdout.isatty", return_value=False), mock.patch("builtins.print") as output:
            ascii_art.run_boot_animation()
        rendered = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn(ascii_art.BANNER, rendered)
        self.assertIn("Moonlight Engine Ready.", rendered)
        self.assertNotIn("\x1b", rendered)


if __name__ == "__main__":
    unittest.main()
