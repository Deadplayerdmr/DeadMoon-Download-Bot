"""Offline checks for dl_utils/provider_engines.py.

The engines themselves shell out to streamrip / dotify-cli / gamdl; most external
calls are captured. What IS covered is the code that runs after them: how
a workdir's contents are turned into (artist, album, year) and a track list, and
how the dispatcher chooses and falls back between backends. That is the layer
where a silent mistake produces a mislabelled archive rather than an error, so
it is the layer worth pinning down.

Run with `python3 -m unittest discover tests`. No network, no external binaries.
"""

import asyncio
import functools
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")

import utils  # noqa: E402
from dl_utils import platform_config as pcfg  # noqa: E402
from dl_utils import provider_engines as pe  # noqa: E402
from dl_utils.provider_engines import (  # noqa: E402
    EngineError,
    EngineResult,
    ReleaseMetadata,
)


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


class WorkdirCase(unittest.TestCase):
    """Base that pins TMP_DIR to a scratch tree for the duration of a test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="deadmoon-engine-")
        self.root = Path(self._tmp.name)
        self._saved_tmp_dir = pe.TMP_DIR
        pe.TMP_DIR = str(self.root)
        self._saved_env = os.environ.copy()
        os.environ.pop("FALLBACK_ENABLED", None)
        self._saved_token_env = utils.TOKEN_ENV_PATH
        # An absent token.env keeps a real, credential-bearing file on disk from
        # influencing what a test asserts about mode resolution.
        utils.TOKEN_ENV_PATH = str(self.root / "no-such-token.env")

    def tearDown(self):
        pe.TMP_DIR = self._saved_tmp_dir
        utils.TOKEN_ENV_PATH = self._saved_token_env
        os.environ.clear()
        os.environ.update(self._saved_env)
        self._tmp.cleanup()

    def set_env(self, **kwargs):
        for key, value in kwargs.items():
            os.environ[key] = value

    def stage(self, sub, *files, size=8):
        """Create a staging dir with `files` under it and return its Path."""
        workdir = self.root / sub
        for name in files:
            target = workdir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fLaC" + b"\x00" * size)
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir


# ---------------------------------------------------------------------------
# Staging and teardown
# ---------------------------------------------------------------------------
class TestStaging(WorkdirCase):
    def test_staging_dir_is_platform_kind_id(self):
        path = pe.staging_dir(pcfg.QOBUZ, "album", "foobaz123")
        self.assertEqual(
            path, self.root / pcfg.QOBUZ / "album" / "foobaz123"
        )
        self.assertTrue(path.is_dir())

    def test_staging_dir_is_idempotent(self):
        # Two requests for the same release must share, not fail.
        first = pe.staging_dir(pcfg.TIDAL, "track", "99")
        (first / "01 - A - B.flac").write_bytes(b"x")
        second = pe.staging_dir(pcfg.TIDAL, "track", "99")
        self.assertEqual(first, second)
        self.assertTrue((second / "01 - A - B.flac").exists())

    def test_cleanup_dir_removes_the_whole_tree(self):
        workdir = pe.staging_dir(pcfg.APPLE, "album", "1234")
        (workdir / "Artist" / "Album [2024]").mkdir(parents=True)
        (workdir / "Artist" / "Album [2024]" / "01 - Song.flac").write_bytes(b"x")
        pe.cleanup_dir(workdir)
        self.assertFalse(workdir.exists())

    def test_cleanup_dir_never_raises(self):
        pe.cleanup_dir(None)
        pe.cleanup_dir("")
        pe.cleanup_dir(self.root / "never-existed")
        # A plain file: rmtree refuses it, and cleanup must not raise over it.
        # Handlers delete the staging tree, never a loose path, so leaving the
        # file is harmless; the point of this test is the absence of an exception.
        loose = self.root / "loose.txt"
        loose.write_text("x")
        pe.cleanup_dir(loose)
        self.assertTrue(loose.exists())

    def test_cleanup_dir_survives_a_permission_error(self):
        workdir = self.stage("sc/album/1", "01 - A - B.flac")
        with mock.patch.object(
            pe.shutil, "rmtree", side_effect=OSError("busy")
        ) as rmtree:
            pe.cleanup_dir(workdir)  # must not propagate
            rmtree.assert_called_once()


# ---------------------------------------------------------------------------
# Finding what an engine produced
# ---------------------------------------------------------------------------
class TestFindAudio(WorkdirCase):
    def test_only_known_extensions_count(self):
        workdir = self.stage(
            "deezer/album/1",
            "01 - A - B.flac",
            "02 - A - C.mp3",
            "03 - A - D.WAV",  # uppercase extension still counts
            "cover.jpg",
            "scan.pdf",
            "notes.txt",
        )
        found = pe.find_audio(workdir)
        self.assertEqual(sorted(p.name for p in found), ["01 - A - B.flac", "02 - A - C.mp3", "03 - A - D.WAV"])

    def test_missing_root_is_empty_not_an_error(self):
        self.assertEqual(pe.find_audio(self.root / "nope"), [])

    def test_sorting_is_natural_not_lexicographic(self):
        # Lexicographic order puts track 10 before track 2.
        workdir = self.stage(
            "qobuz/album/2", "01 - A - One.flac", "02 - A - Two.flac", "10 - A - Ten.flac"
        )
        self.assertEqual(
            [p.name for p in pe.find_audio(workdir)],
            ["01 - A - One.flac", "02 - A - Two.flac", "10 - A - Ten.flac"],
        )

    def test_directories_are_not_returned_as_audio(self):
        workdir = self.stage("tidal/album/3", "01 - A - B.flac")
        (workdir / "a_directory.mp3").mkdir()
        self.assertEqual([p.name for p in pe.find_audio(workdir)], ["01 - A - B.flac"])


class TestDiscoverRelease(WorkdirCase):
    def test_flat_layout_uses_the_staging_dir(self):
        workdir = self.stage("qobuz/album/1", "01 - Test Artist - Song.flac")
        audio, release_dir, cover = pe.discover_release(workdir)
        self.assertEqual(len(audio), 1)
        self.assertEqual(release_dir, workdir)
        self.assertIsNone(cover)

    def test_deepest_folder_wins(self):
        workdir = self.stage(
            "apple/album/2", "Test Artist/Kid Amo [2024]/01 - Song.flac"
        )
        _, release_dir, _ = pe.discover_release(workdir)
        self.assertEqual(release_dir.name, "Kid Amo [2024]")

    def test_busiest_folder_wins_over_a_stray_deep_file(self):
        # A bonus track dumped in a subfolder must not become the release.
        workdir = self.stage(
            "tidal/album/3",
            "Main Album (2020)/01 - A - One.flac",
            "Main Album (2020)/02 - A - Two.flac",
            "Main Album (2020)/Extra/03 - A - Three.flac",
        )
        audio, release_dir, _ = pe.discover_release(workdir)
        self.assertEqual(len(audio), 3)
        self.assertEqual(release_dir.name, "Main Album (2020)")

    def test_cover_is_found_by_conventional_name(self):
        for name in ("cover.jpg", "folder.jpg", "Cover.jpg", "cover.png"):
            with self.subTest(name=name):
                workdir = self.stage(f"deezer/album/{name}", "01 - A - B.flac")
                (workdir / name).write_bytes(b"fake-jpeg")
                _, _, cover = pe.discover_release(workdir)
                self.assertEqual(Path(cover).name, name)

    def test_cover_outside_the_release_dir_is_still_found(self):
        workdir = self.stage("spotify/album/9", "Rel/01 - A - B.flac")
        (workdir / "cover.jpg").write_bytes(b"fake-jpeg")
        _, release_dir, cover = pe.discover_release(workdir)
        self.assertEqual(release_dir.name, "Rel")
        self.assertEqual(Path(cover).name, "cover.jpg")

    def test_empty_workdir_reports_nothing(self):
        workdir = self.stage("yt/album/empty")
        self.assertEqual(pe.discover_release(workdir), ([], None, None))

    def test_non_image_folder_files_are_not_mistaken_for_covers(self):
        workdir = self.stage("sc/album/11", "01 - A - B.flac")
        (workdir / "folder.pdf").write_bytes(b"%PDF")
        _, _, cover = pe.discover_release(workdir)
        self.assertIsNone(cover)


class TestDiscoverReleases(WorkdirCase):
    def test_each_release_folder_is_one_group(self):
        workdir = self.stage(
            "deezer/artist/1",
            "Kid Amo (2024)/01 - A - One.flac",
            "Kid Amo (2024)/02 - A - Two.flac",
            "Pastel (2019)/01 - A - Three.flac",
        )
        groups = pe.discover_releases(workdir)
        self.assertEqual(len(groups), 2)
        by_name = {d.name: [p.name for p in ps] for d, ps in groups}
        self.assertEqual(by_name["Kid Amo (2024)"], ["01 - A - One.flac", "02 - A - Two.flac"])
        self.assertEqual(by_name["Pastel (2019)"], ["01 - A - Three.flac"])

    def test_flat_audio_is_a_single_group(self):
        workdir = self.stage("qobuz/artist/2", "01 - A - One.flac", "02 - A - Two.flac")
        groups = pe.discover_releases(workdir)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0][1]), 2)

    def test_empty_workdir(self):
        self.assertEqual(pe.discover_releases(self.stage("tidal/artist/3")), [])


# ---------------------------------------------------------------------------
# Naming the release — the regression lock for path-component leakage
# ---------------------------------------------------------------------------
class TestInferRelease(WorkdirCase):
    def _infer(self, sub, files, fallback=""):
        workdir = self.stage(sub, *files)
        audio, _, _ = pe.discover_release(workdir)
        return pe.infer_release_from_paths(audio, fallback, workdir)

    def test_flat_layout_does_not_leak_the_object_id(self):
        # Flat engine output sits directly in
        # tmp/qobuz/album/<id>. Reading the parent as an artist used to credit
        # the album id as the artist and the kind ("album") as the artist.
        artist, album, year = self._infer(
            "qobuz/album/foobaz123",
            ["01 - Test Artist - Song 1.flac", "02 - Test Artist - Song 2.flac"],
        )
        self.assertEqual(artist, "Test Artist")
        self.assertNotIn("foobaz123", (artist, album))
        self.assertNotIn("album", (artist, album))
        self.assertEqual(album, "Unknown Album")
        self.assertEqual(year, "")

    def test_streamrip_single_folder_splits_artist_from_album(self):
        # folder_format is "{albumartist} - {title} ({year})", so the artist is
        # glued to the front of the folder name and must not survive in the
        # album title.
        artist, album, year = self._infer(
            "tidal/album/123456789",
            ["Test Artist - Kid Amo (2024)/01. Test Artist - Song.flac"],
        )
        self.assertEqual(artist, "Test Artist")
        self.assertEqual(album, "Kid Amo")
        self.assertEqual(year, "2024")

    def test_streamrip_disc_folders_are_not_the_album_title(self):
        workdir = self.stage(
            "qobuz/album/multi",
            "Test Artist - Kid Amo (2024)/Disc 1/01. Test Artist - One.flac",
            "Test Artist - Kid Amo (2024)/Disc 2/01. Test Artist - Two.flac",
        )
        audio, release_dir, _ = pe.discover_release(workdir)
        self.assertEqual(release_dir.name, "Test Artist - Kid Amo (2024)")
        artist, album, year = pe.infer_release_from_paths(audio, workdir=workdir)
        self.assertEqual((artist, album, year), ("Test Artist", "Kid Amo", "2024"))
        tracks = pe.tracks_from_paths(audio, release_dir)
        self.assertEqual([t["TRACK_NUMBER"] for t in tracks], ["1", "2"])

    def test_gamdl_two_folder_layout(self):
        artist, album, year = self._infer(
            "apple/album/1578066229", ["Test Artist/Kid Amo [2024]/01 - Song.flac"]
        )
        self.assertEqual((artist, album, year), ("Test Artist", "Kid Amo", "2024"))

    def test_staging_path_components_never_become_credits(self):
        # The invariant behind all of the above, stated once for every platform
        # and kind this bot stages into.
        banned = {"album", "track", "artist", "deezer", "qobuz", "tidal", "amazon",
                  "spotify", "apple", "yt", "sc"}
        for sub, files in (
            ("qobuz/album/foobaz123", ["01 - Test Artist - Song.flac"]),
            ("tidal/track/987654321", ["01 - Test Artist - Song.flac"]),
            ("amazon/album/B08X1234AB", ["01 - Test Artist - Song.flac"]),
            ("spotify/artist/4gpHM2bKGPVmvpMQQvbLQT", ["01 - Test Artist - Song.flac"]),
        ):
            with self.subTest(sub=sub):
                artist, album, _ = self._infer(sub, files)
                self.assertNotIn(artist.lower(), banned)
                self.assertNotIn(album.lower(), banned)

    def test_lossy_codec_bracket_tag_is_stripped(self):
        artist, album, year = self._infer(
            "deezer/album/5", ["V/A - Hits (2021) [FLAC 24bit 96kHz]/01 - A - B.flac"]
        )
        self.assertNotIn("FLAC", album)
        self.assertEqual(year, "2021")

    def test_various_artists_uses_the_modal_name(self):
        artist, _, _ = self._infer(
            "deezer/album/6",
            ["01 - Alpha - One.flac", "02 - Beta - Two.flac", "03 - Alpha - Three.flac"],
        )
        self.assertEqual(artist, "Alpha")

    def test_fallback_artist_is_used_when_filenames_have_no_artist(self):
        artist, album, _ = self._infer(
            "qobuz/track/xyz", ["01 - Song Only.flac"], fallback="Bill Evans"
        )
        self.assertEqual(artist, "Bill Evans")
        self.assertEqual(album, "Unknown Album")

    def test_unparseable_filenames_do_not_produce_blank_credits(self):
        artist, album, _ = self._infer("apple/track/7", ["audio_stream.flac"])
        self.assertEqual(artist, "Unknown Artist")
        self.assertNotEqual(album, "")

    def test_no_audio_returns_the_fallback(self):
        workdir = self.stage("tidal/album/blank")
        self.assertEqual(
            pe.infer_release_from_paths([], "Sam", workdir), ("Sam", "Unknown Album", "")
        )

    def test_trailing_year_forms_are_all_stripped(self):
        for folder, expected_album in (
            ("Kid Amo (2024)", "Kid Amo"),
            ("Kid Amo [2024]", "Kid Amo"),
            ("Kid Amo - 2024", "Kid Amo"),
            ("Kid Amo · 2024", "Kid Amo"),
            ("Kid Amo 2024", "Kid Amo 2024"),  # no delimiter: not a year marker
        ):
            with self.subTest(folder=folder):
                _, album, _ = self._infer(
                    f"apple/album/1", [f"Test Artist/{folder}/01 - Song.flac"]
                )
                self.assertEqual(album, expected_album)

    def test_workdir_omitted_is_conservative(self):
        # Without the staging root nothing above the release folder can be
        # trusted, so the artist must come from the filenames.
        workdir = self.stage("qobuz/album/foobaz123", "01 - Test Artist - Song.flac")
        audio, _, _ = pe.discover_release(workdir)
        artist, album, _ = pe.infer_release_from_paths(audio)
        self.assertEqual(artist, "Test Artist")
        self.assertNotIn("foobaz123", album)


class TestTrackParsing(WorkdirCase):
    def test_number_artist_title(self):
        workdir = self.stage("qobuz/album/1", "05 - Bill Evans - My Foolish Heart.flac")
        audio, release_dir, _ = pe.discover_release(workdir)
        (entry,) = pe.tracks_from_paths(audio, release_dir)
        self.assertEqual(entry["TRACK_NUMBER"], "05")
        self.assertEqual(entry["artist_name"], "Bill Evans")
        self.assertEqual(entry["song_name"], "My Foolish Heart")
        self.assertEqual(entry["file_extension"], ".flac")

    def test_dot_separated_number_keeps_the_artist(self):
        # streamrip's track_format is "{tracknumber:02d}. {artist} - {title}";
        # the number shares a segment with the artist, so the prefix has to be
        # stripped rather than the whole segment dropped.
        workdir = self.stage("tidal/album/2", "01. Bill Evans - My Foolish Heart.flac")
        audio, release_dir, _ = pe.discover_release(workdir)
        (entry,) = pe.tracks_from_paths(audio, release_dir)
        self.assertEqual(entry["artist_name"], "Bill Evans")
        self.assertEqual(entry["song_name"], "My Foolish Heart")
        # Padding is preserved; build_release_entries() zfills to two digits
        # anyway, so "01" and "1" both archive as 01.
        self.assertEqual(entry["TRACK_NUMBER"], "01")

    def test_disc_prefix(self):
        workdir = self.stage("apple/album/3", "Disc 02 - 03 - Artist - Title.flac")
        audio, release_dir, _ = pe.discover_release(workdir)
        (entry,) = pe.tracks_from_paths(audio, release_dir)
        self.assertEqual(entry["TRACK_NUMBER"], "03")
        self.assertEqual(entry["artist_name"], "Artist")
        self.assertEqual(entry["song_name"], "Title")

    def test_gamdl_numeric_disc_track_prefix_keeps_artist(self):
        number, artist, title = pe._split_track_stem("1-01 - Artist - Title")
        self.assertEqual((number, artist, title), ("01", "Artist", "Title"))

    def test_multi_disc_tracks_get_unique_sequential_archive_numbers(self):
        workdir = self.stage(
            "apple/album/multi",
            "Artist/Album/1-01 - Artist - Disc One.m4a",
            "Artist/Album/1-02 - Artist - Disc One.m4a",
            "Artist/Album/2-01 - Artist - Disc One.m4a",
            "Artist/Album/2-02 - Artist - Disc Two.m4a",
        )
        audio, release_dir, _ = pe.discover_release(workdir)
        tracks = pe.tracks_from_paths(audio, release_dir)
        self.assertEqual([t["TRACK_NUMBER"] for t in tracks], ["1", "2", "3", "4"])
        from dl_utils.archive_delivery import build_release_entries
        entries = build_release_entries(
            {"cover_data": None, "clean_artist": "Artist", "clean_album_title": "Album", "year": "2024"},
            tracks,
        )
        self.assertEqual(len(entries), 4)
        self.assertEqual(len(set(entries.values())), 4)
        self.assertEqual(
            [Path(entries[t["song_path"]]).name for t in tracks],
            [
                "01 - Artist - Disc One.m4a",
                "02 - Artist - Disc One.m4a",
                "03 - Artist - Disc One.m4a",
                "04 - Artist - Disc Two.m4a",
            ],
        )

    def test_titles_containing_a_dash_survive(self):
        workdir = self.stage("deezer/album/4", "01 - Artist - Top - Bottom.flac")
        audio, release_dir, _ = pe.discover_release(workdir)
        (entry,) = pe.tracks_from_paths(audio, release_dir)
        self.assertEqual(entry["artist_name"], "Artist")
        self.assertEqual(entry["song_name"], "Top - Bottom")

    def test_unnumbered_files_get_their_index(self):
        workdir = self.stage("qobuz/album/5", "zzz.flac", "aaa.flac")
        audio, release_dir, _ = pe.discover_release(workdir)
        entries = pe.tracks_from_paths(audio, release_dir, artist_fallback="Sam")
        numbers = {e["song_name"]: e["TRACK_NUMBER"] for e in entries}
        self.assertEqual(numbers["aaa"], "1")
        self.assertEqual(numbers["zzz"], "2")
        self.assertTrue(all(e["artist_name"] == "Sam" for e in entries))

    def test_every_entry_carries_the_paths_build_release_entries_needs(self):
        workdir = self.stage("tidal/album/6", "01 - A - B.flac")
        audio, release_dir, _ = pe.discover_release(workdir)
        (entry,) = pe.tracks_from_paths(audio, release_dir)
        for key in ("song_path", "download_dir", "song_name", "artist_name",
                    "file_extension", "TRACK_NUMBER", "SNG_ID"):
            self.assertIn(key, entry)
        self.assertEqual(Path(entry["song_path"]).exists(), True)
        self.assertEqual(entry["download_dir"], str(release_dir))


# ---------------------------------------------------------------------------
# Cover handling
# ---------------------------------------------------------------------------
class TestReadCover(WorkdirCase):
    def test_small_jpeg_is_returned_verbatim(self):
        workdir = self.stage("deezer/album/1", "01 - A - B.flac")
        cover = workdir / "cover.jpg"
        cover.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        self.assertEqual(pe.read_cover(str(cover)), cover.read_bytes())

    def test_png_is_reencoded_to_jpeg(self):
        # Telegram will not render a PNG as a photo in every client, and the zip
        # layout specifies cover.jpg, so a lossless PNG becomes a JPEG.
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        workdir = self.stage("qobuz/album/2", "01 - A - B.flac")
        cover = workdir / "cover.png"
        Image.new("RGB", (60, 60), (200, 30, 30)).save(cover)
        data = pe.read_cover(str(cover))
        self.assertIsNotNone(data)
        self.assertEqual(data[:3], b"\xff\xd8\xff")

    def test_oversized_cover_is_shrunk_below_the_cap(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        workdir = self.stage("apple/album/3", "01 - A - B.flac")
        cover = workdir / "cover.jpg"
        # Noise does not compress, so this reliably exceeds a small cap.
        import random

        random.seed(7)
        noise = bytes(random.randrange(256) for _ in range(300 * 300 * 3))
        img = Image.frombytes("RGB", (300, 300), noise)
        img.save(cover, format="JPEG", quality=100)
        original = cover.stat().st_size
        self.assertGreater(original, 60_000)
        # The cap has to stay above the smallest quality rung's output or the
        # function legitimately returns None; 60 KB is comfortably inside range.
        data = pe.read_cover(str(cover), max_bytes=60_000)
        self.assertIsNotNone(data)
        self.assertLessEqual(len(data), 60_000)
        self.assertLess(len(data), original)
        self.assertEqual(data[:3], b"\xff\xd8\xff")

    def test_a_cover_that_cannot_be_compressed_under_the_cap_is_dropped(self):
        # Better no cover than an archive Telegram refuses to send.
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        import random

        workdir = self.stage("apple/album/4", "01 - A - B.flac")
        cover = workdir / "cover.jpg"
        random.seed(11)
        noise = bytes(random.randrange(256) for _ in range(200 * 200 * 3))
        Image.frombytes("RGB", (200, 200), noise).save(cover, format="JPEG", quality=100)
        self.assertIsNone(pe.read_cover(str(cover), max_bytes=512))

    def test_missing_path_and_none_are_both_none(self):
        self.assertIsNone(pe.read_cover(None))
        self.assertIsNone(pe.read_cover(str(self.root / "absent.jpg")))

    def test_corrupt_image_does_not_raise(self):
        workdir = self.stage("tidal/album/5", "01 - A - B.flac")
        cover = workdir / "cover.jpg"
        cover.write_bytes(b"definitely not a jpeg" * 100)
        self.assertIsNone(pe.read_cover(str(cover), max_bytes=10))


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------
class TestSubprocess(WorkdirCase):
    def test_engine_env_sets_ca_bundle_and_dumb_terminal(self):
        env = pe._engine_env()
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertEqual(env["TERM"], "dumb")
        self.assertEqual(env["NO_COLOR"], "1")
        # A child that verifies TLS needs the bundle; the system store is not in
        # the alpine image by default.
        self.assertTrue(env.get("SSL_CERT_FILE"))
        self.assertTrue(env.get("REQUESTS_CA_BUNDLE"))

    def test_dotify_home_and_xdg_state_are_isolated_per_invocation(self):
        workdir = self.stage("spotify/track/isolation")
        shared_home = os.environ.get("HOME")
        first = pe._engine_env(workdir)
        second = pe._engine_env(workdir)

        for env in (first, second):
            for key, leaf in (
                ("HOME", "home"),
                ("XDG_CONFIG_HOME", "config"),
                ("XDG_CACHE_HOME", "cache"),
            ):
                path = Path(env[key])
                self.assertTrue(path.is_relative_to(workdir))
                self.assertEqual(path.name, leaf)
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertNotEqual(env["HOME"], shared_home)
        self.assertNotEqual(first["HOME"], second["HOME"])
        self.assertNotEqual(first["XDG_CONFIG_HOME"], second["XDG_CONFIG_HOME"])
        self.assertNotEqual(first["XDG_CACHE_HOME"], second["XDG_CACHE_HOME"])

    def test_returncode_and_output_are_captured(self):
        workdir = self.stage("qobuz/track/1")
        rc, out = asyncio.run(pe._run_subprocess(["sh", "-c", "echo hi"], workdir))
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "hi")

    def test_stderr_is_merged_into_the_log(self):
        workdir = self.stage("qobuz/track/2")
        rc, out = asyncio.run(
            pe._run_subprocess(["sh", "-c", "echo oops >&2; exit 3"], workdir)
        )
        self.assertEqual(rc, 3)
        self.assertIn("oops", out)

    def test_log_tail_is_bounded(self):
        # A chatty engine must not fill the bot's log or the error text sent to
        # the user; only the last 25 lines are kept.
        workdir = self.stage("qobuz/track/3")
        rc, out = asyncio.run(
            pe._run_subprocess(["sh", "-c", "seq 1 500"], workdir)
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 25)
        self.assertIn("500", out.splitlines()[-1])

    def test_timeout_kills_the_child(self):
        workdir = self.stage("qobuz/track/4")
        with self.assertRaises(EngineError) as ctx:
            asyncio.run(
                pe._run_subprocess(["sh", "-c", "sleep 30"], workdir, timeout=1)
            )
        self.assertIn("timed out", str(ctx.exception))

    @unittest.skipUnless(hasattr(os, "killpg"), "no process groups on this platform")
    def test_timeout_bounds_wall_clock_even_when_a_grandchild_holds_the_pipe(self):
        # The engines are wrappers: `rip` forks ffmpeg, gamdl forks AtomicParsley.
        # Killing only the group leader leaves the grandchild alive with the
        # inherited stdout descriptor, so communicate() keeps blocking for the
        # grandchild's full lifetime and ENGINE_TIMEOUT_SECONDS bounds nothing.
        # A hung engine would then hold a download slot hostage indefinitely.
        workdir = self.stage("qobuz/track/4b")
        marker = f"deadmoon-proc-{os.getpid()}"
        started = time.perf_counter()
        with self.assertRaises(EngineError):
            asyncio.run(
                pe._run_subprocess(
                    ["sh", "-c", f"sleep 60 # {marker} & wait"], workdir, timeout=1
                )
            )
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 5, f"timeout took {elapsed:.1f}s, not the child tree")

        # Give the kernel a beat to reap, then confirm no orphan survives.
        time.sleep(0.3)
        survivors = subprocess.run(
            ["pgrep", "-f", marker], capture_output=True, text=True
        ).stdout.split()
        self.assertEqual(survivors, [], "grandchild outlived the timeout")

    @unittest.skipUnless(hasattr(os, "killpg"), "no process groups on this platform")
    def test_kill_process_tree_takes_the_whole_group(self):
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 60 # deadmoon-killpg-test & wait"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
        )
        time.sleep(0.3)
        pe._kill_process_tree(proc)
        proc.wait(timeout=5)
        time.sleep(0.3)
        survivors = subprocess.run(
            ["pgrep", "-f", "deadmoon-killpg-test"], capture_output=True, text=True
        ).stdout.split()
        self.assertEqual(survivors, [])

    def test_kill_process_tree_survives_an_already_dead_child(self):
        # A process that exited between the timeout and the kill must not raise:
        # this runs on the error path, and an exception here would mask the real
        # EngineError the handler needs to report to the user.
        proc = subprocess.Popen(["true"])
        proc.wait()
        time.sleep(0.2)
        pe._kill_process_tree(proc)

    def test_kill_process_tree_falls_back_when_there_is_no_process_group(self):
        proc = subprocess.Popen(["sleep", "60"])
        with mock.patch.object(pe.os, "killpg", side_effect=OSError("nope")):
            pe._kill_process_tree(proc)
        proc.wait(timeout=5)

    def test_missing_binary_is_an_engine_error_not_a_traceback(self):
        workdir = self.stage("qobuz/track/5")
        with self.assertRaises((EngineError, FileNotFoundError)):
            asyncio.run(pe._run_subprocess(["definitely-not-a-real-binary"], workdir))

    def test_argv_is_not_logged_in_full(self):
        # The command line carries credential paths and tokens.
        workdir = self.stage("qobuz/track/6")
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            asyncio.run(
                pe._run_subprocess(
                    ["sh", "-c", "exit 0", "--secret-token", "sup3r-s3cret"], workdir
                )
            )
        self.assertNotIn("sup3r-s3cret", buf.getvalue())


class TestWhich(WorkdirCase):
    def test_finds_a_real_binary(self):
        self.assertIsNotNone(pe._which("sh"))

    def test_returns_none_for_a_missing_binary(self):
        self.assertIsNone(pe._which("definitely-not-a-real-binary-xyz"))

    def test_first_match_wins_across_aliases(self):
        self.assertEqual(pe._which("sh", "bash"), pe._which("sh"))

    def test_project_local_bin_directory_is_consulted(self):
        # requirements.txt installs the heavyweight engines with --target, so
        # their console scripts are not on PATH.
        local = Path(pe.__file__).resolve().parent.parent / "bin"
        local.mkdir(exist_ok=True)
        script = local / "deadmoon-fake-engine"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        try:
            self.assertEqual(pe._which("deadmoon-fake-engine"), str(script))
        finally:
            script.unlink()


# ---------------------------------------------------------------------------
# Apple cookies synthesis
# ---------------------------------------------------------------------------
class TestAppleCookies(WorkdirCase):
    def test_existing_cookies_file_is_used_as_is(self):
        cookies = self.root / "real-cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n")
        self.set_env(APPLE_COOKIES_PATH=str(cookies))
        workdir = self.stage("apple/album/1", "01 - A - B.flac")
        self.assertEqual(asyncio.run(pe._apple_cookies_file(workdir)), str(cookies))

    def test_missing_cookies_file_without_token_is_a_clear_error(self):
        self.set_env(APPLE_COOKIES_PATH=str(self.root / "gone.txt"))
        workdir = self.stage("apple/album/2", "01 - A - B.flac")
        with self.assertRaises(EngineError) as ctx:
            asyncio.run(pe._apple_cookies_file(workdir))
        self.assertIn("APPLE_COOKIES_PATH", str(ctx.exception))
        self.assertIn("APPLE_MEDIA_USER_TOKEN", str(ctx.exception))

    def test_prefilled_missing_cookies_file_falls_back_to_media_token(self):
        self.set_env(
            APPLE_COOKIES_PATH="/usr/src/app/local_resources/apple_cookies.txt",
            APPLE_MEDIA_USER_TOKEN="mut-prefilled-path",
        )
        self.assertTrue(pcfg.has_apple_credentials())
        workdir = self.stage("apple/album/prefilled", "01 - A - B.flac")
        path = Path(asyncio.run(pe._apple_cookies_file(workdir)))
        self.assertEqual(path.parent, workdir)
        self.assertIn("media-user-token\tmut-prefilled-path", path.read_text())

    def test_unchanged_example_cookies_fall_back_to_media_token(self):
        example = Path(pe.__file__).resolve().parent.parent / "local_resources" / "apple_cookies.txt.example"
        if not example.exists():
            self.skipTest("no Apple cookie example")
        cookies = self.root / "apple_cookies.txt"
        cookies.write_bytes(example.read_bytes())
        self.set_env(APPLE_COOKIES_PATH=str(cookies), APPLE_MEDIA_USER_TOKEN="mut-example")
        workdir = self.stage("apple/album/example", "01 - A - B.flac")
        generated = Path(asyncio.run(pe._apple_cookies_file(workdir)))
        self.assertIn("media-user-token\tmut-example", generated.read_text())

    def test_media_user_token_becomes_a_netscape_file(self):
        self.set_env(APPLE_MEDIA_USER_TOKEN="mut-abc123")
        workdir = self.stage("apple/album/3", "01 - A - B.flac")
        path = Path(asyncio.run(pe._apple_cookies_file(workdir)))
        self.assertTrue(path.exists())
        body = path.read_text()
        self.assertIn("# Netscape HTTP Cookie File", body)
        self.assertIn("media-user-token\tmut-abc123", body)
        self.assertIn(".apple.com", body)
        # The token is a live account credential; it must not be world-readable.
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        # It lives in the request's temp dir, so it dies with the request.
        self.assertIn(str(workdir), str(path))

    def test_no_apple_credential_explains_what_to_set(self):
        workdir = self.stage("apple/album/4", "01 - A - B.flac")
        with self.assertRaises(EngineError) as ctx:
            asyncio.run(pe._apple_cookies_file(workdir))
        message = str(ctx.exception)
        self.assertIn("APPLE_COOKIES_PATH", message)
        self.assertIn("APPLE_MEDIA_USER_TOKEN", message)

    def test_cookies_path_takes_precedence_over_the_token(self):
        cookies = self.root / "both.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n")
        self.set_env(APPLE_COOKIES_PATH=str(cookies), APPLE_MEDIA_USER_TOKEN="mut-x")
        workdir = self.stage("apple/album/5", "01 - A - B.flac")
        self.assertEqual(asyncio.run(pe._apple_cookies_file(workdir)), str(cookies))
        self.assertFalse((workdir / "cookies.txt").exists())


# ---------------------------------------------------------------------------
# Dispatcher: backend choice and fallback
# ---------------------------------------------------------------------------
def _engine_result(platform, url, workdir, n=1):
    audio, release_dir, _ = pe.discover_release(workdir)
    artist, album, year = pe.infer_release_from_paths(audio, workdir=workdir)
    return EngineResult(
        metadata=ReleaseMetadata(artist=artist, title=album, album_title=album, year=year),
        tracks=pe.tracks_from_paths(audio, release_dir, artist_fallback=artist),
        mode="token",
        platform=platform,
        provider="fake",
        workdir=str(workdir),
    )


class TestDownloadReleaseDispatch(WorkdirCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.progress = []

    async def progress_cb(self, text):
        self.progress.append(text)

    def fake_direct(self, platform, name, exc=None, leaves_files=True):
        async def impl(url, kind, workdir):
            self.calls.append(name)
            if leaves_files:
                (Path(workdir) / f"01 - {name} - Song.flac").write_bytes(b"fLaC\x00\x00")
            if exc:
                raise exc
            return _engine_result(platform, url, Path(workdir))

        return impl

    def fake_fallback(self, platform, exc=None):
        async def impl(plat, url, kind, workdir, progress_cb=None):
            self.calls.append("deezer")
            self.assertEqual(plat, platform)
            if progress_cb:
                await progress_cb("fallback progress")
            if exc:
                raise exc
            (Path(workdir) / "01 - Deezer - Song.flac").write_bytes(b"fLaC\x00\x00")
            return _engine_result(platform, url, Path(workdir))

        return impl

    def run_dispatch(self, platform, direct=None, fallback=None, credential=True):
        engines = {platform: direct} if direct else {}
        patches = [
            mock.patch.dict(pe._TOKEN_ENGINES, engines, clear=True),
            mock.patch.object(pcfg, "has_credentials", return_value=credential),
        ]
        if fallback:
            patches.append(mock.patch.object(pe, "download_via_deezer_fallback", fallback))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return asyncio.run(pe.download_release(
            platform, f"https://example/{platform}/album/1", "album",
            self.stage(f"{platform}/album/dispatch"), progress_cb=self.progress_cb,
        ))

    def test_direct_engine_runs_when_credentials_exist(self):
        result = self.run_dispatch(
            pcfg.QOBUZ, self.fake_direct(pcfg.QOBUZ, "streamrip")
        )
        self.assertEqual(self.calls, ["streamrip"])
        self.assertEqual(result.provider, "fake")
        self.assertIn("via streamrip", self.progress[0])

    def test_missing_credentials_fail_before_any_engine_by_default(self):
        async def forbidden(*args, **kwargs):
            self.fail("an engine ran without credentials while fallback was disabled")

        with self.assertRaises(EngineError) as ctx:
            self.run_dispatch(pcfg.SPOTIFY, forbidden, forbidden, credential=False)
        self.assertEqual(self.calls, [])
        self.assertIn("SPOTIFY_COOKIES_PATH", str(ctx.exception))
        self.assertIn("direct-only", str(ctx.exception))

    def test_spotify_never_uses_deezer_even_when_global_fallback_is_enabled(self):
        self.set_env(FALLBACK_ENABLED="1")

        async def forbidden(*args, **kwargs):
            self.fail("Spotify attempted a cross-platform engine")

        with mock.patch.dict(pe._TOKEN_ENGINES, {pcfg.SPOTIFY: forbidden}, clear=True), mock.patch.object(
            pe, "download_via_deezer_fallback", forbidden
        ):
            with self.assertRaises(EngineError) as ctx:
                asyncio.run(pe.download_release(
                    pcfg.SPOTIFY, "https://open.spotify.com/album/1", "album",
                    self.stage("spotify/album/direct-only"),
                ))
        message = str(ctx.exception)
        self.assertIn("SPOTIFY_COOKIES_PATH", message)
        self.assertIn("SPOTIFY_WVD_PATH", message)
        self.assertIn("direct-only", message)

    def test_missing_credentials_use_deezer_when_enabled(self):
        self.set_env(FALLBACK_ENABLED="1")
        result = self.run_dispatch(
            pcfg.TIDAL, fallback=self.fake_fallback(pcfg.TIDAL), credential=False
        )
        self.assertEqual(self.calls, ["deezer"])
        self.assertEqual(result.tracks[0]["artist_name"], "Deezer")
        self.assertEqual(
            self.progress[0],
            "⚠️ Tidal credentials not configured. Falling back to Deezer ARL...",
        )

    def test_direct_engine_error_is_reraised_unchanged_by_default(self):
        error = EngineError("401 unauthorized")

        async def forbidden(*args, **kwargs):
            self.fail("fallback ran while FALLBACK_ENABLED=0")

        with self.assertRaises(EngineError) as ctx:
            self.run_dispatch(
                pcfg.TIDAL, self.fake_direct(pcfg.TIDAL, "streamrip", exc=error), forbidden
            )
        self.assertIs(ctx.exception, error)
        self.assertEqual(self.calls, ["streamrip"])

    def test_direct_failure_clears_partials_before_deezer_fallback(self):
        self.set_env(FALLBACK_ENABLED="1")
        seen = {}

        async def fallback(platform, url, kind, workdir, progress_cb=None):
            seen["contents"] = list(Path(workdir).iterdir())
            (Path(workdir) / "01 - Deezer - Song.flac").write_bytes(b"fLaC\x00\x00")
            return _engine_result(platform, url, Path(workdir))

        direct = self.fake_direct(pcfg.TIDAL, "streamrip", exc=EngineError("outage"))
        self.run_dispatch(pcfg.TIDAL, direct, fallback)
        self.assertEqual(seen["contents"], [])
        self.assertIn("outage", self.progress[-1])
        self.assertIn("Deezer ARL", self.progress[-1])

    def test_unconfigured_direct_engine_is_an_error(self):
        with self.assertRaisesRegex(EngineError, "direct engine is not configured"):
            self.run_dispatch(pcfg.QOBUZ)


class TestDotifyArguments(WorkdirCase):
    def setUp(self):
        super().setUp()
        self.workdir = self.stage("spotify/album/cli")
        self.cookies = self.root / "source-cookies.txt"
        self.cookies.write_text(
            "# Netscape HTTP Cookie File\n"
            ".spotify.com\tTRUE\t/\tTRUE\t0\tsp_dc\tweb-session\n"
        )
        self.wvd = self.root / "device.wvd"
        self.wvd.write_bytes(b"provisioned-test-device")
        self.set_env(
            SPOTIFY_COOKIES_PATH=str(self.cookies),
            SPOTIFY_WVD_PATH=str(self.wvd),
            SPOTIFY_CREDENTIALS_PATH="",
            SPOTIFY_AUDIO_QUALITY="",
            ENABLE_FLAC="1",
        )

    @staticmethod
    def metadata(playable=(True, True, True)):
        return {
            "name": "Test Album",
            "release_date": "2024-01-02",
            "artists": [{"name": "Test Artist"}],
            "tracks": {"items": [
                {
                    "id": f"track{number}",
                    "name": f"Song {number}",
                    "track_number": number,
                    "artists": [{"name": "Test Artist"}],
                    "is_playable": available,
                }
                for number, available in enumerate(playable, 1)
            ]},
        }

    def run_engine(self, fake_run, playable=(True, True, True), sleep=None, progress_cb=None):
        metadata = self.metadata(playable)
        sleep = sleep or mock.AsyncMock()
        with mock.patch.object(pe, "_which", return_value="/usr/bin/dotify"), mock.patch.object(
            pe, "_spotify_release_metadata", new=mock.AsyncMock(return_value=metadata)
        ), mock.patch.object(pe, "_run_subprocess", fake_run), mock.patch.object(
            pe.asyncio, "sleep", sleep
        ):
            return asyncio.run(pe.download_via_dotify(
                "https://open.spotify.com/album/123", "album", self.workdir,
                progress_cb=progress_cb,
            ))

    def test_download_stages_private_cookies_and_preserves_native_audio(self):
        captured = []

        async def fake_run(cmd, cwd):
            captured.append(cmd)
            staged = Path(cmd[cmd.index("--cookies-path") + 1])
            self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
            release = Path(cwd) / "Test Artist - Test Album"
            release.mkdir(exist_ok=True)
            number = cmd[-1].rsplit("track", 1)[-1]
            (release / f"{int(number):02d} - Test Artist - Song {number}.m4a").write_bytes(b"audio")
            return 0, ""

        result = self.run_engine(fake_run)
        self.assertEqual(len(captured), 3)
        cmd = captured[0]
        self.assertEqual(cmd[0], "/usr/bin/dotify")
        self.assertEqual(cmd[cmd.index("--session-type") + 1], "web")
        self.assertEqual(cmd[cmd.index("--audio-remux-mode") + 1], "ffmpeg")
        self.assertEqual(
            cmd[cmd.index("--audio-quality") + 1],
            "aac-high,aac-medium",
        )
        self.assertEqual(cmd[cmd.index("--wvd-path") + 1], str(self.wvd))
        self.assertNotIn("--download-format", cmd)
        self.assertEqual(result.metadata.artist, "Test Artist")
        self.assertEqual(result.metadata.album_title, "Test Album")
        self.assertEqual(result.metadata.year, "2024")
        self.assertEqual(len(result.metadata.tracks_api_data), 3)
        self.assertTrue(all(track["file_extension"] == ".m4a" for track in result.tracks))
        self.assertTrue(all(track["SNG_ID"] for track in result.tracks))

    def test_widevine_403_retries_track_with_aac_and_reuses_it_for_release(self):
        self.set_env(
            SPOTIFY_AUDIO_QUALITY="flac-mp4-24,flac-mp4,vorbis-high,aac-high,aac-medium"
        )
        sleep = mock.AsyncMock()
        calls = []

        async def fake_run(cmd, cwd):
            quality = cmd[cmd.index("--audio-quality") + 1]
            calls.append((cmd[-1], quality))
            if len(calls) == 1:
                return 1, (
                    "[API_REQUEST_FAILED] Widevine license request failed with "
                    "status code 403: Access denied. Your account may not have "
                    "the required permissions."
                )
            release = Path(cwd) / "Test Artist - Test Album"
            release.mkdir(exist_ok=True)
            number = int(cmd[-1].rsplit("track", 1)[-1])
            (release / f"{number:02d} - Test Artist - Song {number}.m4a").write_bytes(
                b"native-aac"
            )
            return 0, ""

        result = self.run_engine(fake_run, sleep=sleep)
        aac = "aac-high,aac-medium"
        self.assertEqual(
            calls,
            [
                ("https://open.spotify.com/track/track1", "flac-mp4-24,flac-mp4,vorbis-high,aac-high,aac-medium"),
                ("https://open.spotify.com/track/track1", aac),
                ("https://open.spotify.com/track/track2", aac),
                ("https://open.spotify.com/track/track3", aac),
            ],
        )
        self.assertEqual(len(result.tracks), 3)
        self.assertEqual(sleep.await_args_list, [mock.call(3.0), mock.call(4.0), mock.call(4.0)])

    def test_http_429_cools_down_and_retries_track_once(self):
        sleep = mock.AsyncMock()
        progress = mock.AsyncMock()
        calls = []

        async def fake_run(cmd, cwd):
            calls.append(cmd[-1])
            if len(calls) == 1:
                return 1, "Widevine license request failed with status code 429"
            release = Path(cwd) / "Test Artist - Test Album"
            release.mkdir(exist_ok=True)
            (release / "01 - Test Artist - Song 1.m4a").write_bytes(b"native-aac")
            return 0, ""

        result = self.run_engine(
            fake_run, playable=(True,), sleep=sleep, progress_cb=progress
        )
        self.assertEqual(calls, ["https://open.spotify.com/track/track1"] * 2)
        sleep.assert_awaited_once_with(pe.SPOTIFY_RATE_LIMIT_COOLDOWN_SECONDS)
        progress.assert_awaited_once()
        message = progress.await_args.args[0]
        self.assertIn("Spotify rate-limited", message)
        self.assertIn("62s", message)
        self.assertEqual(len(result.tracks), 1)

    def test_unplayable_and_failed_tracks_are_reported_without_album_failure(self):
        calls = []

        async def fake_run(cmd, cwd):
            calls.append(cmd[-1])
            if cmd[-1].endswith("track3"):
                return 1, "license unavailable"
            release = Path(cwd) / "Test Artist - Test Album"
            release.mkdir(exist_ok=True)
            (release / "01 - Test Artist - Song 1.m4a").write_bytes(b"audio")
            return 0, ""

        result = self.run_engine(fake_run, playable=(True, False, True))
        self.assertEqual(len(calls), 2)
        self.assertFalse(any(call.endswith("track2") for call in calls))
        self.assertEqual(len(result.tracks), 1)
        from dl_utils.archive_delivery import summarize_track_issues
        _, failures = summarize_track_issues(result.tracks, result.metadata.tracks_api_data)
        self.assertEqual(len(failures), 2)
        self.assertIn("Track 02 - Test Artist - Song 2 was not available", failures[0])
        self.assertIn("Track 03 - Test Artist - Song 3 was not available", failures[1])

    def test_partial_spotify_album_uses_canonical_archive_and_issue_summary(self):
        from dl_utils import archive_delivery, metadata_bridge

        async def fake_run(cmd, cwd):
            release = Path(cwd) / "Test Artist - Test Album"
            release.mkdir(exist_ok=True)
            (release / "01 - Test Artist - Song 1.ogg").write_bytes(b"native-ogg")
            return 0, ""

        result = self.run_engine(fake_run, playable=(True, False))
        metadata = metadata_bridge.merge_info(result.metadata, None)
        entries = archive_delivery.build_release_entries(metadata, result.tracks)
        destinations = sorted(entries.values())
        self.assertEqual(
            destinations,
            ["Test Artist - Test Album [2024]/01 - Test Artist - Song 1.ogg"],
        )
        caption = archive_delivery.append_issue_summary(
            "Album", result.tracks, result.metadata.tracks_api_data
        )
        self.assertIn("Failures: Track 02 - Test Artist - Song 2 was not available", caption)

    def test_no_playable_or_downloaded_tracks_is_an_error(self):
        async def forbidden(cmd, cwd):
            self.fail("an unplayable track was sent to dotify-cli")

        with self.assertRaisesRegex(EngineError, "no playable tracks"):
            self.run_engine(forbidden, playable=(False, False))

        async def failed(cmd, cwd):
            return 1, "authentication failed"

        with self.assertRaisesRegex(EngineError, "could not download any tracks"):
            self.run_engine(failed)

    @unittest.skipUnless(shutil.which("dotify"), "dotify is not installed")
    def test_generated_command_is_accepted_by_real_dotify_parser(self):
        import os
        import subprocess

        command = pe._dotify_command(
            pe._which("dotify") or "dotify",
            self.cookies,
            self.wvd,
            self.workdir,
            "https://open.spotify.com/track/3PjjsUGnolHBjJuQqrp82y",
        )
        completed = subprocess.run(
            command + ["--dry-run"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=45,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
        output = completed.stdout + completed.stderr
        self.assertNotIn("No such option", output)
        self.assertNotIn("Invalid value", output)
        self.assertNotIn("unexpected extra argument", output)

    def test_single_track_keeps_song_title_and_parent_album_context(self):
        metadata = self.metadata((True,))["tracks"]["items"][0]
        metadata["album"] = {
            "id": "parent-album",
            "name": "Test Album",
            "release_date": "2024-01-02",
            "artists": [{"name": "Test Artist"}],
        }

        async def fake_run(cmd, cwd):
            release = Path(cwd) / "Test Artist - Test Album"
            release.mkdir(exist_ok=True)
            (release / "01 - Test Artist - Song 1.m4a").write_bytes(b"audio")
            return 0, ""

        with mock.patch.object(pe, "_which", return_value="/usr/bin/dotify"), mock.patch.object(
            pe, "_spotify_release_metadata", new=mock.AsyncMock(return_value=metadata)
        ), mock.patch.object(pe, "_run_subprocess", fake_run):
            result = asyncio.run(pe.download_via_dotify(
                "https://open.spotify.com/track/track1", "track", self.workdir
            ))

        self.assertEqual(result.metadata.title, "Song 1")
        self.assertEqual(result.metadata.album_title, "Test Album")
        self.assertEqual(
            result.metadata.album_link,
            "https://open.spotify.com/album/parent-album",
        )

    def test_backward_compatible_alias_uses_dotify(self):
        self.assertIs(pe.download_via_zotify, pe.download_via_dotify)


class TestGamdlArguments(WorkdirCase):
    def setUp(self):
        super().setUp()
        self.workdir = self.stage("apple/album/cli")

    def command(self):
        return pe._gamdl_command(
            "/usr/bin/gamdl", "/resources/apple_cookies.txt",
            "https://music.apple.com/us/album/test/123", self.workdir,
        )

    def test_lossless_defaults_to_alac_with_aac_fallback(self):
        self.set_env(ENABLE_FLAC="1")
        cmd = self.command()
        index = cmd.index("--song-codec-priority")
        self.assertEqual(cmd[index + 1], "alac,aac-web")

    def test_lossy_defaults_to_aac_web(self):
        self.set_env(ENABLE_FLAC="0")
        cmd = self.command()
        self.assertEqual(cmd[cmd.index("--song-codec-priority") + 1], "aac-web")

    def test_explicit_codec_priority_is_normalized(self):
        self.set_env(APPLE_CODEC_PRIORITY=" AAC-WEB, alac ")
        cmd = self.command()
        self.assertEqual(cmd[cmd.index("--song-codec-priority") + 1], "aac-web,alac")

    def test_invalid_explicit_codec_is_rejected_before_subprocess(self):
        self.set_env(APPLE_CODEC_PRIORITY="alac-flac-web,aac-web")
        with self.assertRaisesRegex(EngineError, "APPLE_CODEC_PRIORITY"):
            self.command()

    def test_download_function_passes_generated_arguments_to_subprocess(self):
        self.set_env(ENABLE_FLAC="1", APPLE_MEDIA_USER_TOKEN="test-token")
        captured = {}

        async def fake_run(cmd, cwd):
            captured["cmd"] = cmd
            (Path(cwd) / "01 - Artist - Song.m4a").write_bytes(b"audio")
            return 0, ""

        with mock.patch.object(pe, "_which", return_value="/usr/bin/gamdl"), mock.patch.object(
            pe, "_run_subprocess", fake_run
        ):
            result = asyncio.run(pe.download_via_gamdl(
                "https://music.apple.com/us/album/test/123", "album", self.workdir
            ))
        self.assertEqual(result.provider, "gamdl")
        self.assertEqual(
            captured["cmd"][captured["cmd"].index("--song-codec-priority") + 1],
            "alac,aac-web",
        )

    @unittest.skipUnless(_module_available("gamdl"), "gamdl is not installed")
    def test_real_gamdl_3_9_1_parser_accepts_all_default_arguments(self):
        from gamdl import __version__ as gamdl_version
        from gamdl.cli.cli import main
        from gamdl.interface.enums import SongCodec

        self.assertEqual(gamdl_version, "3.9.1")
        self.assertEqual(pe.GAMDL_SONG_CODECS, {codec.value for codec in SongCodec})
        for flac, expected in (("1", [SongCodec.ALAC, SongCodec.AAC_WEB]), ("0", [SongCodec.AAC_WEB])):
            with self.subTest(flac=flac):
                self.set_env(ENABLE_FLAC=flac)
                os.environ.pop("APPLE_CODEC_PRIORITY", None)
                context = main.make_context(
                    "gamdl", self.command()[1:], resilient_parsing=True
                )
                self.assertEqual(context.params["song_codec_piority"], expected)
                self.assertEqual(context.params["urls"], ("https://music.apple.com/us/album/test/123",))
                context.close()


class TestTokenEngineSignatures(WorkdirCase):
    """_TOKEN_ENGINES entries must all be callable as engine(url, kind, workdir).

    download_release() invokes every direct engine with that 3-argument shape.
    download_via_streamrip() takes platform first, so its two entries are bound
    with functools.partial. If that binding is ever dropped the call raises
    TypeError — and because it happens inside the try/except in download_release,
    the TypeError is swallowed into token_error and the request silently degrades
    to Deezer. The signature test catches that regression before a request does.

    These tests deliberately do NOT replace _TOKEN_ENGINES: they exercise the
    real table, which is the only way a signature drift gets caught.
    """

    def test_every_entry_accepts_three_positional_arguments(self):
        # The cheapest honest check: bind the real call signature download_release
        # uses. A dropped functools.partial makes this raise TypeError here, in
        # a test, instead of silently degrading to Deezer in production.
        for platform, engine in pe._TOKEN_ENGINES.items():
            with self.subTest(platform=platform):
                try:
                    inspect.signature(engine).bind("url", "album", Path("/tmp"))
                except TypeError as e:
                    self.fail(f"{platform} engine is not (url, kind, workdir): {e}")

    def test_streamrip_entries_have_platform_prebound(self):
        # The partial must carry the right platform, or Qobuz credentials get
        # written into the Tidal section of the generated config.
        self.assertIsInstance(pe._TOKEN_ENGINES[pcfg.QOBUZ], functools.partial)
        self.assertIsInstance(pe._TOKEN_ENGINES[pcfg.TIDAL], functools.partial)
        self.assertEqual(pe._TOKEN_ENGINES[pcfg.QOBUZ].args, (pcfg.QOBUZ,))
        self.assertEqual(pe._TOKEN_ENGINES[pcfg.TIDAL].args, (pcfg.TIDAL,))
        self.assertIs(pe._TOKEN_ENGINES[pcfg.QOBUZ].func, pe.download_via_streamrip)

    def test_direct_dispatch_uses_the_real_streamrip_engine_for_qobuz(self):
        """Full token-mode dispatch through the untouched _TOKEN_ENGINES table."""
        seen = {"config_platforms": []}

        async def fake_run(cmd, cwd, *args, **kwargs):
            seen["cmd"] = [str(c) for c in cmd]
            seen["cwd"] = Path(cwd)
            # Emulate streamrip's single-folder layout.
            out = Path(cwd) / "Test Artist - Kid Amo (2024)"
            out.mkdir(parents=True, exist_ok=True)
            (out / "01. Test Artist - Kid Amo.flac").write_bytes(b"fLaC\x00\x00")
            return 0, ""

        def record_config(platform, wd):
            seen["config_platforms"].append(platform)
            return Path(wd) / "cfg.toml"

        workdir = self.stage("qobuz/album/sig")
        self.set_env(
            QOBUZ_USER_ID="12345",
            QOBUZ_APP_ID="9999",
            QOBUZ_USER_AUTH_TOKEN="abcdef",
        )
        with mock.patch.object(pe, "_run_subprocess", fake_run), mock.patch.object(
            pe, "_streamrip_config", record_config
        ):
            result = asyncio.run(
                pe.download_release(pcfg.QOBUZ, "https://open.qobuz.com/album/1", "album", workdir)
            )
        self.assertEqual(seen["config_platforms"], [pcfg.QOBUZ])
        self.assertEqual(result.platform, pcfg.QOBUZ)
        self.assertEqual(result.mode, "direct")
        self.assertEqual(result.provider, "streamrip")
        self.assertEqual(result.metadata.artist, "Test Artist")
        self.assertEqual(result.metadata.album_title, "Kid Amo")
        self.assertEqual(result.metadata.year, "2024")
        self.assertIn("url", seen["cmd"])

    def test_direct_dispatch_uses_the_real_streamrip_engine_for_tidal(self):
        seen = {"config_platforms": []}

        async def fake_run(cmd, cwd, *args, **kwargs):
            seen["cmd"] = [str(c) for c in cmd]
            out = Path(cwd) / "Test Artist - Blurred Lines (2013)"
            out.mkdir(parents=True, exist_ok=True)
            (out / "01. Test Artist - Blurred Lines.flac").write_bytes(b"fLaC\x00\x00")
            return 0, ""

        def record_config(platform, wd):
            seen["config_platforms"].append(platform)
            return Path(wd) / "cfg.toml"

        workdir = self.stage("tidal/album/sig")
        self.set_env(
            TIDAL_USER_ID="777",
            TIDAL_ACCESS_TOKEN="at",
            TIDAL_REFRESH_TOKEN="rt",
        )
        with mock.patch.object(pe, "_run_subprocess", fake_run), mock.patch.object(
            pe, "_streamrip_config", record_config
        ):
            result = asyncio.run(
                pe.download_release(pcfg.TIDAL, "https://tidal.com/album/2", "album", workdir)
            )
        # The bound platform is what selects the credential section of the
        # generated config, so it is the assertion that matters most here.
        self.assertEqual(seen["config_platforms"], [pcfg.TIDAL])
        self.assertEqual(result.provider, "streamrip")
        self.assertEqual(result.metadata.album_title, "Blurred Lines")
        self.assertEqual(result.metadata.year, "2013")


class TestClearPartial(WorkdirCase):
    def test_audio_and_cover_are_removed_but_config_survives(self):
        # The generated streamrip config must survive: the fallback engine may
        # still need it, and it holds this request's credentials.
        workdir = self.stage(
            "qobuz/album/1",
            "01 - A - B.flac",
            "02 - A - C.mp3",
            "cover.jpg",
            "spotify_credentials.json",
            "streamrip_config.toml",
            "log.txt",
        )
        pe._clear_partial(workdir)
        remaining = sorted(p.name for p in workdir.iterdir())
        self.assertEqual(remaining, ["log.txt", "streamrip_config.toml"])

    def test_nested_release_folders_are_removed_whole(self):
        workdir = self.stage("tidal/album/2", "Test Artist/Album (2020)/01 - A - B.flac")
        pe._clear_partial(workdir)
        self.assertEqual(list(workdir.iterdir()), [])

    def test_absent_workdir_does_not_raise(self):
        pe._clear_partial(self.root / "never" / "created")


# ---------------------------------------------------------------------------
# Artist discography
# ---------------------------------------------------------------------------
class TestArtistDiscography(WorkdirCase):
    def _run(self, releases, impl):
        base = self.stage("deezer/artist/1")
        with mock.patch.object(pe, "download_release", impl):
            self.set_env(ARTIST_API_DELAY="0")
            downloaded = asyncio.run(
                pe.download_artist_discography(pcfg.DEEZER, "https://deezer.com/artist/1", base, releases)
            )
        return downloaded, base

    def test_successful_releases_keep_their_folder(self):
        async def impl(platform, url, kind, workdir):
            (workdir / "01 - Test Artist - Song.flac").write_bytes(b"fLaC\x00\x00")
            return _engine_result(platform, url, workdir)

        releases = [{"id": "a", "title": "Kid Amo", "url": "https://d/album/a"},
                    {"id": "b", "title": "Pastel", "url": "https://d/album/b"}]
        downloaded, base = self._run(releases, impl)
        self.assertEqual(len(downloaded), 2)
        self.assertEqual(sorted(p.name for p in base.iterdir()), ["rel1", "rel2"])
        # The exact shape create_and_send_artist_zip consumes.
        release, metadata, tracks = downloaded[0]
        self.assertEqual(release["id"], "a")
        self.assertIsInstance(metadata, ReleaseMetadata)
        self.assertEqual(len(tracks), 1)
        self.assertIn("song_path", tracks[0])

    def test_a_failed_release_is_skipped_and_cleaned(self):
        # One broken album must not sink the discography, and its debris must
        # not end up in the shared artist zip.
        async def impl(platform, url, kind, workdir):
            if url.endswith("/b"):
                (workdir / "partial.flac").write_bytes(b"fLaC\x00\x00")
                raise EngineError("upstream 500")
            (workdir / "01 - Test Artist - Song.flac").write_bytes(b"fLaC\x00\x00")
            return _engine_result(platform, url, workdir)

        releases = [{"id": "a", "title": "Good", "url": "https://d/album/a"},
                    {"id": "b", "title": "Bad", "url": "https://d/album/b"}]
        downloaded, base = self._run(releases, impl)
        self.assertEqual([r["id"] for r, _, _ in downloaded], ["a"])
        self.assertEqual([p.name for p in base.iterdir()], ["rel1"])

    def test_a_release_that_produced_no_tracks_is_dropped(self):
        async def impl(platform, url, kind, workdir):
            return EngineResult(
                metadata=ReleaseMetadata(), tracks=[], mode="token", platform=platform
            )

        releases = [{"id": "a", "title": "Empty", "url": "https://d/album/a"}]
        with self.assertRaises(EngineError):
            self._run(releases, impl)

    def test_all_releases_failing_raises(self):
        async def impl(platform, url, kind, workdir):
            raise EngineError("nope")

        releases = [{"id": "a", "url": "https://d/album/a"}, {"id": "b", "url": "https://d/album/b"}]
        with self.assertRaises(EngineError) as ctx:
            self._run(releases, impl)
        self.assertIn("no releases could be downloaded", str(ctx.exception))

    def test_release_without_a_url_is_skipped_without_calling_the_engine(self):
        calls = []

        async def impl(platform, url, kind, workdir):
            calls.append(url)
            (workdir / "01 - A - B.flac").write_bytes(b"fLaC\x00\x00")
            return _engine_result(platform, url, workdir)

        releases = [{"id": "a", "title": "No URL"},
                    {"id": "b", "title": "Has URL", "url": "https://d/album/b"}]
        downloaded, _ = self._run(releases, impl)
        self.assertEqual(calls, ["https://d/album/b"])
        self.assertEqual([r["id"] for r, _, _ in downloaded], ["b"])

    def test_releases_run_sequentially_not_in_parallel(self):
        # Parallel album fetches across a discography are what gets a 429.
        state = {"depth": 0, "max_depth": 0}

        async def impl(platform, url, kind, workdir):
            state["depth"] += 1
            state["max_depth"] = max(state["max_depth"], state["depth"])
            await asyncio.sleep(0)
            state["depth"] -= 1
            (workdir / "01 - A - B.flac").write_bytes(b"fLaC\x00\x00")
            return _engine_result(platform, url, workdir)

        releases = [{"id": str(i), "url": f"https://d/album/{i}"} for i in range(4)]
        self._run(releases, impl)
        self.assertEqual(state["max_depth"], 1)

    def test_delay_is_applied_between_releases_but_not_before_the_first(self):
        slept = []

        async def impl(platform, url, kind, workdir):
            (workdir / "01 - A - B.flac").write_bytes(b"fLaC\x00\x00")
            return _engine_result(platform, url, workdir)

        releases = [{"id": str(i), "url": f"https://d/album/{i}"} for i in range(3)]
        base = self.stage("deezer/artist/delay")
        self.set_env(ARTIST_API_DELAY="0.25")
        real_sleep = asyncio.sleep

        async def recording_sleep(seconds):
            slept.append(seconds)
            return await real_sleep(0)

        with mock.patch.object(pe, "download_release", impl), mock.patch.object(
            pe.asyncio, "sleep", recording_sleep
        ):
            asyncio.run(
                pe.download_artist_discography(pcfg.DEEZER, "https://d/artist/1", base, releases)
            )
        self.assertEqual(slept, [0.25, 0.25])

    def test_each_release_gets_its_own_subdirectory(self):
        # Shared directories would let one release's discover_release() pick up
        # another's files.
        seen = []

        async def impl(platform, url, kind, workdir):
            seen.append(Path(workdir))
            (workdir / "01 - A - B.flac").write_bytes(b"fLaC\x00\x00")
            return _engine_result(platform, url, workdir)

        releases = [{"id": str(i), "url": f"https://d/album/{i}"} for i in range(3)]
        self._run(releases, impl)
        self.assertEqual(len(set(seen)), 3)


class TestResultShapes(unittest.TestCase):
    def test_track_count_is_derived_not_passed(self):
        result = EngineResult(
            metadata=ReleaseMetadata(),
            tracks=[{"song_path": "a"}, {"song_path": "b"}],
            mode="token",
            platform=pcfg.QOBUZ,
        )
        self.assertEqual(result.track_count, 2)

    def test_metadata_defaults_fill_the_fields_the_archiver_needs(self):
        meta = ReleaseMetadata(artist="Bill Evans", title="Sunday at the Village Vanguard")
        self.assertEqual(meta.album_title, "Sunday at the Village Vanguard")
        self.assertEqual(meta.artists_list, ["Bill Evans"])
        self.assertEqual(meta.release_date, "00/00/0000")

    def test_release_date_follows_the_year_when_given(self):
        self.assertEqual(ReleaseMetadata(year="1965").release_date, "1965")

    def test_empty_metadata_still_produces_a_usable_archive_name(self):
        meta = ReleaseMetadata()
        self.assertEqual(meta.artist, "Unknown Artist")
        self.assertEqual(meta.album_title, "Unknown Album")


class TestStreamripConfig(WorkdirCase):
    @staticmethod
    def _write_defaults(path):
        Path(path).write_text("""\
[downloads]
[database]
[cli]
[misc]
[artwork]
[filepaths]
[qobuz]
""")

    def test_qobuz_config_is_private_and_keeps_discs_in_one_folder(self):
        workdir = self.stage("qobuz/album/1", "01 - A - B.flac")
        self.set_env(QOBUZ_USER_ID="123", QOBUZ_USER_AUTH_TOKEN="tok", QOBUZ_APP_ID="9999")
        config = types.ModuleType("streamrip.config")
        config.set_user_defaults = self._write_defaults
        package = types.ModuleType("streamrip")
        package.config = config
        with mock.patch.dict(sys.modules, {"streamrip": package, "streamrip.config": config}):
            path = pe._streamrip_config(pcfg.QOBUZ, workdir)
        self.assertEqual(path, workdir / "streamrip_config.toml")
        from tomlkit import parse
        doc = parse(path.read_text())
        self.assertEqual(doc["downloads"]["folder"], str(workdir))
        self.assertIs(doc["downloads"]["disc_subdirectories"], False)
        self.assertEqual(doc["filepaths"]["track_format"], "{tracknumber:02d}. {artist} - {title}")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")


class TestDeadMoonProviderHardening(WorkdirCase):
    def test_tidal_bearer_valid_expiry_and_throttled_download_config(self):
        expiry = int(time.time()) + 3600
        self.set_env(
            TIDAL_USER_ID="1", TIDAL_ACCESS_TOKEN="valid-token",
            TIDAL_REFRESH_TOKEN="refresh", TIDAL_TOKEN_EXPIRY=str(expiry),
        )
        with mock.patch.object(pe.pcfg, "has_tidal_credentials", return_value=True):
            doc = {"tidal": {"token_type": ""}}
            pe._configure_tidal(doc, True)
        self.assertEqual(doc["tidal"]["token_type"], "Bearer")
        self.assertEqual(doc["tidal"]["user_id"], "1")
        self.assertEqual(doc["tidal"]["country_code"], "US")
        self.assertGreater(int(doc["tidal"]["token_expiry"]), expiry)
        self.set_env(TIDAL_TOKEN_EXPIRY="1")
        with mock.patch.object(pe.pcfg, "has_tidal_credentials", return_value=True):
            pe._configure_tidal(doc, True)
        self.assertEqual(doc["tidal"]["token_expiry"], "1")

        defaults = {
            "downloads": {}, "database": {}, "cli": {}, "misc": {}, "artwork": {},
            "filepaths": {}, "tidal": {},
        }
        fake_config = types.SimpleNamespace(set_user_defaults=lambda path: Path(path).write_text(""))
        class FakeDocument(dict):
            def as_string(self):
                return ""

        defaults = FakeDocument(defaults)
        with mock.patch.dict(sys.modules, {"streamrip": types.ModuleType("streamrip"), "streamrip.config": fake_config}):
            with mock.patch("tomlkit.parse", return_value=defaults):
                pe._streamrip_config(pcfg.TIDAL, self.root)
        self.assertEqual(defaults["downloads"]["requests_per_minute"], 30)
        self.assertEqual(defaults["downloads"]["max_connections"], 3)

    def test_progress_filters_rich_traceback_and_http_failures(self):
        cwd = self.root / "downloads"
        cwd.mkdir()
        seen = set()
        for line in (
            "401 Unauthorized", "HTTP 403 Forbidden", "429 Too Many Requests",
            "╭───── Traceback ─────╮", "│ value error │", "worker.py: failed",
        ):
            self.assertIsNone(pe._progress_from_output(line, seen, cwd))

    def test_refreshed_tidal_token_is_synced_to_private_toml(self):
        import tomlkit
        config = self.root / "streamrip_config.toml"
        resource = self.root / "tidal_config.toml"
        config.write_text(tomlkit.dumps({"tidal": {"access_token": "new", "refresh_token": "new-refresh", "token_expiry": "42"}}))
        from dl_utils.credential_migrator import atomic_write_text
        atomic_write_text(resource, tomlkit.dumps({"tidal": {"access_token": "old", "refresh_token": "old-refresh"}}))
        os.environ["TIDAL_CONFIG_PATH"] = str(resource)
        pe._sync_refreshed_tidal_tokens(config, {"access_token": "old", "user_id": "1"})
        fields = pcfg.tidal_oauth_fields()
        self.assertEqual(fields["access_token"], "new")
        self.assertEqual(fields["refresh_token"], "new-refresh")
        self.assertEqual(resource.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
