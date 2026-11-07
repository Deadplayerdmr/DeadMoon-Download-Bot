"""Offline checks for shortlink expansion, against a real local HTTP server.

These exercise the actual aiohttp redirect-following code in
dl_utils/link_resolver.py rather than mocking the client: a local test server
issues genuine 301/302 responses, so HEAD-vs-GET fallback, hop counting and
chained-wrapper handling are all really tested. Nothing here leaves the box.

Run with `python3 -m unittest discover tests`.
"""

import asyncio
import os
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import quote, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from dl_utils import link_resolver as lr  # noqa: E402


def _routes(base_for):
    """The redirect graph the tests walk. Paths are absolute URLs by construction."""

    def _target(request):
        # The {target} capture group is the percent-encoded absolute URL; aiohttp
        # decodes path segments once, so this is already the plain URL.
        return unquote(request.match_info["target"])

    async def hop(request):
        raise web.HTTPFound(location=_target(request))

    async def permanent(request):
        raise web.HTTPMovedPermanently(location=_target(request))

    async def head_only_rejects(request):
        """A wrapper that answers GET but 405s HEAD, like several link shorteners."""
        target = _target(request)
        if request.method == "HEAD":
            raise web.HTTPMethodNotAllowed(method="HEAD", allowed_methods=("GET",))
        raise web.HTTPFound(location=target)

    async def loop_a(request):
        # A genuine two-node cycle: only the hop counter can break this.
        raise web.HTTPFound(location=base_for("/loop/b"))

    async def loop_b(request):
        raise web.HTTPFound(location=base_for("/loop/a"))

    async def dead(request):
        raise web.HTTPNotFound()

    async def plain(request):
        return web.Response(text="no redirect here")

    return [
        ("/hop/{target:.+}", hop),
        ("/perm/{target:.+}", permanent),
        ("/nohead/{target:.+}", head_only_rejects),
        ("/loop/a", loop_a),
        ("/loop/b", loop_b),
        ("/dead", dead),
        ("/plain", plain),
    ]


# resolve_url() only follows redirects for hosts in the wrapper table, so a
# loopback URL would be handed straight back without any network call. Widening
# the table to cover 127.0.0.1 exercises the real walk — hop counting, the
# HEAD->GET fallback, chained wrappers, query stripping — against a real server,
# while CANONICAL_HOST_REGEX stays exactly as shipped, so reaching a genuine
# platform host is still what terminates the loop.
_LOCAL_WRAPPER_REGEX = re.compile(
        # The ephemeral port has to be part of the pattern: the shipped regexes all
        # match on host only, and aiohttp's TestServer binds a random one.
        r"^https?://(?:www\.)?127\.0\.0\.1(?::\d+)?/(?P<rest>.*)$",
        re.IGNORECASE,
    )


class ResolutionServerCase(unittest.TestCase):
    """Boots a real HTTP server on localhost for the duration of the class."""

    @classmethod
    def setUpClass(cls):
        cls._saved_regex = lr.SHORTLINK_HOST_REGEX
        lr.SHORTLINK_HOST_REGEX = _LOCAL_WRAPPER_REGEX
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)

        async def boot():
            holder = {}

            def base_for(path):
                return f"http://127.0.0.1:{holder['server'].port}{path}"

            app = web.Application()
            for pattern, handler in _routes(base_for):
                app.router.add_route("*", pattern, handler)
            server = TestServer(app)
            holder["server"] = server
            await server.start_server()
            return server

        cls.server = cls.loop.run_until_complete(boot())

    @classmethod
    def tearDownClass(cls):
        async def shutdown():
            await cls.server.close()

        cls.loop.run_until_complete(shutdown())
        asyncio.set_event_loop(None)
        cls.loop.close()
        lr.SHORTLINK_HOST_REGEX = cls._saved_regex

    def url(self, path):
        return f"http://127.0.0.1:{self.server.port}{path}"

    def target(self, path):
        return quote(self.url(path), safe="")

    def resolve(self, url):
        return self.loop.run_until_complete(lr.resolve_url(url))

    def resolve_link(self, url):
        return self.loop.run_until_complete(lr.resolve_link(url))


class TestExpansion(ResolutionServerCase):
    def test_single_hop(self):
        final = self.resolve(self.url(f"/hop/{self.target('/plain')}"))
        self.assertEqual(final, self.url("/plain"))

    def test_permanent_redirect(self):
        final = self.resolve(self.url(f"/perm/{self.target('/plain')}"))
        self.assertEqual(final, self.url("/plain"))

    def test_chained_wrappers_are_walked_to_the_end(self):
        middle = self.url(f"/hop/{self.target('/plain')}")
        outer = self.url(f"/hop/{quote(middle, safe='')}")
        self.assertEqual(self.resolve(outer), self.url("/plain"))

    def test_head_rejection_falls_back_to_get(self):
        final = self.resolve(self.url(f"/nohead/{self.target('/plain')}"))
        self.assertEqual(final, self.url("/plain"))

    def test_redirect_loop_terminates_instead_of_hanging(self):
        # Two independent bounds keep this from spinning forever and pinning a
        # download slot with it: _MAX_HOPS (6) caps the wrapper chain this module
        # walks, and aiohttp's own max_redirects (10) caps redirects within a
        # single walk. A self-cycling URL hits the latter, which is still a
        # ClientError the handler already reports as an unresolvable link.
        from aiohttp import TooManyRedirects

        with self.assertRaises(TooManyRedirects):
            self.resolve(self.url("/loop/a"))

    def test_a_chain_within_the_hop_budget_is_fully_walked(self):
        # Three nested wrappers is under both bounds, so the walk completes and
        # returns the final local target rather than an intermediate hop.
        inner = self.url("/plain")
        middle = self.url(f"/hop/{quote(inner, safe='')}")
        outer = self.url(f"/hop/{quote(middle, safe='')}")
        self.assertEqual(self.resolve(outer), inner)

    def test_dead_link_comes_back_unchanged_and_unclassified(self):
        # aiohttp does not raise for status by default, so a 404 comes back as the
        # unchanged URL. classify() then rejects it and the handler tells the user
        # the link could not be resolved, rather than downloading garbage.
        dead = self.url("/dead")
        self.assertEqual(self.resolve(dead), dead)
        self.assertIsNone(lr.classify(dead))

    def test_empty_url_is_a_value_error(self):
        for blank in ("", "   ", None):
            with self.subTest(blank=blank), self.assertRaises(ValueError):
                self.loop.run_until_complete(lr.resolve_url(blank))


class TestCanonicalBypass(ResolutionServerCase):
    """A canonical URL must not cost an HTTP round trip."""

    def test_canonical_urls_are_returned_without_network(self):
        for url in (
            "https://www.deezer.com/album/98765",
            "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
            "https://tidal.com/browse/album/12345678",
            "https://music.apple.com/us/album/kid-amo/1578066229",
            "https://music.amazon.com/tracks/B08X1234AB",
            "https://open.qobuz.com/track/foobaz123",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.resolve(url), url)

    def test_tracking_is_stripped_on_the_bypass_path_too(self):
        url = "https://open.spotify.com/track/abc123?si=XYZ&utm_source=sh"
        self.assertEqual(self.resolve(url), "https://open.spotify.com/track/abc123")


class TestResolveLink(unittest.IsolatedAsyncioTestCase):
    """Composition of resolve_url + classify.

    The redirect walk itself is covered against a live local server by
    TestExpansion. Here resolve_url is stood in for, because a local 302 whose
    target is a real platform hostname makes aiohttp follow the chain off the
    box and onto the live internet — these tests stay hermetic by construction.
    """

    async def _link_for(self, final_url, sent_url="https://bit.ly/xyz"):
        async def canned(_url):
            return final_url

        original = lr.resolve_url
        lr.resolve_url = canned
        try:
            return await lr.resolve_link(sent_url)
        finally:
            lr.resolve_url = original

    async def test_resolved_url_is_classified_and_the_original_is_kept(self):
        sent = "https://bit.ly/xyz"
        link = await self._link_for("https://www.deezer.com/album/98765", sent)
        self.assertIsNotNone(link)
        self.assertEqual(link.platform, lr.PLATFORM_DEEZER)
        self.assertEqual(link.kind, lr.KIND_ALBUM)
        self.assertEqual(link.object_id, "98765")
        # The link the user actually sent is kept, for logging only.
        self.assertEqual(link.original_url, sent)

    async def test_unclassifiable_target_yields_none(self):
        self.assertIsNone(await self._link_for("http://127.0.0.1:9999/plain"))

    async def test_apple_album_with_song_parameter_is_a_single_track(self):
        link = await self._link_for(
            "https://music.apple.com/us/album/kid-amo/1578066229?i=1578066233"
        )
        self.assertEqual(link.platform, lr.PLATFORM_APPLE)
        self.assertEqual(link.kind, lr.KIND_ALBUM)
        self.assertEqual(link.track_id, "1578066233")
        self.assertTrue(link.is_single_track)

    async def test_blank_final_url_yields_none(self):
        self.assertIsNone(await self._link_for(""))


class TestHostRegexes(unittest.TestCase):
    """The host tables are pure data; check them without a server."""

    def test_every_documented_wrapper_host_is_recognised(self):
        for host in (
            "https://deezer.page.link/x",
            "https://dzr.page.link/x",
            "https://link.deezer.com/x",
            "https://spotify.link/x",
            "https://spoti.fi/x",
            "https://t.idal.com/x",
            "https://tidal.page.link/x",
            "https://amzn.to/x",
            "https://a.co/x",
            "https://apple.co/x",
            "https://on.soundcloud.com/x",
            "https://soundcloud.app.goo.gl/x",
            "https://app.goo.gl/x",
            "https://goo.gl/x",
            "https://bit.ly/x",
            "https://t.co/x",
        ):
            with self.subTest(host=host):
                self.assertTrue(lr.is_shortlink(host))

    def test_scheme_and_case_insensitive(self):
        self.assertTrue(lr.is_shortlink("HTTPS://BIT.LY/AbC"))
        self.assertTrue(lr.is_shortlink("http://bit.ly/AbC"))

    def test_wrapper_prefix_is_not_enough(self):
        # A host that merely starts with a wrapper name is not a wrapper.
        for url in (
            "https://bit.ly.example.com/x",
            "https://notspoti.fi/x",
            "https://apple.co.evil.tld/x",
        ):
            with self.subTest(url=url):
                self.assertFalse(lr.is_shortlink(url))

    def test_youtube_share_links_are_canonical_not_wrappers(self):
        # youtu.be is a short domain but classify() handles it directly, so
        # treating it as a wrapper would waste a round trip.
        self.assertFalse(lr.is_shortlink("https://youtu.be/dQw4w9WgXcQ"))
        link = lr.classify("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(link.platform, lr.PLATFORM_YOUTUBE)
        self.assertEqual(link.object_id, "dQw4w9WgXcQ")

    def test_ssl_context_has_a_ca_bundle(self):
        import ssl

        ctx = lr._ssl_context()
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertTrue(ctx.cert_store_stats().get("x509_ca", 0) > 0)


if __name__ == "__main__":
    unittest.main()
