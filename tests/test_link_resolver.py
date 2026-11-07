"""Offline checks for the link resolver: classification, normalisation, shortlinks.

Run with `python3 -m unittest discover tests` or `python3 tests/test_link_resolver.py`.
No network, no Telegram, no credentials.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Keep the live token.env out of the picture: these tests must not depend on, or
# mutate, real credentials.
os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")

from dl_utils.link_resolver import (  # noqa: E402
    KIND_ALBUM,
    KIND_ARTIST,
    KIND_TRACK,
    PLATFORM_AMAZON,
    PLATFORM_APPLE,
    PLATFORM_DEEZER,
    PLATFORM_QOBUZ,
    PLATFORM_SPOTIFY,
    PLATFORM_TIDAL,
    classify,
    extract_url,
    is_shortlink,
    parse_link,
    strip_tracking_params,
)


class TestClassify(unittest.TestCase):
    def assert_link(self, url, platform, kind, object_id, **extra):
        link = classify(url)
        self.assertIsNotNone(link, f"{url} should classify")
        self.assertEqual(link.platform, platform)
        self.assertEqual(link.kind, kind)
        self.assertEqual(link.object_id, object_id)
        for key, value in extra.items():
            self.assertEqual(getattr(link, key), value, f"{url}: {key}")

    # --- Deezer -------------------------------------------------------------
    def test_deezer_canonical(self):
        self.assert_link("https://www.deezer.com/track/12345", PLATFORM_DEEZER, KIND_TRACK, "12345")
        self.assert_link("https://www.deezer.com/album/98765", PLATFORM_DEEZER, KIND_ALBUM, "98765")
        self.assert_link("https://www.deezer.com/artist/11111", PLATFORM_DEEZER, KIND_ARTIST, "11111")

    def test_deezer_locale_segment(self):
        self.assert_link("https://www.deezer.com/fr/track/12345", PLATFORM_DEEZER, KIND_TRACK, "12345")
        self.assert_link("https://deezer.com/en-us/album/98765", PLATFORM_DEEZER, KIND_ALBUM, "98765")

    def test_deezer_normalises_away_locale_and_query(self):
        link = classify("https://www.deezer.com/es/album/98765?utm_source=share&utm_medium=app")
        self.assertEqual(link.url, "https://www.deezer.com/album/98765")

    # --- Qobuz --------------------------------------------------------------
    def test_qobuz_open_and_play_hosts(self):
        self.assert_link("https://open.qobuz.com/track/foobaz123", PLATFORM_QOBUZ, KIND_TRACK, "foobaz123")
        self.assert_link("https://play.qobuz.com/album/9876543", PLATFORM_QOBUZ, KIND_ALBUM, "9876543")

    def test_qobuz_www_locale(self):
        self.assert_link(
            "https://www.qobuz.com/en_intl/album/some-artist-some-album/0072435123459",
            PLATFORM_QOBUZ,
            KIND_ALBUM,
            "0072435123459",
        )

    def test_qobuz_www_alphanumeric_album_id(self):
        link = classify(
            "https://www.qobuz.com/us-en/album/like-me-t-mass-britt-lari/jge3cs7viw6jb"
        )
        self.assertEqual(link.object_id, "jge3cs7viw6jb")
        self.assertEqual(link.url, "https://open.qobuz.com/album/jge3cs7viw6jb")

    def test_qobuz_normalises_to_open_host(self):
        link = classify("https://play.qobuz.com/track/abc123?ref_share=xyz")
        self.assertEqual(link.url, "https://open.qobuz.com/track/abc123")

    # --- Tidal --------------------------------------------------------------
    def test_tidal_browse_and_plain(self):
        self.assert_link("https://tidal.com/track/12345678", PLATFORM_TIDAL, KIND_TRACK, "12345678")
        self.assert_link("https://tidal.com/browse/album/12345678", PLATFORM_TIDAL, KIND_ALBUM, "12345678")
        self.assert_link("https://listen.tidal.com/artist/123456", PLATFORM_TIDAL, KIND_ARTIST, "123456")

    def test_tidal_normalises_browse_away(self):
        link = classify("https://listen.tidal.com/browse/album/42?utm_campaign=x")
        self.assertEqual(link.url, "https://tidal.com/album/42")

    # --- Amazon -------------------------------------------------------------
    def test_amazon_kinds(self):
        self.assert_link("https://music.amazon.com/tracks/B08X1234AB", PLATFORM_AMAZON, KIND_TRACK, "B08X1234AB")
        self.assert_link("https://music.amazon.de/albums/B07XYZ9876", PLATFORM_AMAZON, KIND_ALBUM, "B07XYZ9876")
        self.assert_link("https://music.amazon.co.uk/artists/B001QJ2XLC", PLATFORM_AMAZON, KIND_ARTIST, "B001QJ2XLC")

    def test_amazon_track_asin_means_single_track(self):
        link = classify("https://music.amazon.com/albums/B07XYZ9876?trackAsin=B08X1234AB")
        self.assertEqual(link.kind, KIND_ALBUM)
        self.assertEqual(link.object_id, "B07XYZ9876")
        self.assertEqual(link.track_id, "B08X1234AB")
        self.assertTrue(link.is_single_track)

    def test_amazon_without_track_asin_is_full_album(self):
        link = classify("https://music.amazon.com/albums/B07XYZ9876")
        self.assertIsNone(link.track_id)
        self.assertFalse(link.is_single_track)

    # --- Spotify ------------------------------------------------------------
    def test_spotify_intl_segment_collapses(self):
        self.assert_link("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC", PLATFORM_SPOTIFY, KIND_TRACK, "4uLU6hMCjMI75M1A2tKUQC")
        link = classify("https://open.spotify.com/intl-de/album/1DFixLWuPkv3KT3TnV35m3?si=AbCdEfG")
        self.assertEqual(link.platform, PLATFORM_SPOTIFY)
        self.assertEqual(link.kind, KIND_ALBUM)
        self.assertEqual(link.url, "https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3")

    def test_spotify_artist(self):
        self.assert_link("https://open.spotify.com/intl-fr/artist/66CXWjxzNUsdJxJ2JdwvnR", PLATFORM_SPOTIFY, KIND_ARTIST, "66CXWjxzNUsdJxJ2JdwvnR")

    # --- Apple --------------------------------------------------------------
    def test_apple_album_and_song(self):
        self.assert_link(
            "https://music.apple.com/us/album/kid-amo/1578066229",
            PLATFORM_APPLE,
            KIND_ALBUM,
            "1578066229",
            storefront="us",
        )
        self.assert_link(
            "https://music.apple.com/us/song/spiders/1578066233",
            PLATFORM_APPLE,
            KIND_TRACK,
            "1578066233",
        )

    def test_apple_album_with_i_param_is_single_track(self):
        link = classify("https://music.apple.com/us/album/kid-amo/1578066229?i=1578066233")
        self.assertEqual(link.kind, KIND_ALBUM)
        self.assertEqual(link.object_id, "1578066229")
        self.assertEqual(link.track_id, "1578066233")
        self.assertTrue(link.is_single_track)
        # The meaningful parameter survives normalisation.
        self.assertIn("i=1578066233", link.url)

    def test_apple_album_with_i_plus_tracking(self):
        link = classify(
            "https://music.apple.com/us/album/kid-amo/1578066229?i=1578066233&utm_source=linkstool&at=11l7Ag"
        )
        self.assertEqual(link.track_id, "1578066233")
        self.assertNotIn("utm_source", link.url)
        self.assertNotIn("at=", link.url)

    def test_apple_storefront_with_dash(self):
        self.assert_link(
            "https://music.apple.com/gb/album/x/1578066229", PLATFORM_APPLE, KIND_ALBUM, "1578066229",
            storefront="gb",
        )

    def test_itunes_host(self):
        link = classify("https://itunes.apple.com/us/album/kid-amo/1578066229")
        self.assertEqual(link.platform, PLATFORM_APPLE)


class TestShortlinks(unittest.TestCase):
    def test_wrapper_hosts(self):
        for url in (
            "https://deezer.page.link/abcDEF123",
            "https://dzr.page.link/xyz",
            "https://link.deezer.com/track/12345",
            "https://spotify.link/QwErTy",
            "https://open.spotify.com/intl-es/track/abc",  # not a shortlink
        ):
            expected = "intl" not in url
            self.assertEqual(is_shortlink(url), expected, url)

    def test_more_wrappers(self):
        for url in (
            "https://spoti.fi/3xYz",
            "https://t.idal.com/aBc",
            "https://amzn.to/3abcDEF",
            "https://a.co/d/abcdEFG",
            "https://apple.co/3xYzAbC",
            "https://on.soundcloud.com/AbCdEf",
            "https://soundcloud.app.goo.gl/xyz",
            "https://bit.ly/3abc",
        ):
            self.assertTrue(is_shortlink(url), url)

    def test_canonical_not_shortlink(self):
        for url in (
            "https://tidal.com/browse/track/12345",
            "https://open.qobuz.com/album/12345",
            "https://music.amazon.com/albums/B07XYZ9876",
            "https://music.apple.com/us/album/x/123",
        ):
            self.assertFalse(is_shortlink(url), url)


class TestQueryStripping(unittest.TestCase):
    def test_tracking_gone_meaningful_kept(self):
        url = "https://music.apple.com/us/album/x/123?i=456&utm_source=sh&fbclid=abc&ls=1"
        out = strip_tracking_params(url)
        self.assertIn("i=456", out)
        for junk in ("utm_source", "fbclid", "ls="):
            self.assertNotIn(junk, out)

    def test_no_query_untouched(self):
        self.assertEqual(strip_tracking_params("https://tidal.com/track/1"), "https://tidal.com/track/1")

    def test_single_letter_keys_not_swallowed(self):
        # `s` and `t` are tracking noise on these platforms, `i` is not.
        self.assertEqual(strip_tracking_params("https://x.com/a?t=1&s=2&i=3"), "https://x.com/a?i=3")


class TestExtractUrl(unittest.TestCase):
    def test_url_inside_sentence(self):
        text = "grab this https://open.spotify.com/track/abc123 thanks"
        self.assertEqual(extract_url(text), "https://open.spotify.com/track/abc123")

    def test_trailing_punctuation_dropped(self):
        self.assertEqual(
            extract_url("see https://tidal.com/track/99."),
            "https://tidal.com/track/99",
        )

    def test_no_url(self):
        self.assertEqual(extract_url("just some words"), "")

    def test_empty(self):
        self.assertEqual(extract_url(None), "")


class TestParseLink(unittest.TestCase):
    def test_parse_link_is_classify(self):
        self.assertEqual(parse_link("https://tidal.com/track/5").object_id, "5")

    def test_parse_link_blank(self):
        self.assertIsNone(parse_link(""))
        self.assertIsNone(classify(""))

    def test_unknown_host(self):
        self.assertIsNone(classify("https://example.com/album/123"))

    def test_whitespace_survives(self):
        link = classify("  https://www.deezer.com/track/77  ")
        self.assertEqual(link.object_id, "77")


if __name__ == "__main__":
    unittest.main()
