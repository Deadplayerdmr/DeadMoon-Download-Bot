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


class AudioDurationTests(unittest.TestCase):
    def test_m4a_uses_generic_mutagen_parser_without_unsupported_warning(self):
        import types
        from dl_utils import deezer_utils

        parsed = types.SimpleNamespace(info=types.SimpleNamespace(length=123.9))
        with mock.patch.object(deezer_utils.mutagen, "File", return_value=parsed) as parser, mock.patch(
            "builtins.print"
        ) as output:
            duration = deezer_utils.get_audio_duration("track.m4a")
        self.assertEqual(duration, 123)
        parser.assert_called_once_with("track.m4a")
        self.assertFalse(any("Unsupported audio format" in str(call) for call in output.call_args_list))


class PublicBoardTests(unittest.TestCase):
    def test_board_has_no_engine_credentials_or_admin_statistics(self):
        with mock.patch.object(
            pcfg, "platform_enabled", side_effect=lambda platform: platform != pcfg.QOBUZ
        ):
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
            "artist": "Artist", "title": "Song", "album_title": "Album", "year": "2026", "release_date": "2026-01-01",
            "clean_artist": "Artist", "clean_title": "Song", "clean_album_title": "Album", "cover_data": None,
            "album_link": "https://example.com/album/1", "track_link": "https://example.com/track/1",
            "tracks_api_data": [], "artists_list": ["Artist"],
        }
        self.track = {"song_path": str(self.audio), "song_name": "Song", "artist_name": "Artist", "file_extension": ".flac", "duration": 12, "download_dir": str(self.root)}
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
        with mock.patch.object(ad.platform_config, "output_format", return_value="audio"), mock.patch.object(
            ad.platform_config, "copy_files_path", return_value=""
        ):
            with mock.patch.object(ad, "deliver_zip", new=mock.AsyncMock()) as zipped:
                await ad.create_and_send_zip(self.event, self.metadata, [self.track], False)
        zipped.assert_not_awaited()
        self.assertEqual(self.event.answer_audio.call_args.kwargs["title"], "Song")
        self.assertIn("8.0 MB (.flac)", self.event.answer_audio.call_args.kwargs["caption"])

    async def test_track_uses_song_title_not_album_title(self):
        self.metadata.update(title="Album Title", album_title="Album Title")
        self.track.update(song_name="Actual Song", artist_name="Track Artist")
        await ad.send_track_audio(self.event, self.metadata, self.track)
        caption = self.event.answer_audio.await_args.kwargs["caption"]
        self.assertIn("🌙 <b>Track: Actual Song</b>", caption)
        self.assertIn("🎧 Track Artist", caption)
        self.assertIn("📦 <b>Size:</b> 8.0 MB (.flac)", caption)
        self.assertNotIn("🎵", caption)
        self.assertNotIn("Track: Album Title", caption)

    async def test_album_uses_track_artist_and_lists_extensions(self):
        self.metadata.update(artist="Unknown Artist", album_title="Album Title")
        self.track.update(song_name="Actual Song", artist_name="Coasting")
        await ad.send_album_audio(self.event, self.metadata, [self.track])
        album_caption = self.event.answer.await_args.args[0]
        track_caption = self.event.answer_audio.await_args.kwargs["caption"]
        self.assertIn("🎧 Coasting", album_caption)
        self.assertIn("🌙 <b>Track: Actual Song</b>", track_caption)
        self.assertIn("8.0 MB (.flac)", album_caption)

    async def test_hosted_audio_mode_sends_cover_and_zip_without_audio(self):
        hosted = self.root / "files"
        self.metadata["cover_data"] = b"\xff\xd8\xff\xe0fakejpeg"
        with mock.patch.object(ad.platform_config, "output_format", return_value="audio"), mock.patch.object(
            ad.platform_config, "copy_files_path", return_value=str(hosted)
        ), mock.patch.object(
            ad.platform_config, "file_link_template", return_value="https://example.com/download/{0}"
        ), mock.patch.object(ad.platform_config, "send_album_cover", return_value=True):
            await ad.create_and_send_zip(self.event, self.metadata, [self.track], False)
        self.event.answer_audio.assert_not_awaited()
        self.event.answer_photo.assert_awaited_once()
        self.assertIn("Track: Song", self.event.answer_photo.await_args.kwargs["caption"])
        archives = list(hosted.glob("Artist_-_Song_(*).zip"))
        self.assertEqual(len(archives), 1)
        link_text = self.event.answer.await_args.args[0]
        self.assertIn("https://example.com/download/Artist_-_Song_%28", link_text)
        self.assertIn("📦 <b>Archive Size:</b>", link_text)
        self.assertIn("(.zip)", link_text)

    async def test_hosted_album_sends_no_individual_audio(self):
        hosted = self.root / "album-files"
        with mock.patch.object(ad.platform_config, "output_format", return_value="zip"), mock.patch.object(
            ad.platform_config, "copy_files_path", return_value=str(hosted)
        ), mock.patch.object(ad, "send_album_audio", new=mock.AsyncMock()) as audio:
            await ad.create_and_send_zip(self.event, self.metadata, [self.track], True)
        audio.assert_not_awaited()
        self.event.answer_audio.assert_not_awaited()
        self.assertEqual(len(list(hosted.glob("*.zip"))), 1)


    async def test_zip_format_without_hosting_never_sends_audio(self):
        with mock.patch.object(ad.platform_config, "output_format", return_value="zip"), mock.patch.object(
            ad.platform_config, "copy_files_path", return_value=""
        ), mock.patch.object(ad, "send_track_audio", new=mock.AsyncMock()) as audio, mock.patch.object(
            ad, "send_album_audio", new=mock.AsyncMock()
        ) as album_audio:
            await ad.create_and_send_zip(self.event, self.metadata, [self.track], False)
        audio.assert_not_awaited()
        album_audio.assert_not_awaited()
        self.event.answer_audio.assert_not_awaited()
        self.event.answer_document.assert_awaited_once()

    async def test_hosted_artist_mode_sends_cover_and_zip_without_audio(self):
        hosted = self.root / "artist-files"
        artist = {
            "name": "Artist", "clean_name": "Artist",
            "link": "https://example.com/artist/1", "cover_data": b"\xff\xd8\xff\xe0fakejpeg",
        }
        with mock.patch.object(ad.platform_config, "output_format", return_value="audio"), mock.patch.object(
            ad.platform_config, "copy_files_path", return_value=str(hosted)
        ), mock.patch.object(
            ad.platform_config, "file_link_template", return_value="https://example.com/download/{0}"
        ), mock.patch.object(ad.platform_config, "send_album_cover", return_value=True):
            await ad.create_and_send_artist_zip(
                self.event, artist, [(self.root, self.metadata, [self.track])]
            )
        self.event.answer_audio.assert_not_awaited()
        self.event.answer_photo.assert_awaited_once()
        self.assertIn("Artist", self.event.answer_photo.await_args.kwargs["caption"])
        archives = list(hosted.glob("Artist_-_Discography_(*).zip"))
        self.assertEqual(len(archives), 1)
        self.assertIn("Download link:", self.event.answer.await_args.args[0])

    async def test_synthetic_jpeg_cover_is_safe_for_album_and_track_audio(self):
        self.metadata["cover_data"] = b"\xff\xd8\xff\xe0fakejpeg"
        os.environ["SEND_ALBUM_COVER"] = "1"

        await ad.send_album_audio(self.event, self.metadata, [self.track])
        self.event.answer_photo.assert_awaited_once()
        self.assertEqual(self.event.answer_photo.await_args.kwargs["parse_mode"], "HTML")

        await ad.send_track_audio(self.event, self.metadata, self.track)
        self.event.answer_audio.assert_awaited()
        self.event.answer_photo.assert_awaited_once()

    async def test_album_photo_failure_falls_back_to_text(self):
        self.metadata["cover_data"] = b"\xff\xd8\xff\xe0fakejpeg"
        os.environ["SEND_ALBUM_COVER"] = "1"
        self.event.answer_photo.side_effect = RuntimeError("invalid photo")

        await ad.send_album_audio(self.event, self.metadata, [self.track])

        self.event.answer_photo.assert_awaited_once()
        self.assertIn("Album", self.event.answer.await_args.args[0])

    def test_prepare_cover_bytes_rejects_missing_and_non_image_data(self):
        self.assertIsNone(ad.prepare_cover_bytes(None))
        self.assertIsNone(ad.prepare_cover_bytes(b""))
        self.assertIsNone(ad.prepare_cover_bytes(b"not an image"))
        synthetic = b"\xff\xd8\xff\xe0fakejpeg"
        self.assertEqual(ad.prepare_cover_bytes(synthetic), synthetic)

    async def test_album_audio_reports_total_size_and_artist_reports_releases(self):
        with mock.patch.object(ad.platform_config, "output_format", return_value="audio"), mock.patch.object(
            ad.platform_config, "copy_files_path", return_value=""
        ):
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
