"""Offline regression tests for Deezer track progress and issue summaries."""

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")

import utils
from dl_utils import deezer_download as real_deezer
from dl_utils import platform_config as pcfg
from dl_utils import provider_engines as pe
from dl_utils.archive_delivery import append_issue_summary, get_album_caption
from handlers import deezer


class TestNativeDeezerIssues(unittest.IsolatedAsyncioTestCase):
    async def test_track_progress_and_caption_report_fallback_and_failure(self):
        songs = [
            {
                "SNG_ID": "1",
                "SNG_TITLE": "crybaby",
                "ART_NAME": "lil peep",
                "TRACK_NUMBER": "1",
                "FILESIZE_MP3_128": 1,
            },
            {
                "SNG_ID": "2",
                "SNG_TITLE": "unavailable",
                "ART_NAME": "lil peep",
                "TRACK_NUMBER": "2",
            },
        ]

        def download(song, audio_format, path):
            if song["SNG_ID"] == "1":
                Path(path).write_bytes(b"audio")
                return
            raise OSError("not available")

        updates = []
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            deezer, "get_song_infos_from_deezer_website", return_value=songs
        ), mock.patch.object(deezer, "get_file_format", return_value=(".mp3", "MP3_128")), mock.patch.object(
            deezer, "get_artists", return_value="lil peep"
        ), mock.patch.object(deezer, "download_song", side_effect=download), mock.patch.object(
            deezer, "MAX_RETRIES", 1
        ):
            async def record(text):
                updates.append(text)

            tracks = await deezer.download_album(
                "album", download_base_dir=Path(root), progress_cb=record
            )
        self.assertEqual(len(tracks), 1)
        self.assertTrue(tracks[0]["format_fallback"])
        expected = [
            {"id": "1", "title": "crybaby", "artist": {"name": "lil peep"}},
            {"id": "2", "title": "unavailable", "artist": {"name": "lil peep"}},
        ]
        caption = append_issue_summary("<b>Album: crybaby</b>", tracks, expected)
        self.assertIn("Fallback: Track 01 - lil peep - crybaby", caption)
        self.assertIn("Failures: Track 02 - lil peep - unavailable was not available", caption)

    async def test_live_progress_reports_fallback_and_failure(self):
        songs = [
            {"SNG_ID": "1", "SNG_TITLE": "crybaby", "ART_NAME": "lil peep"},
            {"SNG_ID": "2", "SNG_TITLE": "song", "ART_NAME": "lil peep"},
        ]
        updates = []

        async def progress(text):
            updates.append(text)

        def download(song, audio_format, path):
            if song["SNG_ID"] == "1":
                Path(path).write_bytes(b"audio")
            else:
                raise OSError("gone")

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            pcfg, "flac_enabled", return_value=True
        ), mock.patch.object(
            deezer, "get_song_infos_from_deezer_website", return_value=songs
        ), mock.patch.object(deezer, "get_file_format", return_value=(".mp3", "MP3_128")), mock.patch.object(
            deezer, "get_artists", return_value="lil peep"
        ), mock.patch.object(deezer, "download_song", side_effect=download), mock.patch.object(
            deezer, "MAX_RETRIES", 1
        ):
            tracks = await deezer.download_album(
                "album", download_base_dir=Path(root), progress_cb=progress
            )
        self.assertEqual(len(tracks), 1)
        self.assertTrue(any("Downloading track 1/2" in text for text in updates))
        self.assertTrue(any(text.startswith("⚠️ Fallback: Track 01") for text in updates))
        self.assertTrue(any(text.startswith("❌ Failure: Track 02") for text in updates))
        expected = [
            {"id": "1", "title": "crybaby", "artist": {"name": "lil peep"}},
            {"id": "2", "title": "song", "artist": {"name": "lil peep"}},
        ]
        caption = get_album_caption(
            {
                "title": "crybaby",
                "artist": "lil peep",
                "release_date": "10/06/2016",
                "album_link": "https://www.deezer.com/album/994314001",
                "caption_suffix": "\n\n" + "\n".join(
                    line for group in __import__(
                        "dl_utils.archive_delivery", fromlist=["summarize_track_issues"]
                    ).summarize_track_issues(tracks, expected) for line in group
                ),
            }
        )
        self.assertIn("Source: Deezer", caption)
        self.assertIn("Fallback: Track 01 - lil peep - crybaby", caption)
        self.assertIn("Failures: Track 02 - lil peep - song", caption)

    async def test_no_fallback_or_failure_lines_for_lossless_complete_release(self):
        tracks = [{"id": "1", "SNG_ID": "1", "format_fallback": False}]
        expected = [{"id": "1"}]
        self.assertEqual(append_issue_summary("Album: Complete", tracks, expected), "Album: Complete")


class TestFallbackEngineIssues(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_engine_reports_each_track_and_keeps_successes(self):
        songs = [
            {"SNG_ID": "1", "SNG_TITLE": "one", "ART_NAME": "artist"},
            {"SNG_ID": "2", "SNG_TITLE": "two", "ART_NAME": "artist"},
        ]

        class Deezer:
            session = object()
            get_song_infos_from_deezer_website = staticmethod(lambda kind, object_id: songs)
            get_file_format = staticmethod(lambda song: (".mp3", "MP3_128"))
            get_artists = staticmethod(lambda song: song["ART_NAME"])

            @staticmethod
            def download_song(song, audio_format, path):
                if song["SNG_ID"] == "1":
                    Path(path).write_bytes(b"audio")
                else:
                    raise OSError("unavailable")

        updates = []

        async def progress(text):
            updates.append(text)

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_deezer, "session", object()
        ), mock.patch.object(
            real_deezer, "get_song_infos_from_deezer_website", return_value=songs
        ), mock.patch.object(
            real_deezer, "get_file_format", return_value=(".mp3", "MP3_128")
        ), mock.patch.object(
            real_deezer, "get_artists", side_effect=lambda song: song["ART_NAME"]
        ), mock.patch.object(
            real_deezer, "download_song", side_effect=Deezer.download_song
        ), mock.patch.object(pcfg, "flac_enabled", return_value=True), mock.patch.object(
            pcfg, "fallback_enabled", return_value=True
        ), mock.patch.object(pcfg, "has_deezer_credentials", return_value=True), mock.patch.object(
            pe, "_deezer_match", new=mock.AsyncMock(return_value=("https://www.deezer.com/album/1", None))
        ), mock.patch.object(pe.metadata_bridge, "deezer_album", new=mock.AsyncMock(return_value=None)), mock.patch.object(
            pe, "_quality_label", return_value="MP3 128 kbps"
        ):
            result = await pe.download_via_deezer_fallback(
                pcfg.SPOTIFY, "https://open.spotify.com/album/abc", "album", Path(root), progress
            )
        self.assertEqual(len(result.tracks), 1)
        self.assertTrue(any(text.startswith("⚠️ Fallback: Track 01") for text in updates))
        self.assertTrue(any(text.startswith("❌ Failure: Track 02") for text in updates))
        caption = append_issue_summary("Album: Test", result.tracks, songs)
        self.assertIn("Fallback: Track 01", caption)
        self.assertIn("Failures: Track 02", caption)


class TestDiscographyProgress(unittest.IsolatedAsyncioTestCase):
    async def test_release_and_track_updates_include_current_release(self):
        updates = []

        async def progress(text):
            updates.append(text)

        async def release(platform, url, kind, workdir, progress_cb=None):
            if progress_cb:
                await progress_cb("⬇️ Downloading track 1/2: Artist - Song...")
            return pe.EngineResult(
                tracks=[{"id": "1", "song_path": "/tmp/song.flac"}],
                metadata=pe.ReleaseMetadata(),
                mode="direct",
                platform=platform,
            )

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            pe, "download_release", side_effect=release
        ), mock.patch.object(pcfg, "artist_api_delay", return_value=0):
            await pe.download_artist_discography(
                pcfg.DEEZER,
                "https://www.deezer.com/artist/1",
                Path(root),
                [{
                    "id": "a",
                    "url": "https://www.deezer.com/album/a",
                    "artist": "Artist",
                    "title": "Album Title",
                    "year": "2020",
                    "record_type": "album",
                }],
                progress_cb=progress,
            )
        self.assertTrue(any("Release 1/1" in text for text in updates))
        self.assertTrue(any(
            "Release 1/1" in text and "Downloading track 1/2" in text
            for text in updates
        ))


if __name__ == "__main__":
    unittest.main()
