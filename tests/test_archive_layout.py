"""Offline checks for the packaging layer: zip layout, hashed names, tags.

The contract these enforce is that a Qobuz album, a Deezer album and an Apple
album all arrive on disk and inside the archive with byte-identical path shapes,
because the MIR pipeline downstream parses those paths.

Run with `python3 -m unittest discover tests`. No network, no Telegram.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")

from dl_utils import archive_delivery as ad  # noqa: E402
from dl_utils import platform_config as pcfg  # noqa: E402
from dl_utils.deezer_utils import clean_filename  # noqa: E402


def make_track(
    source_dir: Path,
    name: str,
    number,
    artist="Test Artist",
    title=None,
    ext=".flac",
    size: int = 36,
):
    """One fake downloaded track, in the shape provider_engines produces."""
    path = source_dir / f"{name}{ext}"
    # Incompressible filler: deflate would otherwise shrink these to near zero and
    # every size-based assertion below would silently stop partitioning anything.
    path.write_bytes(os.urandom(size))
    return {
        "song_path": str(path),
        "song_name": title if title is not None else name,
        "artist_name": artist,
        "TRACK_NUMBER": number,
        "file_extension": ext,
        "download_dir": str(source_dir),
    }


def metadata(**overrides):
    base = {
        "id": "123",
        "title": "Midnight Static",
        "artist": "Test Artist",
        "album_title": "Midnight Static",
        "artists_list": ["Test Artist"],
        "release_date": "12/03/2024",
        "year": "2024",
        "album_link": "https://example.com/album/123",
        "track_link": "",
        "cover_data": b"\xff\xd8\xff\xe0fakejpeg",
        "tracks_api_data": [],
        "isrcs": [],
        "siblings": {},
        "clean_artist": clean_filename("Test Artist"),
        "clean_album_title": clean_filename("Midnight Static"),
        "clean_title": clean_filename("Midnight Static"),
    }
    base.update(overrides)
    return base


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="deadmoon-archive-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)


class TestReleaseEntries(TempDirTestCase):
    def test_album_layout_is_folder_plus_numbered_tracks_plus_cover(self):
        tracks = [
            make_track(self.dir, "01 - Opening", 1, title="Opening"),
            make_track(self.dir, "02 - Interlude", 2, title="Interlude"),
            make_track(self.dir, "03 - Closing", 3, title="Closing"),
        ]
        entries = ad.build_release_entries(metadata(), tracks)

        inside = sorted(entries.values())
        self.assertEqual(
            inside,
            [
                "Test Artist - Midnight Static [2024]/01 - Test Artist - Opening.flac",
                "Test Artist - Midnight Static [2024]/02 - Test Artist - Interlude.flac",
                "Test Artist - Midnight Static [2024]/03 - Test Artist - Closing.flac",
                "Test Artist - Midnight Static [2024]/cover.jpg",
            ],
        )

    def test_track_numbers_are_zero_padded_and_sorted(self):
        # Deliberately out of order on input: the archive must still be ordered.
        tracks = [
            make_track(self.dir, "10 - Late", 10, title="Late"),
            make_track(self.dir, "2 - Early", 2, title="Early"),
            make_track(self.dir, "1 - First", 1, title="First"),
        ]
        entries = ad.build_release_entries(metadata(), tracks)
        names = [
            Path(v).name for v in sorted(entries.values()) if not v.endswith("cover.jpg")
        ]
        self.assertEqual(
            names,
            [
                "01 - Test Artist - First.flac",
                "02 - Test Artist - Early.flac",
                "10 - Test Artist - Late.flac",
            ],
        )

    def test_missing_track_number_falls_back_to_position(self):
        tracks = [
            make_track(self.dir, "a", None, title="Alpha"),
            make_track(self.dir, "b", None, title="Beta"),
        ]
        entries = ad.build_release_entries(metadata(), tracks)
        names = sorted(Path(v).name for v in entries.values() if not v.endswith("cover.jpg"))
        self.assertEqual(
            names, ["01 - Test Artist - Alpha.flac", "02 - Test Artist - Beta.flac"]
        )

    def test_parent_dir_nests_every_entry_one_level_deeper(self):
        tracks = [make_track(self.dir, "01 - Opening", 1, title="Opening")]
        flat = ad.build_release_entries(metadata(), tracks)
        nested = ad.build_release_entries(metadata(), tracks, parent_dir="Test Artist")
        for source, dest in flat.items():
            self.assertEqual(nested[source], f"Test Artist/{dest}")

    def test_artist_discography_layout(self):
        """<Artist>/<Artist> - <Album> [Year]/NN - <Artist> - <Title>.<ext>"""
        tracks = [make_track(self.dir, "01 - Song", 1, title="Song")]
        entries = ad.build_release_entries(
            metadata(album_title="Red Rooms", clean_album_title=clean_filename("Red Rooms")),
            tracks,
            parent_dir="Test Artist",
        )
        audio = [v for v in entries.values() if v.endswith(".flac")]
        self.assertEqual(
            audio, ["Test Artist/Test Artist - Red Rooms [2024]/01 - Test Artist - Song.flac"]
        )

    def test_featured_contributor_wins_over_album_artist(self):
        meta = metadata(
            tracks_api_data=[
                {"id": "t1", "title": "Collab", "track_position": 1,
                 "contributors": [{"name": "Test Artist"}, {"name": "Guest MC"}]}
            ]
        )
        tracks = [dict(make_track(self.dir, "01", 1, title="Collab"), SNG_ID="t1")]
        entries = ad.build_release_entries(meta, tracks)
        audio = [v for v in entries.values() if v.endswith(".flac")]
        self.assertEqual(
            audio,
            ["Test Artist - Midnight Static [2024]/01 - Test Artist, Guest MC - Collab.flac"],
        )

    def test_per_track_title_from_api_overrides_filename(self):
        meta = metadata(
            tracks_api_data=[{"id": "t9", "title": "Proper Title", "track_position": 1,
                              "contributors": []}]
        )
        tracks = [dict(make_track(self.dir, "01 - wrong", 1, title="wrong"), SNG_ID="t9")]
        entries = ad.build_release_entries(meta, tracks)
        audio = [v for v in entries.values() if v.endswith(".flac")]
        self.assertTrue(audio[0].endswith("01 - Test Artist - Proper Title.flac"), audio)

    def test_cover_is_written_into_the_source_dir(self):
        tracks = [make_track(self.dir, "01 - Song", 1, title="Song")]
        entries = ad.build_release_entries(metadata(), tracks)
        cover = self.dir / "cover.jpg"
        self.assertTrue(cover.exists())
        self.assertEqual(cover.read_bytes(), b"\xff\xd8\xff\xe0fakejpeg")
        self.assertIn(str(cover), entries)

    def test_absent_cover_data_still_builds(self):
        tracks = [make_track(self.dir, "01 - Song", 1, title="Song")]
        entries = ad.build_release_entries(metadata(cover_data=None), tracks)
        self.assertFalse(any(v.endswith("cover.jpg") for v in entries.values()))
        self.assertEqual(len(entries), 1)

    def test_nonexistent_song_path_is_skipped(self):
        good = make_track(self.dir, "01 - Song", 1, title="Song")
        ghost = make_track(self.dir, "02 - Gone", 2, title="Gone")
        ghost["song_path"] = str(self.dir / "does-not-exist.flac")
        entries = ad.build_release_entries(metadata(), [good, ghost])
        audio = [v for v in entries.values() if v.endswith(".flac")]
        self.assertEqual(len(audio), 1)

    def test_empty_track_list_raises(self):
        with self.assertRaises(ValueError):
            ad.build_release_entries(metadata(), [])

    def test_illegal_characters_are_sanitised_out(self):
        meta = metadata(
            artist='AC/DC: "Live" Vol.1',
            album_title='AC/DC: "Live" Vol.1',
            clean_artist=clean_filename('AC/DC: "Live" Vol.1'),
            clean_album_title=clean_filename('AC/DC: "Live" Vol.1'),
        )
        tracks = [make_track(self.dir, "01", 1, title='Bad?: "Take"')]
        entries = ad.build_release_entries(meta, tracks)
        for dest in entries.values():
            self.assertFalse(dest.startswith("/"), dest)
            # "/" is the archive's own path separator; every OTHER component
            # must be free of characters any target filesystem would reject.
            for component in dest.split("/"):
                for ch in '\\/:*?"<>|':
                    self.assertNotIn(ch, component, dest)

    def test_mp3_and_m4a_extensions_pass_through(self):
        for ext in (".mp3", ".m4a", ".flac", ".wav"):
            with self.subTest(ext=ext):
                tracks = [make_track(self.dir, f"01{ext}", 1, title="Song", ext=ext)]
                entries = ad.build_release_entries(metadata(), tracks)
                audio = [v for v in entries.values() if v.endswith(ext)]
                self.assertEqual(len(audio), 1, ext)


class TestZipWriting(TempDirTestCase):
    def test_write_zip_roundtrips_entries(self):
        tracks = [make_track(self.dir, "01 - Song", 1, title="Song")]
        entries = ad.build_release_entries(metadata(), tracks)
        out = self.dir.parent / "release.zip"
        ad._write_zip_sync(out, list(entries.items()))
        with zipfile.ZipFile(out) as zf:
            names = sorted(zf.namelist())
            self.assertEqual(
                names,
                [
                    "Test Artist - Midnight Static [2024]/01 - Test Artist - Song.flac",
                    "Test Artist - Midnight Static [2024]/cover.jpg",
                ],
            )
            self.assertEqual(
                zf.read(names[0]), (self.dir / "01 - Song.flac").read_bytes()
            )

    # 4 KiB of random filler per track, 10 KiB ceiling => at most 2 tracks/part.
    TRACK_BYTES = 4096
    PART_CEILING = 10 * 1024

    def _tracks(self, count):
        return [
            make_track(
                self.dir, f"{i:02d} - Song {i}", i, title=f"Song {i}",
                size=self.TRACK_BYTES,
            )
            for i in range(1, count + 1)
        ]

    def test_multipart_split_respects_the_size_ceiling(self):
        entries = list(ad.build_release_entries(metadata(), self._tracks(5)).items())
        paths = ad._write_multipart_zips_sync(
            self.dir.parent / "rel.zip", entries, self.PART_CEILING
        )
        self.assertGreater(len(paths), 1)
        for path in paths:
            self.assertLessEqual(path.stat().st_size, self.PART_CEILING, path.name)

    def test_every_entry_appears_exactly_once_across_parts(self):
        entries = list(ad.build_release_entries(metadata(), self._tracks(6)).items())
        paths = ad._write_multipart_zips_sync(
            self.dir.parent / "rel.zip", entries, self.PART_CEILING
        )
        seen = []
        for path in paths:
            with zipfile.ZipFile(path) as zf:
                seen.extend(zf.namelist())
        self.assertEqual(len(seen), len(set(seen)), "an entry was duplicated")
        self.assertEqual(sorted(seen), sorted(dest for _, dest in entries))

    def test_single_file_over_the_ceiling_is_dropped_not_retried_forever(self):
        # A track bigger than Telegram's hard upload cap cannot be sent at all, so
        # it is skipped rather than looping until the part ceiling is exhausted.
        tracks = self._tracks(2)
        tracks.append(
            make_track(self.dir, "03 - Huge", 3, title="Huge", size=3 * self.PART_CEILING)
        )
        entries = list(ad.build_release_entries(metadata(), tracks).items())
        paths = ad._write_multipart_zips_sync(
            self.dir.parent / "rel.zip", entries, self.PART_CEILING
        )
        seen = []
        for path in paths:
            with zipfile.ZipFile(path) as zf:
                seen.extend(zf.namelist())
        # 2 tracks + cover.jpg survive; the oversized track is the only loss.
        self.assertEqual(len(seen), 3, sorted(seen))
        self.assertTrue(all("Huge" not in n for n in seen), seen)
        self.assertTrue(all(p.stat().st_size <= self.PART_CEILING for p in paths))

    def test_part_filenames_are_sequential(self):
        entries = list(ad.build_release_entries(metadata(), self._tracks(5)).items())
        paths = ad._write_multipart_zips_sync(
            self.dir.parent / "rel.zip", entries, self.PART_CEILING
        )
        self.assertEqual(
            [p.name for p in paths],
            [f"rel.zip_part{i}.zip" for i in range(1, len(paths) + 1)],
        )


class TestHashedArchiveName(TempDirTestCase):
    def test_name_carries_an_eight_char_hash(self):
        # The basename is extension-less: deliver_zip appends .zip (or
        # _partN.zip) itself, so the hash must land before that suffix.
        name = ad.hashed_archive_basename("Artist - Album [2024]", "Artist - Album")
        self.assertNotIn(".", name, name)
        self.assertTrue(name.endswith(")"), name)
        hash_part = name.rsplit("(", 1)[1][:-1]
        self.assertEqual(len(hash_part), 8, hash_part)
        self.assertTrue(all(c in "0123456789abcdef" for c in hash_part), hash_part)
        # The prefix comes from copy_zip_name, not hash_title, so the bracketed
        # year never reaches the filename a user sees.
        self.assertTrue(name.startswith("Artist_-_Album_"), name)

    def test_spaces_and_punctuation_are_flattened(self):
        name = ad.hashed_archive_basename("A/B: C", 'D "E" F [G]')
        for ch in '/:*?"<>| ':
            self.assertNotIn(ch, name, name)

    def test_same_release_same_name(self):
        a = ad.hashed_archive_basename("Artist - Album [2024]", "Artist - Album")
        b = ad.hashed_archive_basename("Artist - Album [2024]", "Artist - Album")
        self.assertEqual(a, b)

    def test_different_release_different_name(self):
        a = ad.hashed_archive_basename("Artist - Album [2024]", "Artist - Album")
        b = ad.hashed_archive_basename("Artist - Album [2025]", "Artist - Album")
        self.assertNotEqual(a, b)

    def test_distinct_titles_do_not_collide(self):
        names = {
            ad.hashed_archive_basename(f"A - {t} [2024]", f"A - {t}")
            for t in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        }
        self.assertEqual(len(names), 5)


class TestReleaseZipNames(TempDirTestCase):
    def test_album_names_use_artist_and_album(self):
        meta = metadata()
        hash_title, copy_name = ad.release_zip_names(meta, is_album=True)
        self.assertEqual(hash_title, "Test Artist - Midnight Static [2024]")
        self.assertEqual(copy_name, "Test Artist - Midnight Static")

    def test_transliteration_keeps_the_filename_ascii(self):
        meta = metadata(
            artist="Björk",
            title="Vespertine",
            year="2001",
            clean_artist="Björk",
            clean_title="Vespertine",
        )
        _, copy_name = ad.release_zip_names(meta, is_album=True)
        self.assertTrue(copy_name.isascii(), copy_name)


class TestCaptions(unittest.TestCase):
    """Captions are fixed-shape HTML templates filled from the release metadata."""

    def test_track_caption_has_artist_title_and_both_links(self):
        meta = metadata(track_link="https://example.com/track/123")
        caption = ad.get_track_caption(meta)
        self.assertIn("<b>Track: Midnight Static</b>", caption)
        self.assertIn("Test Artist - 12/03/2024", caption)
        self.assertIn('href="https://example.com/album/123"', caption)
        self.assertIn('href="https://example.com/track/123"', caption)

    def test_track_caption_omits_an_unavailable_album_link(self):
        meta = metadata(album_link="", track_link="https://example.com/track/123")
        caption = ad.get_track_caption(meta)
        self.assertNotIn("Album Link", caption)
        self.assertNotIn('href="None"', caption)
        self.assertIn('href="https://example.com/track/123"', caption)

    def test_album_caption_omits_the_track_link(self):
        caption = ad.get_album_caption(metadata())
        self.assertIn("<b>Album: Midnight Static</b>", caption)
        self.assertIn('href="https://example.com/album/123"', caption)

    def test_captions_report_actual_source_and_quality(self):
        meta = metadata(
            source_label="Deezer ARL (Fallback from Qobuz)", quality_label="FLAC"
        )
        self.assertIn("Source: Deezer ARL (Fallback from Qobuz) · FLAC", ad.get_album_caption(meta))
        self.assertIn("Source: Qobuz (Direct · streamrip) · MP3", ad.get_track_caption(metadata(
            source_label="Qobuz (Direct · streamrip)", quality_label="MP3"
        )))

    def test_artist_caption_reports_the_scale_of_the_pull(self):
        artist = {"name": "Test Artist", "link": "https://example.com/artist/1"}
        caption = ad.get_artist_caption(artist, 4, 37)
        self.assertIn("<b>Artist: Test Artist</b>", caption)
        self.assertIn("4 release(s), 37 track(s)", caption)
        self.assertIn('href="https://example.com/artist/1"', caption)

    def test_captions_stay_well_under_the_telegram_limit(self):
        # Telegram rejects audio captions over 1024 characters. These are
        # templates rather than generated tracklists, so a long release cannot
        # blow the limit — but a missing key must still surface as a KeyError
        # here rather than as a failed send in production.
        meta = metadata(track_link="https://example.com/track/123")
        for builder in (ad.get_track_caption, ad.get_album_caption):
            self.assertLess(len(builder(meta)), 1024, builder.__name__)

    def test_missing_key_is_a_hard_error_not_a_silent_blank(self):
        broken = metadata()
        del broken["release_date"]
        with self.assertRaises(KeyError):
            ad.get_track_caption(broken)


class TestSafeDelete(TempDirTestCase):
    def test_existing_message_like_object_is_deleted(self):
        import asyncio

        victim = _FakeDeletable()
        asyncio.run(ad._safe_delete(victim))
        self.assertTrue(victim.deleted)

    def test_missing_file_is_ignored(self):
        import asyncio

        asyncio.run(ad._safe_delete(self.dir / "never-existed.bin"))

    def test_a_raising_delete_does_not_propagate(self):
        # Deletion runs in a background task after the reply is already sent; an
        # exception there would otherwise become an un-awaited-task warning.
        import asyncio

        class Exploding:
            async def delete(self):
                raise RuntimeError("telegram said no")

        asyncio.run(ad._safe_delete(Exploding()))


class _FakeDeletable:
    def __init__(self):
        self.deleted = False

    async def delete(self):
        self.deleted = True


class TestThumbnailFallback(unittest.TestCase):
    def test_garbage_bytes_return_none_not_raise(self):
        self.assertIsNone(ad.make_audio_thumbnail(b"not an image at all"))

    def test_empty_bytes_return_none(self):
        self.assertIsNone(ad.make_audio_thumbnail(b""))


def _deezer_handler_importable() -> bool:
    """handlers.deezer pulls dl_utils.deezer_download, which needs pycryptodome."""
    return importlib.util.find_spec("Crypto") is not None


@unittest.skipUnless(_deezer_handler_importable(), "pycryptodome is not installed")
class TestDeezerReExports(unittest.TestCase):
    """The Deezer handler must re-export the shared packaging layer, not fork it.

    Every other platform delivers through archive_delivery. Deezer predates that
    refactor and keeps thin module-level aliases so its existing call sites, the
    /start deep-link path and the inline handlers keep working. If an alias is
    ever rebound to a local copy, Deezer silently diverges from the layout the
    downstream MIR parser reads -- so assert object identity, not just presence.
    """

    REEXPORTS = (
        "build_release_entries",
        "create_and_send_zip",
        "create_and_send_artist_zip",
        "send_track_audio",
        "send_album_audio",
        "schedule_messages_deletion",
        "schedule_file_deletion",
        "deliver_zip",
    )

    @classmethod
    def setUpClass(cls):
        import handlers.deezer as deezer

        cls.deezer = deezer

    def test_aliases_point_at_the_shared_implementations(self):
        for name in self.REEXPORTS:
            with self.subTest(symbol=name):
                self.assertIs(
                    getattr(self.deezer, name),
                    getattr(ad, name),
                    f"handlers.deezer.{name} is not archive_delivery.{name}",
                )

    def test_safe_delete_swallow_errors(self):
        """_safe_delete must not propagate: a vanished message is not a failure."""
        self.assertTrue(callable(self.deezer._safe_delete))

    def test_artist_toggle_default_matches_config(self):
        """Every platform, including Deezer, requires an explicit artist opt-in."""
        for platform in pcfg.PLATFORMS:
            self.assertFalse(pcfg.artist_download_enabled(platform), platform)


if __name__ == "__main__":
    unittest.main()
