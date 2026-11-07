"""Offline checks for handlers/streaming.py.

These target the two contracts that are easiest to break silently and hardest to
notice in production:

* a refused request must not consume a download slot, and
* every taken slot must be released and every staging directory removed, on
  every exit path — success, engine error, or an unexpected exception.

Both are tested against the real handler functions with a fake aiogram event, so
no network, no Telegram and no external downloader is involved.

Run with `python3 -m unittest discover tests`.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")

import utils  # noqa: E402
from dl_utils import platform_config as pcfg  # noqa: E402
from dl_utils.provider_engines import EngineError  # noqa: E402
from handlers import streaming as st  # noqa: E402


class _FakeMessage:
    def __init__(self):
        self.deleted = False
        self.edits = []

    async def delete(self):
        self.deleted = True

    async def edit_text(self, text):
        self.edits.append(text)


class _FakeEvent:
    """Stand-in for types.Message: only answer()/from_user are exercised."""

    def __init__(self, user_id=4242, text=""):
        self.text = text
        self.answers = []
        self.messages = []
        self.reply_markups = []
        self.from_user = _FakeUser(user_id)

    async def answer(self, text, parse_mode=None, reply_markup=None, **kwargs):
        self.answers.append(text)
        self.reply_markups.append(reply_markup)
        message = _FakeMessage()
        self.messages.append(message)
        return message

    @property
    def last_answer(self):
        return self.answers[-1] if self.answers else None


class _FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.username = "samsauce"
        self.first_name = "Sam"


class StreamingCaseBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_caps = (utils.MAX_CONCURRENT_DOWNLOADS, utils.MAX_QUEUED_DOWNLOADS)
        utils.MAX_CONCURRENT_DOWNLOADS = 2
        utils.MAX_QUEUED_DOWNLOADS = 1
        self._clear_scheduler()

        self._tmp = tempfile.TemporaryDirectory(prefix="deadmoon-streaming-")
        self.tmpdir = Path(self._tmp.name)
        # staging_dir() is the only path writer here and it lives in
        # provider_engines, which imported TMP_DIR by value; patch that copy.
        import dl_utils.provider_engines as pe

        self._pe = pe
        self._saved_tmp = pe.TMP_DIR
        pe.TMP_DIR = str(self.tmpdir)

        self._saved_env = os.environ.copy()
        os.environ.pop("FALLBACK_ENABLED", None)
        self._saved_token_env = utils.TOKEN_ENV_PATH
        # Point token.env at an absent path so the real file (which may hold live
        # credentials and arbitrary platform toggles) cannot influence a test.
        utils.TOKEN_ENV_PATH = str(self.tmpdir / "nonexistent-token.env")

        # Hard network ban. Every metadata lookup in these handlers goes through
        # metadata_bridge._fetch_text, so poisoning that one function guarantees
        # a test can never reach song.link / iTunes / Deezer by accident — if a
        # code path grows a new outbound call, a test fails loudly instead of
        # quietly hitting the internet in CI.
        from dl_utils import metadata_bridge as mb

        self._mb = mb
        self._saved_fetch_text = mb._fetch_text
        self._saved_fetch_json = mb._fetch_json

        async def no_network(*args, **kwargs):
            raise AssertionError("metadata_bridge attempted a network call")

        mb._fetch_text = no_network
        mb._fetch_json = no_network

        # Delivery is covered by test_archive_layout; here it is replaced with a
        # recorder so the success path can be asserted without a real Bot.
        self.delivered = []
        self._saved_send_zip = st.create_and_send_zip
        self._saved_send_artist_zip = st.create_and_send_artist_zip

        async def record_zip(event, metadata, tracks, is_album):
            self.delivered.append(("release", metadata, list(tracks), is_album))

        async def record_artist_zip(event, artist_metadata, downloaded):
            self.delivered.append(("artist", artist_metadata, list(downloaded)))

        st.create_and_send_zip = record_zip
        st.create_and_send_artist_zip = record_artist_zip

    def tearDown(self):
        utils.MAX_CONCURRENT_DOWNLOADS, utils.MAX_QUEUED_DOWNLOADS = self._saved_caps
        self._clear_scheduler()
        self._pe.TMP_DIR = self._saved_tmp
        utils.TOKEN_ENV_PATH = self._saved_token_env
        self._mb._fetch_text = self._saved_fetch_text
        self._mb._fetch_json = self._saved_fetch_json
        st.create_and_send_zip = self._saved_send_zip
        st.create_and_send_artist_zip = self._saved_send_artist_zip
        os.environ.clear()
        os.environ.update(self._saved_env)
        self._tmp.cleanup()

    def _clear_scheduler(self):
        utils._active_slots.clear()
        utils._queued_slots.clear()

    def set_env(self, **kwargs):
        for key, value in kwargs.items():
            os.environ[key] = value

    def active_count(self):
        return len(utils._active_slots)

    def enable_qobuz(self):
        """Give Qobuz a complete token-mode credential set."""
        self.set_env(
            QOBUZ_ENABLED="1",
            QOBUZ_USER_ID="12345",
            QOBUZ_APP_ID="9999",
            QOBUZ_USER_AUTH_TOKEN="abcdef",
        )

    def staging_root_is_clean(self):
        leftovers = [p for p in self.tmpdir.rglob("*") if p.is_file()]
        return leftovers


class TestRefusalDoesNotConsumeASlot(StreamingCaseBase):
    async def test_disabled_platform_is_refused_without_a_slot(self):
        self.set_env(QOBUZ_ENABLED="0")
        link = st.parse_link("https://open.qobuz.com/track/foobaz123")
        self.assertEqual(link.platform, pcfg.QOBUZ)

        event = _FakeEvent()
        await st._dispatch(event, link)

        self.assertEqual(self.active_count(), 0, "a refused request took a slot")
        self.assertIn("disabled", event.last_answer)
        # Nothing was staged either — a refusal should not touch the disk.
        self.assertEqual(self.staging_root_is_clean(), [])

    async def test_every_routed_platform_can_be_disabled(self):
        for platform, sample in (
            (pcfg.QOBUZ, "https://open.qobuz.com/album/foobaz123"),
            (pcfg.TIDAL, "https://tidal.com/album/123456789"),
            (pcfg.AMAZON, "https://music.amazon.com/tracks/B08X1234AB"),
            (pcfg.SPOTIFY, "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"),
            (pcfg.APPLE, "https://music.apple.com/us/album/x/1578066229"),
        ):
            with self.subTest(platform=platform):
                self._clear_scheduler()
                # pcfg._env_key, not an f-string: the platform constants are
                # lowercase ("qobuz") and the env keys are uppercase
                # ("QOBUZ_ENABLED"), so splicing produced "qobuz_ENABLED" —
                # which no reader ever sees, leaving the platform enabled and
                # sending the test down the live dispatch path.
                self.set_env(**{pcfg._env_key(platform, "ENABLED"): "0"})
                event = _FakeEvent()
                await st._dispatch(event, st.parse_link(sample))
                self.assertEqual(self.active_count(), 0)
                self.assertIn("disabled", event.last_answer)

    async def test_artist_link_on_a_disabled_artist_toggle_is_refused_without_a_slot(self):
        self.set_env(
            TIDAL_ENABLED="1",
            TIDAL_ARTIST_DOWNLOAD_ENABLED="0",
            TIDAL_USER_ID="1",
            TIDAL_OAUTH_TOKEN="tok",
        )
        link = st.parse_link("https://tidal.com/artist/123456789")
        self.assertEqual(link.kind, st.KIND_ARTIST)

        event = _FakeEvent()
        await st._handle_artist(event, link)

        self.assertEqual(self.active_count(), 0, "a disabled feature still took a slot")
        self.assertIn("only supported for Deezer", event.last_answer)
        self.assertIn("album or track", event.last_answer)

    async def test_unrouted_link_is_ignored_silently(self):
        # A link this router does not own must fall through to another router
        # without a reply, without a slot, and without touching an engine.
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://soundcloud.com/artist/track",
            "https://www.deezer.com/album/98765",
        ):
            with self.subTest(url=url):
                self._clear_scheduler()
                event = _FakeEvent()
                await st._dispatch(event, st.parse_link(url))
                self.assertEqual(self.active_count(), 0)
                self.assertEqual(event.answers, [])
                self.assertEqual(self.delivered, [])

    async def test_dispatch_rejects_none(self):
        event = _FakeEvent()
        await st._dispatch(event, None)
        self.assertEqual(self.active_count(), 0)
        self.assertEqual(event.answers, [])


class TestSlotAndStagingAreAlwaysReleased(StreamingCaseBase):
    """The finally: block is the whole contract — prove it holds on every path."""

    async def _run_release(self, download_impl):
        original = st.download_release

        async def accepts_progress(*args, **kwargs):
            kwargs.pop("progress_cb", None)
            return await download_impl(*args, **kwargs)

        st.download_release = accepts_progress
        try:
            link = st.parse_link("https://open.qobuz.com/track/foobaz123")
            event = _FakeEvent()
            await st._handle_release(event, link)
            return event
        finally:
            st.download_release = original

    async def test_slot_released_and_staging_removed_on_success(self):
        self.enable_qobuz()

        async def ok(platform, url, kind, workdir):
            # Leave a file behind so cleanup has something to remove.
            (workdir / "01 - Song.flac").write_bytes(b"fLaC" + b"\x00" * 64)
            return _Result(platform, url, workdir)

        await self._run_release(ok)
        self.assertEqual(self.active_count(), 0, "slot leaked on the success path")
        self.assertEqual(self.staging_root_is_clean(), [], "staging dir survived")
        self.assertEqual(len(self.delivered), 1)


    async def test_engine_labels_reach_delivery_after_metadata_enrichment(self):
        self.enable_qobuz()
        from dl_utils.provider_engines import ReleaseMetadata

        async def ok(platform, url, kind, workdir):
            result = _Result(platform, url, workdir)
            result.metadata = ReleaseMetadata(
                artist="Engine Artist",
                title="Engine Song",
                kind=st.KIND_TRACK,
                source_label="Qobuz (Direct · streamrip)",
                quality_label="FLAC 24-bit/96 kHz",
            )
            return result

        info = self._mb.ReleaseInfo(
            platform=pcfg.QOBUZ,
            kind=st.KIND_TRACK,
            object_id="foobaz123",
            artist="Canonical Artist",
            title="Canonical Song",
            album_title="Canonical Album",
        )
        original_release_info = st.metadata_bridge.release_info

        async def enriched(**kwargs):
            return info

        st.metadata_bridge.release_info = enriched
        try:
            await self._run_release(ok)
        finally:
            st.metadata_bridge.release_info = original_release_info

        metadata = self.delivered[0][1]
        self.assertEqual(metadata["artist"], "Canonical Artist")
        self.assertEqual(metadata["title"], "Canonical Song")
        self.assertEqual(metadata["source_label"], "Qobuz (Direct · streamrip)")
        self.assertEqual(metadata["quality_label"], "FLAC 24-bit/96 kHz")


    async def test_slot_released_and_staging_removed_on_engine_error(self):
        self.enable_qobuz()

        async def engine_booms(platform, url, kind, workdir):
            (workdir / "partial.flac").write_bytes(b"x")
            raise EngineError("streamrip exited 1")

        event = await self._run_release(engine_booms)
        self.assertEqual(self.active_count(), 0, "slot leaked on the EngineError path")
        self.assertEqual(self.staging_root_is_clean(), [])
        self.assertIn("download failed", event.last_answer.lower())
        self.assertIn("streamrip exited 1", event.last_answer)

    async def test_slot_released_and_staging_removed_on_unexpected_exception(self):
        self.enable_qobuz()

        async def weird_failure(platform, url, kind, workdir):
            (workdir / "half.flac").write_bytes(b"yy")
            raise ValueError("something never seen before")

        event = await self._run_release(weird_failure)
        self.assertEqual(self.active_count(), 0, "slot leaked on the exception path")
        self.assertEqual(self.staging_root_is_clean(), [])
        self.assertIn("something never seen before", event.last_answer)

    async def test_progress_placeholder_is_deleted_but_the_error_stays(self):
        self.enable_qobuz()

        async def engine_booms(platform, url, kind, workdir):
            raise EngineError("nope")

        event = await self._run_release(engine_booms)
        # answers[0] is the "downloading" placeholder: it must be cleared so the
        # chat is not left showing a spinner that never resolves. answers[1] is
        # the failure notice and must survive — deleting it would leave the user
        # with no explanation at all.
        self.assertEqual(len(event.answers), 2, event.answers)
        self.assertTrue(event.messages[0].deleted, "placeholder left behind")
        self.assertFalse(event.messages[1].deleted, "error reply was deleted")

    async def test_metadata_failure_does_not_abort_the_download(self):
        # The bridge is banned in this suite, so its failure is guaranteed here.
        # A dead metadata service must degrade to the engine's own reading of the
        # file tree, not abort a download the user already waited for.
        self.enable_qobuz()

        async def ok(platform, url, kind, workdir):
            return _Result(platform, url, workdir)

        await self._run_release(ok)
        self.assertEqual(self.active_count(), 0)
        self.assertEqual(self.staging_root_is_clean(), [])
        self.assertEqual(len(self.delivered), 1, "delivery never happened")

    async def test_a_second_user_can_still_get_a_slot_afterwards(self):
        # The practical consequence of a leak: the bot wedges after one failure.
        self.enable_qobuz()

        async def engine_booms(platform, url, kind, workdir):
            raise EngineError("transient upstream 500")

        await self._run_release(engine_booms)
        slot = utils.request_download(999)
        try:
            self.assertTrue(slot.granted, "scheduler wedged after a single failure")
        finally:
            utils.release_download(slot)


class _Track:
    def __init__(self, path, number=1, artist="Test Artist", title="Song 1"):
        self.path = path
        self.number = number
        self.artist = artist
        self.title = title
        self.duration = 180
        self.track_id = None
        self.isrc = None


class _Result:
    """Stand-in for provider_engines.DownloadResult."""

    def __init__(self, platform, url, workdir, track_count=1):
        from dl_utils.provider_engines import ReleaseMetadata

        self.platform = platform
        self.url = url
        self.tracks = [_Track(workdir / "01 - Song.flac")]
        self.metadata = ReleaseMetadata(title="Midnight Static", artist="Test Artist")
        self.track_count = track_count
        self.provider = "token"
        self.errors = []
        self.workdir = workdir


class TestConcurrencyContractShape(StreamingCaseBase):
    async def test_a_busy_user_is_told_to_wait_and_no_slot_is_double_taken(self):
        self.enable_qobuz()
        first = utils.request_download(4242)
        self.assertTrue(first.granted)
        try:
            link = st.parse_link("https://open.qobuz.com/track/foobaz123")
            event = _FakeEvent(user_id=4242)
            await st._handle_release(event, link)
            self.assertIn("already", event.last_answer.lower())
            # The refused second request must not have disturbed the first slot.
            self.assertEqual(self.active_count(), 1)
            self.assertIs(utils._active_slots[(4242,)], first)
        finally:
            utils.release_download(first)

    async def test_reply_keyboard_is_removed_when_refusing_a_busy_user(self):
        # Users in a forced-reply flow otherwise stay stuck in it.
        self.enable_qobuz()
        first = utils.request_download(4242)
        try:
            event = _FakeEvent(user_id=4242)
            await st._handle_release(event, st.parse_link("https://open.qobuz.com/track/x1"))
            self.assertIsNotNone(event.reply_markups[-1])
        finally:
            utils.release_download(first)


class TestEffectiveKindAndUrl(StreamingCaseBase):
    def test_apple_album_with_i_param_downloads_the_single_track(self):
        link = st.parse_link("https://music.apple.com/us/album/kid-amo/1578066229?i=1578066233")
        self.assertEqual(st._effective_kind(link), st.KIND_TRACK)
        self.assertEqual(st._download_url(link), link.url)
        self.assertIn("?i=1578066233", st._download_url(link))

    def test_apple_album_without_i_param_downloads_the_album(self):
        link = st.parse_link("https://music.apple.com/us/album/kid-amo/1578066229")
        self.assertEqual(st._effective_kind(link), st.KIND_ALBUM)
        self.assertEqual(st._download_url(link), link.url)

    def test_amazon_album_with_trackasin_downloads_the_single_track(self):
        link = st.parse_link(
            "https://music.amazon.com/albums/B08X1234AB?trackAsin=B08X5678ZY"
        )
        self.assertEqual(st._effective_kind(link), st.KIND_TRACK)
        self.assertEqual(
            st._download_url(link), "https://music.amazon.com/tracks/B08X5678ZY"
        )

    def test_plain_track_url_passes_through_untouched(self):
        link = st.parse_link("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC")
        self.assertEqual(st._effective_kind(link), st.KIND_TRACK)
        self.assertEqual(st._download_url(link), link.url)

    def test_apple_link_without_a_storefront_is_not_classified_offline(self):
        # Apple only publishes storefront-qualified URLs; a bare
        # /album/<slug>/<id> needs the redirect walk, not the offline pass.
        self.assertIsNone(
            st.parse_link("https://music.apple.com/album/kid-amo/1578066229?i=1578066233")
        )

    def test_download_url_preserves_apple_url_without_storefront(self):
        link = st.ResolvedLink(
            platform=pcfg.APPLE,
            kind=st.KIND_ALBUM,
            object_id="1578066229",
            url="https://music.apple.com/album/kid-amo/1578066229?i=1578066233",
            track_id="1578066233",
            storefront=None,
        )
        self.assertEqual(st._download_url(link), link.url)


class TestDiscographyLayout(StreamingCaseBase):
    """_download_discography_in_one_go must keep each release's own identity.

    A multi-release engine result is split back into individual releases.
    Passing workdir to infer_release_from_paths is what lets it distinguish a
    real release folder from a staging path component.
    """

    def _build_discography(self, workdir: Path) -> None:
        # Multi-release layout: <artist>/<Album> [Year]/NN - ....
        artist_dir = workdir / "Thelonious Monk"
        for album, tracks in (
            ("Brilliant Corners [1957]", ("01 - Brilliant Corners", "02 - Ba-Lue Bolivar Ba-Lues-A")),
            ("Monk's Dream [1963]", ("01 - Monk's Dream", "02 - Bright Mississipi")),
            ("Underground [1968]", ("01 - Underground", "02 - Is You Is Or Is You Ain't")),
        ):
            release_dir = artist_dir / album
            release_dir.mkdir(parents=True)
            for name in tracks:
                (release_dir / f"{name} - Thelonious Monk.flac").write_bytes(b"fLaC\x00\x00")
            (release_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0junk")

    async def _one_go(self, workdir: Path):
        from dl_utils.provider_engines import EngineResult, ReleaseMetadata

        link = st.parse_link("https://open.qobuz.com/artist/12345")
        self.assertEqual(link.platform, pcfg.QOBUZ)
        self.assertEqual(link.kind, st.KIND_ARTIST)

        async def fake_download_release(platform, url, kind, wd):
            return EngineResult(
                metadata=ReleaseMetadata(
                    artist="Thelonious Monk",
                    title="Thelonious Monk",
                    album_title="Thelonious Monk",
                ),
                tracks=[],
                mode="direct",
                platform=platform,
                provider="streamrip",
                workdir=str(wd),
            )

        with mock.patch.object(st, "download_release", fake_download_release):
            return await st._download_discography_in_one_go(pcfg.QOBUZ, link, workdir)

    async def test_each_release_keeps_its_own_album_title_and_year(self):
        workdir = self.tmpdir / "qobuz" / "artist" / "12345"
        workdir.mkdir(parents=True)
        self._build_discography(workdir)

        downloaded = await self._one_go(workdir)

        self.assertEqual(len(downloaded), 3, "one entry per release folder")
        by_title = {release["title"]: metadata for release, metadata, _ in downloaded}
        self.assertEqual(
            set(by_title), {"Brilliant Corners", "Monk's Dream", "Underground"}
        )
        self.assertEqual(by_title["Brilliant Corners"].year, "1957")
        self.assertEqual(by_title["Monk's Dream"].year, "1963")
        self.assertEqual(by_title["Underground"].year, "1968")
        for release, metadata, tracks in downloaded:
            with self.subTest(release=release["title"]):
                self.assertNotEqual(metadata.album_title, "Unknown Album")
                self.assertNotEqual(metadata.title, "Unknown Album")
                self.assertEqual(metadata.artist, "Thelonious Monk")
                self.assertEqual(metadata.kind, st.KIND_ALBUM)
                self.assertEqual(len(tracks), 2)

    async def test_a_flat_tree_is_delivered_as_one_entry_not_split(self):
        # discover_releases groups by parent directory; a single folder must not
        # be invented apart, so the whole result passes through untouched.
        workdir = self.tmpdir / "qobuz" / "artist" / "flat"
        workdir.mkdir(parents=True)
        (workdir / "01 - Thelonious Monk - Round Midnight.flac").write_bytes(b"fLaC\x00\x00")
        (workdir / "02 - Thelonious Monk - Blue Monk.flac").write_bytes(b"fLaC\x00\x00")

        link = st.parse_link("https://open.qobuz.com/artist/12345")
        from dl_utils.provider_engines import EngineResult, ReleaseMetadata

        sentinel = ReleaseMetadata(artist="Thelonious Monk", title="The Complete Riverside")
        tracks = ["t1", "t2"]

        async def fake_download_release(platform, url, kind, wd):
            return EngineResult(
                metadata=sentinel, tracks=tracks, mode="direct",
                platform=platform, provider="streamrip", workdir=str(wd),
            )

        with mock.patch.object(st, "download_release", fake_download_release):
            downloaded = await st._download_discography_in_one_go(pcfg.QOBUZ, link, workdir)

        self.assertEqual(len(downloaded), 1)
        self.assertIs(downloaded[0][1], sentinel)
        self.assertEqual(downloaded[0][2], tracks)


class TestRouterFilter(StreamingCaseBase):
    async def test_itunes_apple_com_links_are_claimed_by_this_router(self):
        # link_resolver accepts both music.apple.com and itunes.apple.com, but
        # CLAIM_HOSTS only listed the former. An itunes.apple.com link therefore
        # matched no router at all: the bot stayed silent, which looks exactly
        # like a dropped message rather than a missing host.
        f = st._StreamingLinkFilter()
        for url in (
            "https://itunes.apple.com/us/album/id1578066229",
            "https://itunes.apple.com/us/album/midnight-static/1578066229?i=1578066229",
            "https://music.apple.com/us/album/id1578066229",
        ):
            with self.subTest(url=url):
                self.assertTrue(await f(_FakeEvent(text=url)))
        self.assertTrue(st.ANY_LINK_RE.match("https://itunes.apple.com/us/album/id1"))

    async def test_itunes_apple_com_classifies_to_the_apple_platform(self):
        link = st.parse_link("https://itunes.apple.com/us/album/midnight-static/1578066229?i=1578066230")
        self.assertIsNotNone(link)
        self.assertEqual(link.platform, pcfg.APPLE)
        self.assertEqual(link.storefront, "us")
        # ?i= on an album link means "just that song", not the whole album.
        self.assertTrue(link.is_single_track)
        self.assertEqual(link.track_id, "1578066230")

    async def test_an_itunes_track_link_reaches_the_apple_handler(self):
        # End to end through the router's own decision path: the concrete
        # per-platform filter must claim it, otherwise the update falls through
        # every router and the bot says nothing.
        url = "https://itunes.apple.com/us/album/midnight-static/1578066229"
        link = st.parse_link(url)
        self.assertEqual(link.platform, pcfg.APPLE)
        self.assertTrue(await st._PlatformLinkFilter(pcfg.APPLE)(_FakeEvent(text=url)))
        self.assertFalse(await st._PlatformLinkFilter(pcfg.QOBUZ)(_FakeEvent(text=url)))

    async def test_an_unreadable_apple_shape_falls_through_without_a_slot(self):
        # music.apple.com/us/album/<id> with no slug is not a shape classify()
        # reads. It must not take a slot or raise: the shortlink fallback tries
        # once more and returns, which is the pre-existing behaviour for every
        # unrecognised canonical shape.
        self.set_env(APPLE_ENABLED="1")
        event = _FakeEvent(text="https://music.apple.com/us/album/1578066229")
        await st.handle_streaming_shortlink(event)
        self.assertEqual(self.active_count(), 0)
        self.assertIsNone(st.parse_link("https://music.apple.com/us/album/1578066229"))

    async def test_deezer_links_never_match_this_router(self):
        # Deezer has its own router; stealing the update would break it.
        f = st._StreamingLinkFilter()
        for url in (
            "https://www.deezer.com/album/98765",
            "https://deezer.page.link/abc",
            "https://dzr.page.link/abc",
        ):
            with self.subTest(url=url):
                self.assertFalse(await f(_FakeEvent(text=url)))

    async def test_youtube_and_soundcloud_never_match(self):
        f = st._StreamingLinkFilter()
        for url in (
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://soundcloud.com/artist/track",
        ):
            with self.subTest(url=url):
                self.assertFalse(await f(_FakeEvent(text=url)))

    async def test_a_link_with_a_caption_still_matches(self):
        f = st._PlatformLinkFilter(pcfg.SPOTIFY)
        message = _FakeEvent(text="grab this one plz https://open.spotify.com/track/abc123")
        self.assertTrue(await f(message))

    async def test_plain_text_does_not_match(self):
        f = st._PlatformLinkFilter(pcfg.SPOTIFY)
        self.assertFalse(await f(_FakeEvent(text="what do you think of jazz?")))
        self.assertFalse(await f(_FakeEvent(text="")))
        self.assertFalse(await f(_FakeEvent(text=None)))

    async def test_non_streaming_platform_never_matches_its_own_filter(self):
        # The filter is per-platform: a Tidal link must not fire the Qobuz handler.
        f = st._PlatformLinkFilter(pcfg.QOBUZ)
        self.assertFalse(await f(_FakeEvent(text="https://tidal.com/album/123456789")))


if __name__ == "__main__":
    unittest.main()
