"""Universal shortlink and mobile app-link resolver.

Two jobs, both of which have to happen before a URL reaches a handler:

1. **Expand** whatever the user actually pasted. Mobile share sheets hand out
   wrapping links (`spotify.link`, `amzn.to`, `apple.co`, `tidal.page.link`,
   ...) rather than the platform's own URL, so the real destination is only
   known after following the redirects.

2. **Normalise** the expanded URL into one canonical shape per platform, with
   tracking query parameters stripped. Handlers match on regexes; every extra
   `?utm_source=...` or `?si=...` variant that has to be tolerated is another
   branch in another regex.

The resolved URL keeps the query parameters that carry meaning:

* Apple Music `?i=<songId>` on an `/album/<slug>/<albumId>` link — that is the
  difference between "this album" and "this one track from that album".
* Amazon Music `?trackAsin=<asin>` on an `/albums/<asin>` link — same idea.

Everything else in the query string is tracking or affiliate noise and is
dropped.

Resolution is a single `HEAD` request with redirects followed, mirroring
`handlers.deezer.handle_shortlink`. If a host answers `HEAD` with 405 or 403 —
some link wrappers only implement `GET` — the request is retried once with
`GET` and no body read.
"""

import asyncio
import re
import ssl
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
import certifi

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

PLATFORM_DEEZER = "deezer"
PLATFORM_QOBUZ = "qobuz"
PLATFORM_TIDAL = "tidal"
PLATFORM_AMAZON = "amazon"
PLATFORM_SPOTIFY = "spotify"
PLATFORM_APPLE = "apple"
PLATFORM_YOUTUBE = "yt"
PLATFORM_SOUNDCLOUD = "sc"

KIND_TRACK = "track"
KIND_ALBUM = "album"
KIND_ARTIST = "artist"
KIND_PLAYLIST = "playlist"
KIND_UNKNOWN = "unknown"

# Query parameters that change what a link means and must survive normalisation.
_MEANINGFUL_QUERY_KEYS = frozenset({"i", "trackasin", "albumasin"})

# Parameters that never do. `TRACKING_QUERY_PREFIXES` are matched as prefixes
# (so every utm_* flavour falls out); `TRACKING_QUERY_EXACT` is matched exactly
# so a short key like `si` never swallows an unrelated parameter starting
# with the same letters.
_STRIPPED_QUERY_PREFIXES = ("utm_", "ref_", "igsh", "mc_", "piwik", "matomo")
_TRACKING_QUERY_EXACT = frozenset(
    {
        "si", "t", "feature", "share", "locale", "lang", "cid", "ts", "app",
        "nd", "igshid", "fbclid", "gclid", "msclkid", "dpl", "dl_branch",
        "at", "ign-pt", "pt", "ls", "s", "sk", "iu", "uo", "of",
    }
)


def _is_tracking_key(key: str) -> bool:
    k = key.lower()
    if k in _MEANINGFUL_QUERY_KEYS:
        return False
    return k.startswith(_STRIPPED_QUERY_PREFIXES) or k in _TRACKING_QUERY_EXACT


@dataclass
class ResolvedLink:
    """A canonical platform URL plus the identity of the object it points at."""

    platform: str
    kind: str
    object_id: str
    url: str
    # Apple `?i=` / Amazon `?trackAsin=`: the *track* id carried by an album URL.
    # When set, the request is a single-track download even though kind is album.
    track_id: str | None = None
    storefront: str | None = None
    original_url: str = ""
    query: dict = field(default_factory=dict)

    @property
    def is_single_track(self) -> bool:
        return self.kind == KIND_TRACK or self.track_id is not None


# ---------------------------------------------------------------------------
# Shortlink hosts that must be expanded before they can be classified
# ---------------------------------------------------------------------------

SHORTLINK_HOSTS = (
    # Deezer
    r"deezer\.page\.link",
    r"dzr\.page\.link",
    r"link\.deezer\.com",
    # Spotify
    r"spotify\.link",
    r"spoti\.fi",
    # Tidal
    r"t\.idal\.com",
    r"tidal\.page\.link",
    # Amazon
    r"amzn\.to",
    r"a\.co",
    # Apple
    r"apple\.co",
    # SoundCloud
    r"on\.soundcloud\.com",
    r"soundcloud\.app\.goo\.gl",
    # Generic mobile-app wrappers
    r"app\.goo\.gl",
    r"goo\.gl",
    r"bit\.ly",
    r"t\.co",
)

SHORTLINK_HOST_REGEX = re.compile(
    r"^https?://(?:www\.)?(?:" + "|".join(SHORTLINK_HOSTS) + r")/(?P<rest>.*)$",
    re.IGNORECASE,
)

# A link is "already canonical" if it points at one of these hosts directly.
CANONICAL_HOST_REGEX = re.compile(
    r"^https?://(?:www\.)?("
    r"deezer\.com|api\.deezer\.com"
    r"|qobuz\.com|open\.qobuz\.com|play\.qobuz\.com"
    r"|tidal\.com|listen\.tidal\.com"
    r"|music\.amazon\.[a-z.]+|amazon\.[a-z.]+/music"
    r"|open\.spotify\.com|spotify\.com"
    r"|music\.apple\.com|itunes\.apple\.com"
    r"|music\.youtube\.com|youtu\.be|youtube\.com|www\.youtube\.com|m\.youtube\.com"
    r"|soundcloud\.com"
    r")(/|\?|$)",
    re.IGNORECASE,
)


def is_shortlink(url: str) -> bool:
    """True when `url` is a wrapping link that has to be resolved first."""
    url = (url or "").strip()
    if not url:
        return False
    if CANONICAL_HOST_REGEX.match(url):
        return False
    return bool(SHORTLINK_HOST_REGEX.match(url))


# First bare URL in a message. Users paste a link with a caption more often
# than not, so every entry point that inspects message text goes through this.
_URL_IN_TEXT_REGEX = re.compile(r"https?://\S+", re.I)


def extract_url(text: str) -> str:
    """The first http(s) URL in `text`, or "" when there is none."""
    match = _URL_IN_TEXT_REGEX.search(text or "")
    return match.group(0).rstrip(").,;'\"") if match else ""


# ---------------------------------------------------------------------------
# Query handling
# ---------------------------------------------------------------------------
def strip_tracking_params(url: str) -> str:
    """Drop tracking query parameters, keeping the ones that carry meaning."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not _is_tracking_key(k)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept), "")
    )


# ---------------------------------------------------------------------------
# Classification of canonical URLs
# ---------------------------------------------------------------------------
# Apple serves both music.apple.com and itunes.apple.com with the same path
# shape, and the locale segment is either a bare storefront code (`us`) or a
# full locale (`en-us`, `pt-BR`), so both are matched as fragments below rather
# than repeated six times per host.
_APPLE_HOST = r"(?:music|itunes)\.apple\.com"
_APPLE_LOCALE = r"[a-z]{2}(?:[-_][a-z0-9]{2,4})?"

# Each entry: (platform, kind, compiled regex). Group 1 is the object id.
_CLASSIFIERS: list[tuple[str, str, re.Pattern]] = [
    # Deezer: /track/123, /album/123, /artist/123, with optional locale segment
    # (both `fr/` and `en-us/` / `en_intl/` flavours occur in the wild).
    (
        PLATFORM_DEEZER,
        KIND_TRACK,
        re.compile(
            r"^https?://(?:www\.)?deezer\.com/(?:[a-z]{2}(?:[-_][a-z0-9]+)?/)?track/(\d+)", re.I
        ),
    ),
    (
        PLATFORM_DEEZER,
        KIND_ALBUM,
        re.compile(
            r"^https?://(?:www\.)?deezer\.com/(?:[a-z]{2}(?:[-_][a-z0-9]+)?/)?album/(\d+)", re.I
        ),
    ),
    (
        PLATFORM_DEEZER,
        KIND_ARTIST,
        re.compile(
            r"^https?://(?:www\.)?deezer\.com/(?:[a-z]{2}(?:[-_][a-z0-9]+)?/)?artist/(\d+)", re.I
        ),
    ),
    (
        PLATFORM_DEEZER,
        KIND_PLAYLIST,
        re.compile(
            r"^https?://(?:www\.)?deezer\.com/(?:[a-z]{2}(?:[-_][a-z0-9]+)?/)?playlist/(\d+)", re.I
        ),
    ),
    # Qobuz: open.qobuz.com | play.qobuz.com | www.qobuz.com/<locale>/album/<slug>/<upc>
    (
        PLATFORM_QOBUZ,
        KIND_TRACK,
        re.compile(
            r"^https?://(?:(?:www|open|play)\.)?qobuz\.com/"
            r"(?:[a-z]{2}(?:[_-][a-z0-9]+)?/|worldwide/)?track/([A-Za-z0-9]+)",
            re.I,
        ),
    ),
    # The www form carries a human slug followed by a numeric or alphanumeric
    # album id, so it has to be tried before the generic single-segment form.
    (
        PLATFORM_QOBUZ,
        KIND_ALBUM,
        re.compile(
            r"^https?://(?:(?:www|open|play)\.)?qobuz\.com/(?:[^/]+/){0,3}"
            r"album/[^/]+/([A-Za-z0-9]+)(?:[/?#]|$)",
            re.I,
        ),
    ),
    (
        PLATFORM_QOBUZ,
        KIND_ALBUM,
        re.compile(
            r"^https?://(?:(?:www|open|play)\.)?qobuz\.com/"
            r"(?:[a-z]{2}(?:[_-][a-z0-9]+)?/|worldwide/)?album/([A-Za-z0-9\-]+)",
            re.I,
        ),
    ),
    (
        PLATFORM_QOBUZ,
        KIND_ARTIST,
        re.compile(
            r"^https?://(?:(?:www|open|play)\.)?qobuz\.com/(?:[^/]+/){0,3}"
            r"artist/[^/]+/([A-Za-z0-9]+)(?:[/?#]|$)",
            re.I,
        ),
    ),
    (
        PLATFORM_QOBUZ,
        KIND_ARTIST,
        re.compile(
            r"^https?://(?:(?:www|open|play)\.)?qobuz\.com/"
            r"(?:[a-z]{2}(?:[_-][a-z0-9]+)?/|worldwide/)?artist/([A-Za-z0-9\-]+)",
            re.I,
        ),
    ),
    # Tidal: tidal.com/track/123, tidal.com/browse/album/123, listen.tidal.com/artist/123
    (
        PLATFORM_TIDAL,
        KIND_TRACK,
        re.compile(r"^https?://(?:(?:www|listen)\.)?tidal\.com/(?:browse/)?track/(\d+)", re.I),
    ),
    (
        PLATFORM_TIDAL,
        KIND_ALBUM,
        re.compile(r"^https?://(?:(?:www|listen)\.)?tidal\.com/(?:browse/)?album/(\d+)", re.I),
    ),
    (
        PLATFORM_TIDAL,
        KIND_ARTIST,
        re.compile(r"^https?://(?:(?:www|listen)\.)?tidal\.com/(?:browse/)?artist/(\d+)", re.I),
    ),
    (
        PLATFORM_TIDAL,
        KIND_PLAYLIST,
        re.compile(r"^https?://(?:(?:www|listen)\.)?tidal\.com/(?:browse/)?playlist/([A-Za-z0-9\-]+)", re.I),
    ),
    # Amazon Music: music.amazon.tld/{albums,tracks,artists}/<asin>
    (
        PLATFORM_AMAZON,
        KIND_TRACK,
        re.compile(r"^https?://music\.amazon\.[a-z.]+/tracks/([A-Z0-9]{10})", re.I),
    ),
    (
        PLATFORM_AMAZON,
        KIND_ALBUM,
        re.compile(r"^https?://music\.amazon\.[a-z.]+/albums/([A-Z0-9]{10})", re.I),
    ),
    (
        PLATFORM_AMAZON,
        KIND_ARTIST,
        re.compile(r"^https?://music\.amazon\.[a-z.]+/artists/([A-Z0-9.\-]+)", re.I),
    ),
    # Spotify: open.spotify.com/track/ID, with optional /intl-xx/ segment
    (
        PLATFORM_SPOTIFY,
        KIND_TRACK,
        re.compile(r"^https?://open\.spotify\.com/(?:intl-[a-z\-]+/)?track/([A-Za-z0-9]+)", re.I),
    ),
    (
        PLATFORM_SPOTIFY,
        KIND_ALBUM,
        re.compile(r"^https?://open\.spotify\.com/(?:intl-[a-z\-]+/)?album/([A-Za-z0-9]+)", re.I),
    ),
    (
        PLATFORM_SPOTIFY,
        KIND_ARTIST,
        re.compile(r"^https?://open\.spotify\.com/(?:intl-[a-z\-]+/)?artist/([A-Za-z0-9]+)", re.I),
    ),
    (
        PLATFORM_SPOTIFY,
        KIND_PLAYLIST,
        re.compile(r"^https?://open\.spotify\.com/(?:intl-[a-z\-]+/)?playlist/([A-Za-z0-9]+)", re.I),
    ),
    # Apple Music / iTunes. Storefront codes are two letters (`us`, `gb`) but
    # locale-prefixed forms (`en-us`, `pt-BR`) appear in shared links too, so the
    # segment is matched loosely rather than as `[a-z]{2}(-[A-Z]{2})?`.
    #
    # Ordering matters: `/album/<slug>/<id>` is an album even when a `?i=`
    # parameter is present, and `/song/<slug>/<id>` is a track. The `?i=` case is
    # resolved in classify(), which records the song id on `track_id` so the
    # handler downloads one track out of that album.
    (
        PLATFORM_APPLE,
        KIND_TRACK,
        re.compile(
            rf"^https?://{_APPLE_HOST}/{_APPLE_LOCALE}/(?:song|track)/[^/]*/([0-9]+)(?:[/?#]|$)", re.I
        ),
    ),
    (
        PLATFORM_APPLE,
        KIND_ALBUM,
        re.compile(
            rf"^https?://{_APPLE_HOST}/{_APPLE_LOCALE}/album/[^/]*/([0-9]+)(?:[/?#]|$)", re.I
        ),
    ),
    (
        PLATFORM_APPLE,
        KIND_ARTIST,
        re.compile(
            rf"^https?://{_APPLE_HOST}/{_APPLE_LOCALE}/artist/(?:[^/]+/)?([0-9]+)(?:[/?#]|$)", re.I
        ),
    ),
    (
        PLATFORM_APPLE,
        KIND_PLAYLIST,
        re.compile(
            rf"^https?://{_APPLE_HOST}/{_APPLE_LOCALE}/playlist/[^/]*/([pt][0-9]+)(?:[/?#]|$)", re.I
        ),
    ),
    # YouTube
    (
        PLATFORM_YOUTUBE,
        KIND_TRACK,
        re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/watch\?v=([A-Za-z0-9_\-]{11})", re.I),
    ),
    (
        PLATFORM_YOUTUBE,
        KIND_TRACK,
        re.compile(r"^https?://youtu\.be/([A-Za-z0-9_\-]{11})", re.I),
    ),
    (
        PLATFORM_YOUTUBE,
        KIND_PLAYLIST,
        re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/playlist\?list=([A-Za-z0-9_\-]+)", re.I),
    ),
    (
        PLATFORM_YOUTUBE,
        KIND_ARTIST,
        re.compile(r"^https?://music\.youtube\.com/(?:channel|artist)/([A-Za-z0-9_\-\.]+)", re.I),
    ),
    # SoundCloud
    (
        PLATFORM_SOUNDCLOUD,
        KIND_TRACK,
        re.compile(r"^https?://(m\.|www\.)?soundcloud\.com/[^/]+/[^/?#]+", re.I),
    ),
    (
        PLATFORM_SOUNDCLOUD,
        KIND_ARTIST,
        re.compile(r"^https?://(m\.|www\.)?soundcloud\.com/[^/?#]+$", re.I),
    ),
]

# Apple: /album/<slug>/<id>?i=<songId> — the album regex matches first, so the
# `i` parameter is read out of the query string afterwards rather than in the
# pattern itself.
_APPLE_STOREFRONT_REGEX = re.compile(
    rf"^https?://{_APPLE_HOST}/({_APPLE_LOCALE})/", re.I
)

# Amazon: /albums/<asin>?trackAsin=<asin>
_AMAZON_TRACK_ASIN_REGEX = re.compile(r"(?:^|&|;)trackAsin=([A-Z0-9]{10})", re.I)


def classify(url: str) -> ResolvedLink | None:
    """Identify platform + kind + id for an already-canonical URL."""
    if not url:
        return None
    cleaned = strip_tracking_params(url.strip())
    query = {k.lower(): v for k, v in parse_qsl(urlsplit(cleaned).query)}

    for platform, kind, regex in _CLASSIFIERS:
        match = regex.match(cleaned)
        if not match:
            continue
        object_id = match.group(1)

        track_id = None
        storefront = None

        if platform == PLATFORM_APPLE:
            match_locale = _APPLE_STOREFRONT_REGEX.match(cleaned)
            # `en-us` and `us` both occur; the storefront Apple's API wants is the
            # trailing code, so a full locale is reduced to it.
            storefront = match_locale.group(1).lower().rsplit("-", 1)[-1] if match_locale else None
            # ?i=<songId> on an album link means "just that song".
            if kind == KIND_ALBUM and query.get("i"):
                track_id = query["i"]
        elif platform == PLATFORM_AMAZON:
            if kind == KIND_ALBUM and query.get("trackasin"):
                track_id = query["trackasin"]
        elif platform == PLATFORM_SPOTIFY:
            cleaned = _normalize_spotify(cleaned)
        elif platform == PLATFORM_TIDAL:
            cleaned = _normalize_tidal(cleaned, kind, object_id)
        elif platform == PLATFORM_DEEZER:
            cleaned = _normalize_deezer(cleaned, kind, object_id)
        elif platform == PLATFORM_QOBUZ:
            cleaned = _normalize_qobuz(cleaned, kind, object_id)

        return ResolvedLink(
            platform=platform,
            kind=kind,
            object_id=object_id,
            url=cleaned,
            track_id=track_id,
            storefront=storefront,
            original_url=url.strip(),
            query=query,
        )
    return None


def _normalize_deezer(url: str, kind: str, object_id: str) -> str:
    return f"https://www.deezer.com/{kind}/{object_id}"


def _normalize_spotify(url: str) -> str:
    """Collapse /intl-xx/ and trailing query noise to open.spotify.com/<kind>/<id>."""
    match = re.match(
        r"^https?://open\.spotify\.com/(?:intl-[a-z\-]+/)?(track|album|artist|playlist)/([A-Za-z0-9]+)",
        url,
        re.I,
    )
    if not match:
        return url
    return f"https://open.spotify.com/{match.group(1).lower()}/{match.group(2)}"


def _normalize_tidal(url: str, kind: str, object_id: str) -> str:
    return f"https://tidal.com/{kind}/{object_id}"


def _normalize_qobuz(url: str, kind: str, object_id: str) -> str:
    return f"https://open.qobuz.com/{kind}/{object_id}"


# ---------------------------------------------------------------------------
# Redirect resolution
# ---------------------------------------------------------------------------
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_MAX_HOPS = 6
_TIMEOUT_TOTAL = 20


def _ssl_context() -> ssl.SSLContext:
    """certifi-backed context, matching handlers.deezer.handle_shortlink.

    The alpine image ships no system CA bundle that aiohttp finds on its own,
    so without this every HTTPS shortlink fails certificate verification.
    """
    return ssl.create_default_context(cafile=certifi.where())


async def _head_or_get(session: aiohttp.ClientSession, url: str) -> str:
    """Follow redirects from `url` and return the final absolute URL."""
    try:
        async with session.head(
            url,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_TOTAL),
            headers=_HEADERS,
        ) as response:
            if response.status in (403, 405, 501):
                raise aiohttp.ClientResponseError(
                    response.request_info, response.history, status=response.status
                )
            return str(response.url)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        # Some wrappers answer GET but reject HEAD. Re-walk with GET and read
        # no body; the redirect chain is in the response headers either way.
        async with session.get(
            url,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_TOTAL),
            headers=_HEADERS,
        ) as response:
            return str(response.url)


async def resolve_url(url: str) -> str:
    """Expand a shortlink to its final URL. A canonical URL is returned as-is.

    Raises aiohttp.ClientError / asyncio.TimeoutError on network failure; the
    caller owns the user-facing message.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("empty url")
    if not is_shortlink(url):
        return strip_tracking_params(url)

    connector = aiohttp.TCPConnector(ssl=_ssl_context())
    async with aiohttp.ClientSession(
        connector=connector, max_line_size=65536, max_field_size=65536
    ) as session:
        current = url
        seen = set()
        for _ in range(_MAX_HOPS):
            final = await _head_or_get(session, current)
            if final in seen:
                break
            seen.add(final)
            # A wrapper that chains into another wrapper (goo.gl -> spoti.fi ->
            # open.spotify.com) needs another pass; classify() rejects the
            # intermediate host, so keep walking until it does not.
            if CANONICAL_HOST_REGEX.match(final) or not SHORTLINK_HOST_REGEX.match(final):
                current = final
                break
            current = final
        return strip_tracking_params(current)


async def resolve_link(url: str) -> ResolvedLink | None:
    """Resolve and classify in one call. Returns None for unknown link shapes."""
    final = await resolve_url(url)
    link = classify(final)
    if link is None:
        return None
    link.original_url = (url or "").strip()
    return link


def parse_link(url: str) -> ResolvedLink | None:
    """Classify without any network call — canonical URLs only."""
    return classify(url)
