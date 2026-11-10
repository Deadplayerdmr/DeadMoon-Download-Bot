"""Best-effort public and configured-provider metadata resolution.

The download engines know how to get bytes; they are not a reliable source for
*what* those bytes are. A CLI tool writes whatever folder template it likes,
and its track list does not always match the platform the user linked. Release
metadata is resolved here first from public endpoints and configured provider
APIs:

* **iTunes Lookup** (`https://itunes.apple.com/lookup`) — Apple Music albums,
  songs and artists, with full tracklists and artwork URLs. No auth.
* **Deezer public API** (`https://api.deezer.com`) — albums, tracks, and
  `search/isrc:<isrc>` cross-matching. No auth.
* **Spotify oEmbed** (`https://open.spotify.com/oembed`) — title and artwork
  for a track/album/plural link. No auth.
* **Qobuz catalogue API** — album/track metadata, using configured app and user
  authentication when available.
* **Odesli / song.link** (`https://api.song.link/v1-alpha.1/links`) — the
  bridge: given a link on any one platform it returns the matching links on
  every other platform that carries the same recording or release. That is how
  a Tidal, Qobuz or Amazon link reaches a metadata source that does not require
  credentials: resolve it to its Deezer or Apple sibling, then ask that sibling.

Every function is async, uses one shared aiohttp session with a certifi SSL
context (the alpine image ships no CA bundle aiohttp finds by itself), and
returns `None` rather than raising when a lookup fails. Metadata is a
convenience here — a missing title must never cost the user an audio file that
already downloaded successfully.
"""

import asyncio
import json
import re
import ssl
from dataclasses import dataclass, field
from io import BytesIO
from urllib.parse import quote

import aiohttp
import certifi
from PIL import Image

from dl_utils import platform_config as pcfg
from dl_utils.link_resolver import (
    KIND_ALBUM,
    KIND_TRACK,
    PLATFORM_AMAZON,
    PLATFORM_APPLE,
    PLATFORM_DEEZER,
    PLATFORM_QOBUZ,
    PLATFORM_SPOTIFY,
    PLATFORM_TIDAL,
    PLATFORM_YOUTUBE,
    PLATFORM_SOUNDCLOUD,
)

_ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
_APPLE_ART_MAX = 3000
_DEEZER_API = "https://api.deezer.com"
_ODESLI = "https://api.song.link/v1-alpha.1/links"
_SPOTIFY_OEMBED = "https://open.spotify.com/oembed"

_TIMEOUT = aiohttp.ClientTimeout(total=25, connect=10)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.8, */*;q=0.5",
}

# One retry with a short pause: these are public endpoints and a transient 5xx
# or a dropped connection is far more common than a permanent failure.
_ATTEMPTS = 3


@dataclass
class TrackInfo:
    """One track of a release, as reported by a public metadata source."""

    number: int = 0
    disc: int = 1
    title: str = ""
    artist: str = ""
    isrc: str = ""
    duration_ms: int = 0
    explicit: bool = False
    id: str = ""


@dataclass
class ReleaseInfo:
    """Canonical description of a track or album, from any public source."""

    platform: str = ""
    kind: str = KIND_ALBUM
    object_id: str = ""
    title: str = ""
    artist: str = ""
    album_title: str = ""
    year: str = ""
    release_date: str = ""
    cover_data: bytes | None = None
    cover_url: str = ""
    album_link: str = ""
    track_link: str = ""
    tracks: list = field(default_factory=list)
    isrcs: list = field(default_factory=list)
    source: str = ""
    #: Cross-matched links on other platforms, {"deezer": "https://...", ...}
    siblings: dict = field(default_factory=dict)

    @property
    def track_count(self) -> int:
        return len(self.tracks)


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


class _Session:
    """Lazy, shared ClientSession.

    aiohttp pools connections per event loop; holding one module-level session
    and rebuilding it when the loop changes keeps the bot from leaking sockets
    across restarts in the same process.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._loop = None
        self._lock = asyncio.Lock()

    async def get(self) -> aiohttp.ClientSession:
        loop = asyncio.get_running_loop()
        if self._session is not None and not self._session.closed and self._loop is loop:
            return self._session
        async with self._lock:
            if self._session is not None and not self._session.closed and self._loop is loop:
                return self._session
            connector = aiohttp.TCPConnector(ssl=_ssl_context(), limit=8)
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers=_HEADERS,
                timeout=_TIMEOUT,
                max_line_size=65536,
                max_field_size=65536,
            )
            self._loop = loop
            return self._session


_SESSION = _Session()


async def _fetch_text(
    url: str, *, params: dict | None = None, headers: dict | None = None
) -> str | None:
    session = await _SESSION.get()
    last_error: Exception | None = None
    for attempt in range(_ATTEMPTS):
        try:
            request = session.get(url, params=params, **({"headers": headers} if headers else {}))
            async with request as resp:
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait = max(float(retry_after), 1.0)
                    except (TypeError, ValueError):
                        wait = 2.0 * (attempt + 1)
                    await asyncio.sleep(min(wait, 15.0))
                    continue
                if resp.status >= 500:
                    last_error = aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=resp.status
                    )
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                if resp.status >= 400:
                    print(f"[meta] GET {url} failed: HTTP {resp.status}")
                    return None
                resp.raise_for_status()
                return await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_error = e
            await asyncio.sleep(0.8 * (attempt + 1))
    if last_error:
        print(f"[meta] GET {url} failed after {_ATTEMPTS} attempts: {last_error}")
    return None


async def _fetch_json(
    url: str, *, params: dict | None = None, headers: dict | None = None
) -> dict | list | None:
    text = await _fetch_text(url, params=params, headers=headers)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[meta] bad JSON from {url}: {e}")
        return None


def upgrade_cover_url(url: str) -> str:
    """Request the largest provider artwork variant without rewriting its path shape."""
    url = str(url or "").strip()
    if not url:
        return ""
    if "i.scdn.co/image/" in url or "spotifycdn.com/image/" in url:
        return re.sub(r"ab67616d0000(?:4851|1e02)", "ab67616d0000b273", url)
    if "dzcdn.net/images/cover/" in url:
        return re.sub(r"/(?:250|500|1000)x(?:250|500|1000)-", "/1200x1200-", url)
    if "mzstatic.com" in url:
        return re.sub(r"/\d+x\d+(bb|png|jpg)?", "/1200x1200\\1", url)
    if "resources.tidal.com/images/" in url:
        return re.sub(r"/\d+x\d+\.jpg(?=$|[?#])", "/1280x1280.jpg", url)
    if "static.qobuz.com/images/covers/" in url:
        return re.sub(r"_(?:230|600)\.jpg(?=([?#]|$))", "_max.jpg", url)
    if "sndcdn.com" in url:
        return re.sub(r"-large\.(jpg|png)(?=([?#]|$))", r"-t500x500.\1", url)
    return url


def cover_url_candidates(url: str) -> list[str]:
    """Ordered high-resolution request and provider-specific fallbacks."""
    upgraded = upgrade_cover_url(url)
    candidates = [upgraded] if upgraded else []
    fallback = ""
    if "i.scdn.co/image/" in url or "spotifycdn.com/image/" in url:
        if "ab67616d0000b273" in upgraded:
            fallback = upgraded.replace("ab67616d0000b273", "ab67616d000082c1")
        elif "ab67616d000082c1" in upgraded:
            fallback = upgraded.replace("ab67616d000082c1", "ab67616d0000b273")
    elif "dzcdn.net/images/cover/" in url and "/1200x1200-" in upgraded:
        fallback = upgraded.replace("/1200x1200-", "/1000x1000-")
    elif "mzstatic.com" in url and "/1200x1200" in upgraded:
        fallback = re.sub(r"/1200x1200", "/600x600", upgraded)
    elif "static.qobuz.com/images/covers/" in url and "_max.jpg" in upgraded:
        fallback = upgraded.replace("_max.jpg", "_600.jpg")
    elif "sndcdn.com" in url and "-t500x500." in upgraded:
        fallback = re.sub(r"-t500x500\.", "-large.", upgraded)
    if fallback and fallback not in candidates:
        candidates.append(fallback)
    if url and url not in candidates:
        candidates.append(url)
    return candidates


async def fetch_bytes(url: str) -> bytes | None:
    """Download the best available artwork, falling back on non-200 responses."""
    urls = cover_url_candidates(url) or [url]
    for candidate_index, candidate in enumerate(urls):
        session = await _SESSION.get()
        for attempt in range(_ATTEMPTS):
            try:
                async with session.get(candidate) as resp:
                    if resp.status != 200:
                        print(f"[meta] art fetch failed ({candidate}): HTTP {resp.status}")
                        break
                    data = await resp.read()
                    if data:
                        return data
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"[meta] art fetch failed ({candidate}), attempt {attempt + 1}: {e}")
                await asyncio.sleep(0.8 * (attempt + 1))
        if candidate_index + 1 < len(urls):
            print(f"[meta] trying artwork fallback {urls[candidate_index + 1]}")
    return None


_ITUNES_SEARCH = "https://itunes.apple.com/search"
_QOBUZ_API = "https://play.qobuz.com/api.json/0.2"
_GENERIC_METADATA_NAMES = frozenset({
    "qobuz", "play qobuz", "open qobuz", "tidal", "spotify",
    "apple music", "itunes", "deezer", "soundcloud", "amazon music",
    "amazon", "youtube", "youtube music", "music", "home", "login",
    "sign in", "log in", "web player", "music streaming",
})


def _clean_metadata_text(value) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("title") or ""
    text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n-|–—")
    return "" if text.casefold() in _GENERIC_METADATA_NAMES else text


def _real_release_metadata(info: ReleaseInfo | None) -> bool:
    if not info:
        return False
    artist = _clean_metadata_text(info.artist)
    if artist.casefold() == "unknown artist":
        artist = ""
    title = _clean_metadata_text(
        info.title if info.kind == KIND_TRACK else info.album_title
    )
    if title.casefold() == "unknown album":
        title = ""
    return bool(artist or title)


def _search_metadata_hint(info: ReleaseInfo | None, kind: str, url: str) -> str:
    """Build a search only from human metadata or a descriptive store slug."""
    if _real_release_metadata(info):
        parts = [
            _clean_metadata_text(info.artist),
            _clean_metadata_text(info.title if kind == KIND_TRACK else info.album_title),
        ]
        return " ".join(part for part in parts if part and part.casefold() != "unknown artist").strip()

    slug = search_hint_from_url(url)
    words = re.findall(r"[^\W\d_]+", slug.casefold())
    if len(words) < 2 or any(word in _GENERIC_METADATA_WORDS for word in words):
        return ""
    return slug


def _canonical_request_link(
    platform: str, kind: str, object_id: str, url: str = ""
) -> str:
    if platform == PLATFORM_QOBUZ and kind in (KIND_ALBUM, KIND_TRACK) and object_id:
        return f"https://open.qobuz.com/{kind}/{object_id}"
    return url


def search_hint_from_url(url: str) -> str:
    """Extract human-readable Qobuz slug words for public catalogue searches."""
    match = re.search(r"/(?:album|track)/([^/]+)/[A-Za-z0-9]+(?:[/?#]|$)", url, re.I)
    if not match or match.group(1) == "-":
        return ""
    from urllib.parse import unquote

    return unquote(match.group(1)).replace("-", " ").strip()


async def qobuz_public(kind: str, object_id: str) -> ReleaseInfo | None:
    """Read Qobuz catalogue metadata, authenticating when configured."""
    endpoint = "album/get" if kind == KIND_ALBUM else "track/get"
    fields = pcfg.qobuz_fields()
    app_id = str(fields.get("app_id") or "").strip()
    use_auth_token = fields.get("use_auth_token")
    auth_token = str(fields.get("password_or_token") or "").strip() if use_auth_token else ""
    params = {f"{kind}_id": object_id}
    headers = {}
    if app_id:
        params["app_id"] = app_id
        headers["X-App-Id"] = app_id
    if auth_token:
        headers["X-User-Auth-Token"] = auth_token
    payload = await _fetch_json(
        f"{_QOBUZ_API}/{endpoint}", params=params,
        **({"headers": headers} if headers else {}),
    )
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    if isinstance(payload.get(kind), dict):
        payload = payload[kind]
    title = _clean_metadata_text(payload.get("title"))
    if not title:
        return None

    album = payload.get("album") or {}
    artist_data = payload.get("artist") or album.get("artist") or {}
    artist = _clean_metadata_text(
        artist_data.get("name") or artist_data.get("display") or ""
    )
    album_title = _clean_metadata_text(
        album.get("title") or (title if kind == KIND_ALBUM else "")
    )
    date = (
        payload.get("release_date_original")
        or album.get("release_date_original")
        or payload.get("release_date")
        or ""
    )
    image = payload.get("image") or album.get("image") or {}
    raw_tracks = (payload.get("tracks") or {}).get("items") or []
    if isinstance(raw_tracks, dict):
        raw_tracks = list(raw_tracks.values())
    tracks = [
        TrackInfo(
            number=int(item.get("track_number") or index),
            disc=int(item.get("media_number") or 1),
            title=item.get("title") or item.get("name") or "",
            artist=(item.get("performer") or artist_data).get("name", artist),
            duration_ms=int(item.get("duration") or 0) * 1000,
            id=str(item.get("id") or ""),
            isrc=item.get("isrc") or "",
        )
        for index, item in enumerate(raw_tracks, start=1)
        if isinstance(item, dict)
    ]
    title = title or album_title
    link = f"https://open.qobuz.com/{kind}/{object_id}"
    return ReleaseInfo(
        platform=PLATFORM_QOBUZ,
        kind=kind,
        object_id=object_id,
        title=title if kind == KIND_TRACK else "",
        artist=artist or "Unknown Artist",
        album_title=album_title,
        year=_year_from(date),
        release_date=date[:10],
        cover_url=upgrade_cover_url(image.get("large") or image.get("extralarge") or image.get("small") or ""),
        album_link=link if kind == KIND_ALBUM else f"https://open.qobuz.com/album/{album.get('id')}",
        track_link=link if kind == KIND_TRACK else "",
        tracks=tracks,
        isrcs=[t.isrc for t in tracks if t.isrc],
        source="qobuz-api",
        siblings={"qobuz": link},
    )


def _search_score(query: str, result: dict) -> float:
    title = result.get("collectionName") if result.get("wrapperType") != "track" else result.get("trackName")
    haystack = f"{result.get('artistName', '')} {title or ''}".lower()
    words = re.findall(r"[\w]+", query.lower())
    return sum(word in haystack for word in words) / max(len(words), 1)


async def itunes_search(term: str, kind: str, storefront: str | None = None) -> ReleaseInfo | None:
    if not term:
        return None
    entity = "song" if kind == KIND_TRACK else "album"
    payload = await _fetch_json(
        _ITUNES_SEARCH,
        params={"term": term, "entity": entity, "limit": "10", "country": storefront or "US"},
    )
    results = (payload or {}).get("results") or []
    if not results:
        return None
    item = max(results, key=lambda result: _search_score(term, result))
    info = ReleaseInfo(
        platform=PLATFORM_APPLE,
        kind=kind,
        object_id=str(item.get("trackId") or item.get("collectionId") or ""),
        title=item.get("trackName", "") if kind == KIND_TRACK else "",
        artist=item.get("artistName") or "Unknown Artist",
        album_title=item.get("collectionName", ""),
        year=_year_from(item.get("releaseDate", "")),
        release_date=(item.get("releaseDate") or "")[:10],
        cover_url=_apple_artwork(item.get("artworkUrl100", "")),
        track_link=item.get("trackViewUrl", "") if kind == KIND_TRACK else "",
        album_link=item.get("collectionViewUrl", ""),
        source="itunes-search",
        siblings={"apple": item.get("trackViewUrl") or item.get("collectionViewUrl", "")},
    )
    info.siblings["itunes"] = info.siblings["apple"]
    return info


async def deezer_search(term: str, kind: str) -> ReleaseInfo | None:
    if not term:
        return None
    payload = await _fetch_json(
        f"{_DEEZER_API}/search/{kind}", params={"q": term, "limit": "10"}
    )
    items = (payload or {}).get("data") or []
    if not items:
        return None
    item = max(items, key=lambda result: _search_score(term, {
        "artistName": (result.get("artist") or {}).get("name", ""),
        "collectionName": result.get("title", ""),
    }))
    object_id = str(item.get("id") or "")
    if not object_id:
        return None
    album = item.get("album") or {}
    date = item.get("release_date") or album.get("release_date") or ""
    link = item.get("link") or f"https://www.deezer.com/{kind}/{object_id}"
    return ReleaseInfo(
        platform=PLATFORM_DEEZER,
        kind=kind,
        object_id=object_id,
        title=item.get("title", "") if kind == KIND_TRACK else "",
        artist=(item.get("artist") or {}).get("name", "Unknown Artist"),
        album_title=album.get("title", "") if kind == KIND_TRACK else item.get("title", ""),
        year=_year_from(date),
        release_date=date[:10],
        cover_url=upgrade_cover_url(item.get("cover_xl") or item.get("cover_big") or album.get("cover_xl", "")),
        track_link=link if kind == KIND_TRACK else album.get("link", ""),
        album_link=link if kind == KIND_ALBUM else album.get("link", ""),
        source="deezer-search",
        siblings={"deezer": link},
    )


async def _cross_match_public(info: ReleaseInfo, term: str, kind: str, storefront=None) -> ReleaseInfo:
    """Add sibling links without replacing verified direct-provider metadata."""
    for candidate in (
        await itunes_search(term, kind, storefront),
        await deezer_search(term, kind),
    ):
        if not candidate:
            continue
        key = "deezer" if candidate.platform == PLATFORM_DEEZER else "apple"
        info.siblings[key] = candidate.track_link if kind == KIND_TRACK else candidate.album_link
        if key == "apple":
            info.siblings["itunes"] = info.siblings[key]
    return info


async def public_search_metadata(
    platform: str, kind: str, object_id: str, url: str, storefront=None
) -> ReleaseInfo | None:
    """Resolve store metadata without turning boilerplate into a search query."""
    info = (
        await qobuz_public(kind, object_id) if platform == PLATFORM_QOBUZ else None
    )
    if info is None:
        info = await page_metadata(url, platform, kind, object_id)
    term = _search_metadata_hint(info, kind, url)
    if not term:
        return info
    if info is not None:
        return await _cross_match_public(info, term, kind, storefront)

    candidate = await itunes_search(term, kind, storefront) or await deezer_search(term, kind)
    if candidate is None:
        return None
    if platform == PLATFORM_QOBUZ:
        # A cross-store guess is enrichment, not proof of the Qobuz release's
        # identity. Keep the requested platform and canonical link intact.
        candidate.platform = platform
        candidate.object_id = object_id
        candidate.album_link = _canonical_request_link(platform, KIND_ALBUM, object_id, url)
        candidate.track_link = _canonical_request_link(platform, KIND_TRACK, object_id, url)
    return candidate


# ---------------------------------------------------------------------------
# iTunes Lookup (Apple Music)
# ---------------------------------------------------------------------------
def _apple_artwork(url: str, size: int = _APPLE_ART_MAX) -> str:
    """Request a large JPEG through the shared CDN upgrade policy."""
    del size
    return upgrade_cover_url(url)


def _year_from(date_str: str) -> str:
    match = re.match(r"(\d{4})", date_str or "")
    return match.group(1) if match else ""


def _apple_track_from(result: dict) -> TrackInfo:
    return TrackInfo(
        number=int(result.get("trackNumber") or 0),
        disc=int(result.get("discNumber") or 1),
        title=result.get("trackName", "") or result.get("collectionName", ""),
        artist=result.get("artistName", ""),
        duration_ms=int(result.get("durationMillis") or 0),
        explicit=bool(result.get("trackExplicitness") == "explicit"),
        id=str(result.get("trackId") or result.get("id") or ""),
    )


async def itunes_album(album_id: str, storefront: str | None = None) -> ReleaseInfo | None:
    """Full album metadata plus tracklist from the iTunes Lookup API."""
    params = {"id": str(album_id), "entity": "song", "limit": "200"}
    if storefront:
        params["country"] = storefront
    payload = await _fetch_json(_ITUNES_LOOKUP, params=params)
    if not payload or not payload.get("results"):
        return None

    results = payload["results"]
    album = next(
        (r for r in results if r.get("wrapperType") == "album" or r.get("collectionType")),
        results[0],
    )
    tracks = [_apple_track_from(r) for r in results if r.get("wrapperType") == "track"]
    release_date = album.get("releaseDate", "")

    info = ReleaseInfo(
        platform=PLATFORM_APPLE,
        kind=KIND_ALBUM,
        object_id=str(album_id),
        title=album.get("collectionName", ""),
        artist=album.get("artistName", ""),
        album_title=album.get("collectionName", ""),
        year=_year_from(release_date),
        release_date=release_date[:10] if release_date else "",
        cover_url=_apple_artwork(album.get("artworkUrl100", "")),
        album_link=album.get("collectionViewUrl") or album.get("trackViewUrl", ""),
        tracks=tracks,
        isrcs=[t.isrc for t in tracks if t.isrc],
        source="itunes",
    )
    return info


async def itunes_track(track_id: str, storefront: str | None = None) -> ReleaseInfo | None:
    """Single-track metadata; the parent album supplies album title and art."""
    params = {"id": str(track_id)}
    if storefront:
        params["country"] = storefront
    payload = await _fetch_json(_ITUNES_LOOKUP, params=params)
    if not payload or not payload.get("results"):
        return None

    results = payload["results"]
    song = next((r for r in results if r.get("wrapperType") == "track"), results[0])
    album_entry = next((r for r in results if r.get("wrapperType") == "album"), None)
    release_date = song.get("releaseDate", "")
    track = _apple_track_from(song)
    track.number = int(song.get("trackNumber") or 1)

    return ReleaseInfo(
        platform=PLATFORM_APPLE,
        kind=KIND_TRACK,
        object_id=str(track_id),
        title=track.title,
        artist=song.get("artistName", ""),
        album_title=song.get("collectionName", "")
        or (album_entry or {}).get("collectionName", ""),
        year=_year_from(release_date),
        release_date=release_date[:10] if release_date else "",
        cover_url=_apple_artwork(song.get("artworkUrl100", "")),
        track_link=song.get("trackViewUrl", ""),
        album_link=(album_entry or song).get("collectionViewUrl", ""),
        tracks=[track],
        isrcs=[track.isrc] if track.isrc else [],
        source="itunes",
    )


async def itunes_artist(artist_id: str, storefront: str | None = None) -> dict | None:
    """Artist name plus their album IDs, newest first."""
    params = {
        "id": str(artist_id),
        "entity": "album",
        "limit": "200",
        "returnOnlyCatalog": "true",
    }
    if storefront:
        params["country"] = storefront
    payload = await _fetch_json(_ITUNES_LOOKUP, params=params)
    if not payload or not payload.get("results"):
        return None

    results = payload["results"]
    artist = next((r for r in results if r.get("wrapperType") == "artist"), None)
    albums = [
        {
            "id": str(r.get("collectionId") or r.get("id")),
            "title": r.get("collectionName", ""),
            "year": _year_from(r.get("releaseDate", "")),
            "release_date": (r.get("releaseDate") or "")[:10],
        }
        for r in results
        if r.get("wrapperType") == "album" or r.get("collectionId")
    ]
    seen, unique = set(), []
    for album in albums:
        if album["id"] and album["id"] not in seen:
            seen.add(album["id"])
            unique.append(album)
    unique.sort(key=lambda a: a["release_date"], reverse=True)

    return {
        "id": str(artist_id),
        "name": (artist or {}).get("artistName", "") or (results[0].get("artistName", "")),
        "link": (artist or {}).get("artistUrl", ""),
        "albums": unique,
    }


# ---------------------------------------------------------------------------
# Deezer public API
# ---------------------------------------------------------------------------
def _deezer_art(cover: str, size: int = 1200) -> str:
    del size
    return upgrade_cover_url(cover)


async def deezer_album(album_id: str) -> ReleaseInfo | None:
    payload = await _fetch_json(f"{_DEEZER_API}/album/{album_id}")
    if not payload or "error" in payload:
        return None

    tracks_raw = []
    pages = payload.get("tracks", {})
    url = pages.get("url")
    # A Deezer album's track list is paginated; 500 per page covers any real
    # release, but a box set can exceed it, so follow `next` while it exists.
    guard = 0
    while url and guard < 6:
        page = await _fetch_json(url, params={"limit": 500})
        if not page:
            break
        tracks_raw.extend(page.get("data", []))
        url = page.get("next")
        guard += 1

    tracks = [
        TrackInfo(
            number=int(t.get("track_position") or i + 1),
            disc=int(t.get("disk_number") or 1),
            title=t.get("title", ""),
            artist=(t.get("artist") or {}).get("name", ""),
            duration_ms=int(t.get("duration") or 0) * 1000,
            explicit=bool(t.get("explicit_content")),
            id=str(t.get("id") or ""),
            isrc=t.get("isrc", "") or "",
        )
        for i, t in enumerate(tracks_raw)
    ]
    release_date = payload.get("release_date", "")
    return ReleaseInfo(
        platform=PLATFORM_DEEZER,
        kind=KIND_ALBUM,
        object_id=str(album_id),
        title=payload.get("title", ""),
        artist=(payload.get("artist") or {}).get("name", ""),
        album_title=payload.get("title", ""),
        year=_year_from(release_date),
        release_date=release_date,
        cover_url=_deezer_art(payload.get("cover_xl") or payload.get("cover_big") or ""),
        album_link=payload.get("link", ""),
        tracks=tracks,
        isrcs=[t.isrc for t in tracks if t.isrc],
        source="deezer",
    )


async def deezer_track(track_id: str) -> ReleaseInfo | None:
    payload = await _fetch_json(f"{_DEEZER_API}/track/{track_id}")
    if not payload or "error" in payload:
        return None

    album = payload.get("album") or {}
    release_date = album.get("release_date", "")
    contributors = [
        c.get("name") for c in payload.get("contributors", []) if c.get("name")
    ]
    track = TrackInfo(
        number=int(payload.get("rank") or 1),
        title=payload.get("title", ""),
        artist=(payload.get("artist") or {}).get("name", ""),
        duration_ms=int(payload.get("duration") or 0) * 1000,
        explicit=bool(payload.get("explicit_content")),
        id=str(payload.get("id") or ""),
        isrc=payload.get("isrc", "") or "",
    )
    return ReleaseInfo(
        platform=PLATFORM_DEEZER,
        kind=KIND_TRACK,
        object_id=str(track_id),
        title=track.title,
        artist=(payload.get("artist") or {}).get("name", ""),
        album_title=album.get("title", ""),
        year=_year_from(release_date),
        release_date=release_date,
        cover_url=_deezer_art(album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium") or ""),
        track_link=payload.get("link", ""),
        album_link=album.get("link", ""),
        tracks=[track],
        isrcs=[track.isrc] if track.isrc else [],
        source="deezer",
        # `artists_list` equivalent: contributors carry featured artists.
        siblings={"contributors": contributors},
    )


async def deezer_artist(artist_id: str) -> dict | None:
    payload = await _fetch_json(f"{_DEEZER_API}/artist/{artist_id}")
    if not payload or "error" in payload:
        return None
    albums = await _fetch_json(f"{_DEEZER_API}/artist/{artist_id}/albums", params={"limit": 500})
    releases = []
    for item in (albums or {}).get("data", []):
        release_date = item.get("release_date", "")
        releases.append(
            {
                "id": str(item.get("id")),
                "title": item.get("title", ""),
                "record_type": item.get("record_type", "album"),
                "year": _year_from(release_date),
                "release_date": release_date,
            }
        )
    releases.sort(key=lambda r: r["release_date"], reverse=True)
    return {
        "id": str(artist_id),
        "name": payload.get("name", ""),
        "link": payload.get("link", ""),
        "picture": payload.get("picture_xl") or payload.get("picture_big") or "",
        "albums": releases,
    }


async def deezer_isrc_match(isrc: str) -> dict | None:
    """Cross-match an ISRC to a Deezer track. The glue between platforms.

    Deezer's `search/isrc:` endpoint is the only free, credential-less lookup
    that answers "which recording is this ISRC" with a platform-native id, so
    it is what turns an Apple or Spotify track into downloadable audio through
    the local Deezer ARL engine.
    """
    if not isrc:
        return None
    payload = await _fetch_json(f"{_DEEZER_API}/track/isrc:{quote(isrc)}")
    if not payload or payload.get("error"):
        return None
    if payload.get("id"):
        return payload
    data = payload.get("data") or []
    return data[0] if data else None


# ---------------------------------------------------------------------------
# Spotify (oEmbed only — the REST API needs an OAuth token)
# ---------------------------------------------------------------------------
async def spotify_oembed(url: str) -> dict | None:
    """Title and thumbnail for a Spotify track/album link, no auth needed.

    The response splits as "<Artist> - <Title>" for a track and "<Artist> -
    <Album>" for an album, which is enough to name an archive correctly when
    nothing else is available.
    """
    payload = await _fetch_json(_SPOTIFY_OEMBED, params={"url": url})
    if not payload:
        return None
    title = payload.get("title", "") or ""
    parts = [p.strip() for p in re.split(r"\s+[–—-]\s+", title, maxsplit=1) if p.strip()]
    artist = parts[0] if len(parts) > 1 else ""
    if not artist or artist.casefold() == "unknown artist":
        object_id = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        kind = KIND_ALBUM if "/album/" in url else KIND_TRACK
        info = await page_metadata(url, PLATFORM_SPOTIFY, kind, object_id)
        if info:
            artist = info.artist
        if not artist or artist.casefold() == "unknown artist":
            try:
                from dl_utils import dotify_shim

                entity = await asyncio.to_thread(dotify_shim.embed_entity, kind, object_id)
                artist = dotify_shim._artist(entity)
            except Exception as error:
                print(f"[meta] Spotify artist fallback failed: {error}")
    return {
        "artist": "" if artist.casefold() == "unknown artist" else artist,
        "title": parts[-1] if parts else title,
        "thumbnail": upgrade_cover_url(payload.get("thumbnail_url", "") or ""),
        "provider": payload.get("provider_name", "Spotify"),
    }


# ---------------------------------------------------------------------------
# Odesli / song.link — the cross-platform bridge
# ---------------------------------------------------------------------------
_ALLOWED_NODE = ("spotify", "appleMusic", "tidal", "deezer", "qobuz", "amazonMusic")

_NODE_TO_PLATFORM = {
    "spotify": PLATFORM_SPOTIFY,
    "appleMusic": PLATFORM_APPLE,
    "itunes": PLATFORM_APPLE,
    "tidal": PLATFORM_TIDAL,
    "deezer": PLATFORM_DEEZER,
    "qobuz": PLATFORM_QOBUZ,
    "amazonMusic": PLATFORM_AMAZON,
}


async def odesli_links(url: str) -> dict | None:
    """Resolve a link to its counterparts using Song.link's free parameters."""
    params = {"url": url, "userCountry": "US"}
    payload = await _fetch_json(_ODESLI, params=params)
    if not payload or not isinstance(payload, dict):
        return None

    entities = payload.get("entitiesByUniqueId") or payload.get("entities") or {}
    entity = entities.get("XO") or next(iter(entities.values()), {})
    links = payload.get("linksByPlatform") or entity.get("linksByPlatform") or {}

    matched_urls: dict[str, str] = {}
    platforms: dict[str, str] = {}
    for node, value in links.items():
        key = _NODE_TO_PLATFORM.get(node)
        if not isinstance(value, dict) or not value.get("url"):
            continue
        if node in ("deezer", "spotify", "tidal", "appleMusic", "itunes"):
            matched_urls[node] = value["url"]
        if key and key not in platforms:
            platforms[key] = value["url"]

    artwork = entity.get("thumbnailUrl") or ""
    thumbnails = entity.get("thumbnails") or {}
    for size in ("blurred", "strongReflex", "default"):
        if not artwork and thumbnails.get(size):
            artwork = thumbnails[size]

    return {
        "title": entity.get("title", "") or "",
        "artist": entity.get("artistName", "") or "",
        "artwork": upgrade_cover_url(artwork),
        "isrc": next(
            (
                value.get("isrc")
                for value in entities.values()
                if isinstance(value, dict) and value.get("isrc")
            ),
            "",
        ),
        "matched_urls": matched_urls,
        # Retain the existing normalized names for release-info consumers.
        "platforms": platforms,
    }


async def tidal_api_metadata(kind: str, object_id: str) -> ReleaseInfo | None:
    """Resolve Tidal metadata through its authenticated public object API."""
    if kind not in (KIND_TRACK, KIND_ALBUM):
        return None
    from dl_utils import platform_config as pcfg

    if not pcfg.has_tidal_credentials():
        return None
    fields = pcfg.tidal_oauth_fields()
    endpoint = "tracks" if kind == KIND_TRACK else "albums"
    country = str(fields.get("country_code") or "US")
    headers = {
        "Authorization": f"{fields.get('token_type') or 'Bearer'} {fields['access_token']}"
    }
    data = await _fetch_json(
        f"https://api.tidal.com/v1/{endpoint}/{object_id}",
        params={"countryCode": country},
        headers=headers,
    )
    if not isinstance(data, dict):
        return None
    artist_data = data.get("artist") or {}
    artists = data.get("artists") or []
    artist = artist_data.get("name") or next(
        (item.get("name") for item in artists if isinstance(item, dict) and item.get("name")),
        "Unknown Artist",
    )
    album = data.get("album") or {}
    title = str(data.get("title") or "")
    date = str(data.get("releaseDate") or data.get("streamStartDate") or "")
    cover = str(data.get("cover") or album.get("cover") or "").replace("-", "/")
    info = ReleaseInfo(
        platform=PLATFORM_TIDAL,
        kind=kind,
        object_id=object_id,
        title=title if kind == KIND_TRACK else "",
        artist=artist,
        album_title=title if kind == KIND_ALBUM else str(album.get("title") or ""),
        year=_year_from(date),
        release_date=date[:10],
        cover_url=f"https://resources.tidal.com/images/{cover}/1280x1280.jpg" if cover else "",
        track_link=f"https://tidal.com/browse/track/{object_id}" if kind == KIND_TRACK else "",
        album_link=f"https://tidal.com/browse/album/{object_id}" if kind == KIND_ALBUM else "",
        source="tidal-api",
    )
    if kind == KIND_TRACK and data.get("isrc"):
        info.isrcs = [str(data["isrc"])]
    if kind == KIND_ALBUM:
        items = await _fetch_json(
            f"https://api.tidal.com/v1/albums/{object_id}/tracks",
            params={"countryCode": country},
            headers=headers,
        )
        rows = items.get("items", []) if isinstance(items, dict) else []
        for index, item in enumerate(rows, 1):
            if not isinstance(item, dict):
                continue
            track_artists = item.get("artists") or []
            info.tracks.append(TrackInfo(
                number=int(item.get("trackNumber") or index),
                disc=int(item.get("volumeNumber") or 1),
                title=str(item.get("title") or ""),
                artist=(item.get("artist") or {}).get("name") or next(
                    (a.get("name") for a in track_artists if isinstance(a, dict)),
                    artist,
                ),
                isrc=str(item.get("isrc") or ""),
                duration_ms=int(item.get("duration") or 0) * 1000,
                explicit=bool(item.get("explicit")),
                id=str(item.get("id") or ""),
            ))
    return info


def _meta_content(text: str, key: str) -> str:
    from html import unescape

    pattern = re.compile(r"<meta\b[^>]*>", re.I)
    for tag in pattern.findall(text):
        attributes = dict(
            (name.lower(), unescape(value))
            for name, _, value in re.findall(
                r"([\w:-]+)\s*=\s*([\"'])(.*?)\2", tag, re.S
            )
        )
        if attributes.get("property", "").lower() == key or attributes.get("name", "").lower() == key:
            return attributes.get("content", "").strip()
    return ""


async def page_metadata(url: str, platform: str, kind: str, object_id: str) -> ReleaseInfo | None:
    """Read public Open Graph or title metadata when a page is accessible."""
    text = await _fetch_text(url)
    if not text:
        return None
    structured_artist = ""
    structured_title = ""
    structured_date = ""
    structured_cover = ""
    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text, re.S | re.I,
    ):
        try:
            data = json.loads(raw.strip())
        except (TypeError, ValueError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            artist_data = item.get("byArtist") or item.get("brand") or {}
            if isinstance(artist_data, dict):
                structured_artist = structured_artist or _clean_metadata_text(artist_data.get("name"))
            if kind == KIND_ALBUM and item.get("@type") in ("MusicAlbum", "Product"):
                structured_title = structured_title or _clean_metadata_text(item.get("name"))
            structured_date = structured_date or item.get("datePublished") or item.get("releaseDate") or ""
            images = item.get("image") or []
            if isinstance(images, str):
                images = [images]
            if images:
                structured_cover = structured_cover or images[-1]
    title = _meta_content(text, "og:title")
    description = _meta_content(text, "og:description")
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if match:
            from html import unescape
            title = unescape(match.group(1)).strip()
    title = re.sub(
        r"\s+(?:-|–|—|\|)\s*(?:Qobuz|Play Qobuz|Open Qobuz|TIDAL|Spotify|Apple Music|Deezer|SoundCloud|Amazon Music).*$",
        "", title, flags=re.I,
    )
    title = _clean_metadata_text(title)
    artist, release_title = "", title
    if not release_title:
        release_title = structured_title
    if platform == PLATFORM_AMAZON:
        # Amazon commonly formats title as "Album by Artist" and places
        # "Artist - Title" in the Open Graph description.
        by = re.match(r"(.+?)\s+by\s+(.+)$", title, re.I)
        if by:
            release_title, artist = (part.strip() for part in by.groups())
        elif " - " in description:
            artist, release_title = (part.strip() for part in description.split(" - ", 1))
        elif "," in title:
            release_title, artist = (part.strip() for part in title.split(",", 1))
    elif "," in title:
        release_title, artist = (part.strip() for part in title.split(",", 1))
    elif " - " in title:
        artist, release_title = (part.strip() for part in title.split(" - ", 1))
    if platform == PLATFORM_TIDAL:
        artist, release_title = release_title, artist
    artist = _clean_metadata_text(artist or structured_artist)
    release_title = _clean_metadata_text(release_title or structured_title)
    if not release_title:
        return None
    canonical = _canonical_request_link(platform, kind, object_id, url)
    return ReleaseInfo(
        platform=platform,
        kind=kind,
        object_id=object_id,
        title=release_title if kind == KIND_TRACK else "",
        artist=artist or "Unknown Artist",
        album_title="" if kind == KIND_TRACK else release_title,
        year=_year_from(structured_date),
        release_date=structured_date[:10],
        cover_url=upgrade_cover_url(_meta_content(text, "og:image") or structured_cover),
        album_link=canonical if kind == KIND_ALBUM else "",
        track_link=canonical if kind == KIND_TRACK else "",
        source="page-title",
    )


# ---------------------------------------------------------------------------
# Unified entry points
# ---------------------------------------------------------------------------
async def _attach_cover(info: ReleaseInfo) -> ReleaseInfo:
    if info.cover_data is None and info.cover_url:
        data = await fetch_bytes(info.cover_url)
        if data:
            info.cover_data = data
    return info


async def release_info(
    platform: str,
    kind: str,
    object_id: str,
    track_id: str | None = None,
    storefront: str | None = None,
    url: str = "",
) -> ReleaseInfo | None:
    """Best-effort canonical metadata for one resolved link.

    `track_id` is the Apple `?i=` / Amazon `?trackAsin=` case: the link points
    at an album but the user wants one track from it.
    """
    info: ReleaseInfo | None = None

    if platform == PLATFORM_APPLE:
        if track_id:
            info = await itunes_track(track_id, storefront)
        elif kind == KIND_TRACK:
            info = await itunes_track(object_id, storefront)
        elif kind == KIND_ALBUM:
            info = await itunes_album(object_id, storefront)
    elif platform == PLATFORM_DEEZER:
        if kind == KIND_TRACK:
            info = await deezer_track(object_id)
        elif kind == KIND_ALBUM:
            info = await deezer_album(object_id)
    elif platform == PLATFORM_SPOTIFY and url:
        info = await _from_spotify_oembed(url, kind, object_id)
    elif platform in (PLATFORM_QOBUZ, PLATFORM_TIDAL) and url:
        if platform == PLATFORM_QOBUZ:
            info = await qobuz_public(kind, object_id)
        odesli = await _from_odesli(url, kind, object_id, platform)
        if info is not None and odesli is not None:
            for sibling_platform, sibling_url in (odesli.siblings or {}).items():
                info.siblings.setdefault(sibling_platform, sibling_url)
        else:
            info = odesli
        if info is None and platform == PLATFORM_TIDAL:
            info = await tidal_api_metadata(kind, object_id)
        if info is None:
            info = await public_search_metadata(
                platform, kind, object_id, url, storefront
            )
    elif platform == PLATFORM_AMAZON and url:
        info = await public_search_metadata(
            platform, kind, object_id, url, storefront
        )

    if info is None and url:
        info = await _from_odesli(url, kind, object_id, platform)

    if info is None and url and platform in (PLATFORM_QOBUZ, PLATFORM_TIDAL, PLATFORM_AMAZON):
        info = await page_metadata(url, platform, kind, object_id)

    if info is not None:
        await _attach_cover(info)
    return info


async def _from_spotify_oembed(url: str, kind: str, object_id: str) -> ReleaseInfo | None:
    data = await spotify_oembed(url)
    if not data:
        return None
    is_album = kind == KIND_ALBUM
    title = data["title"]
    return ReleaseInfo(
        platform=PLATFORM_SPOTIFY,
        kind=kind,
        object_id=object_id,
        title="" if is_album else title,
        artist=data["artist"] or "Unknown Artist",
        album_title=title if is_album else "",
        year="",
        release_date="",
        cover_url=data.get("thumbnail", ""),
        album_link=url if is_album else "",
        track_link="" if is_album else url,
        source="spotify-oembed",
    )


async def _from_odesli(url: str, kind: str, object_id: str, platform: str) -> ReleaseInfo | None:
    """Use song.link to name the release, and record the sibling links.

    The siblings are the actionable part: they let the caller fall through to a
    platform that does have a free metadata API, or to the local Deezer engine.
    """
    data = await odesli_links(url)
    if not data:
        return None
    title = _clean_metadata_text(data["title"])
    artist = _clean_metadata_text(data["artist"])
    if not title and not artist:
        return None
    is_track = kind == KIND_TRACK
    canonical = _canonical_request_link(platform, kind, object_id, url)
    return ReleaseInfo(
        platform=platform,
        kind=kind,
        object_id=object_id,
        title=title if is_track else "",
        artist=artist or "Unknown Artist",
        album_title="" if is_track else title,
        cover_url=data.get("artwork", ""),
        track_link=canonical if is_track else "",
        album_link="" if is_track else canonical,
        isrcs=[data["isrc"]] if data.get("isrc") else [],
        source="odesli",
        siblings=data.get("platforms", {}),
    )


def _extract_artist_playlist(source: str, options: dict) -> dict:
    import yt_dlp

    with yt_dlp.YoutubeDL(options) as downloader:
        return downloader.extract_info(source, download=False) or {}


async def artist_album_links(platform: str, object_id: str, storefront: str | None = None):
    """(artist_name, [release dicts]) for a discography request, or (None, []).

    Apple and Deezer expose catalogue APIs; YouTube Music and SoundCloud use a
    capped yt-dlp extraction. Other stores use their direct engine when their
    per-platform artist toggle is enabled.
    """
    if platform == PLATFORM_APPLE:
        data = await itunes_artist(object_id, storefront)
        if not data:
            return None, []
        country = storefront or "us"
        return data, [
            {
                "id": album["id"],
                "title": album["title"],
                "year": album["year"],
                "release_date": album["release_date"],
                "record_type": "album",
                "url": f"https://music.apple.com/{country}/album/id{album['id']}",
            }
            for album in data["albums"]
        ]
    if platform in (PLATFORM_YOUTUBE, PLATFORM_SOUNDCLOUD):
        source = (
            f"https://music.youtube.com/channel/{object_id}"
            if platform == PLATFORM_YOUTUBE
            else f"https://soundcloud.com/{object_id}"
        )
        options = {
            "quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
            "skip_download": True, "playlistend": pcfg.max_artist_releases() + 1,
        }
        try:
            data = await asyncio.to_thread(_extract_artist_playlist, source, options)
        except Exception as error:
            print(f"[meta] {platform} artist catalogue lookup failed: {error}")
            return None, []
        from urllib.parse import urljoin

        entries = data.get("entries") or []
        artist = data.get("uploader") or data.get("channel") or data.get("title") or object_id
        return {"name": artist}, [
            {
                "id": str(entry.get("id") or index),
                "title": entry.get("title") or "Unknown release",
                "year": "", "release_date": "", "record_type": "album",
                "url": urljoin(source + "/", entry.get("webpage_url") or entry.get("url")),
            }
            for index, entry in enumerate(entries, 1)
            if entry.get("webpage_url") or entry.get("url")
        ]
    if platform == PLATFORM_DEEZER:
        data = await deezer_artist(object_id)
        if not data:
            return None, []
        return data, [
            {
                **release,
                "url": f"https://www.deezer.com/album/{release['id']}",
            }
            for release in data["albums"]
        ]
    return None, []


def merge_info(engine_metadata, bridge_info: ReleaseInfo | None) -> dict:
    """Combine public enrichment with metadata read from downloaded audio.

    Direct-engine tags and paths are authoritative when a cross-store search is
    unverified or the bridge describes a different release. Provider APIs still
    enrich matching downloads with dates, artwork, links, and track details.
    """
    from dl_utils.deezer_utils import clean_filename

    def pick(*values):
        for value in values:
            if value:
                return value
        return None

    engine = engine_metadata

    def artist_value(*values):
        for value in values:
            candidate = str(value or "").strip()
            if candidate and candidate.casefold() != "unknown artist":
                return candidate
        return "Unknown Artist"

    def best_cover(*values):
        candidates = [value for value in values if value]
        if not candidates:
            return None

        def resolution(data):
            try:
                with Image.open(BytesIO(data)) as image:
                    width, height = image.size
                return width * height, len(data)
            except Exception:
                return 0, len(data)

        return max(candidates, key=resolution)

    track_artists = []
    for track in getattr(engine, "tracks_api_data", None) or []:
        if not isinstance(track, dict):
            continue
        artist_data = track.get("artist")
        if isinstance(artist_data, dict):
            artist_data = artist_data.get("name")
        track_artists.append(
            track.get("artist_name") or track.get("ART_NAME") or artist_data
        )

    def real_text(value, unknown=""):
        text = _clean_metadata_text(value)
        return "" if text.casefold() == unknown else text

    def normalized_words(value):
        ignored = {"album", "single", "ep", "version", "remaster", "remastered", "deluxe"}
        return {
            word for word in re.findall(r"[^\W_]+", str(value or "").casefold())
            if word not in ignored
        }

    def conflicts(engine_value, bridge_value):
        left, right = normalized_words(engine_value), normalized_words(bridge_value)
        return bool(left and right and not (left & right))

    engine_artist = real_text(engine and engine.artist, "unknown artist")
    engine_album = real_text(engine and engine.album_title, "unknown album")
    bridge_artist = real_text(bridge_info and bridge_info.artist, "unknown artist")
    bridge_album = real_text(bridge_info and bridge_info.album_title, "unknown album")
    direct_labels = ("streamrip", "dotify-cli", "gamdl", "amazon-cli")
    engine_verified = bool(
        engine_artist and engine_album
        and any(label in str(getattr(engine, "source_label", "")).casefold() for label in direct_labels)
    )
    unverified_search = bool(
        bridge_info and bridge_info.source in ("itunes-search", "deezer-search")
    )
    wild_conflict = bool(
        engine_verified and bridge_info and (
            conflicts(engine_album, bridge_album)
            or (engine_artist and bridge_artist and conflicts(engine_artist, bridge_artist))
        )
    )
    engine_wins = engine_verified and (unverified_search or wild_conflict)
    if not _real_release_metadata(bridge_info):
        engine_wins = engine_verified

    artist = artist_value(
        engine_artist if engine_wins else bridge_artist,
        bridge_artist if engine_wins else engine_artist,
        *track_artists,
    )
    album_title = pick(
        engine_album if engine_wins else bridge_album,
        bridge_album if engine_wins else engine_album,
        "Unknown Album",
    )
    year = pick(
        engine and engine.year if engine_wins else bridge_info and bridge_info.year,
        bridge_info and bridge_info.year if engine_wins else engine and engine.year,
        "",
    )
    release_date = pick(
        engine and engine.release_date if engine_wins else bridge_info and bridge_info.release_date,
        bridge_info and bridge_info.release_date if engine_wins else engine and engine.release_date,
        "",
    )
    cover = best_cover(bridge_info and bridge_info.cover_data, engine and engine.cover_data)
    qobuz_request = any(
        getattr(source, "platform", None) == PLATFORM_QOBUZ
        for source in (engine, bridge_info) if source
    ) or any(
        "qobuz" in str(getattr(source, "source_label", "")).casefold()
        for source in (engine,) if source
    )
    album_link = pick(bridge_info and bridge_info.album_link, engine and engine.album_link, "")
    track_link = pick(bridge_info and bridge_info.track_link, engine and engine.track_link, "")
    if qobuz_request:
        object_id = next(
            (
                str(getattr(source, "object_id", "") or "") for source in (bridge_info, engine)
                if source and getattr(source, "object_id", "")
            ),
            "",
        )
        if not object_id:
            candidate = pick(engine and engine.album_link, engine and engine.track_link, "")
            object_id = str(candidate or "").rstrip("/").rsplit("/", 1)[-1]
        kind = (
            KIND_TRACK if (bridge_info and bridge_info.kind == KIND_TRACK)
            or (engine and getattr(engine, "kind", None) == KIND_TRACK) else KIND_ALBUM
        )
        canonical = _canonical_request_link(PLATFORM_QOBUZ, kind, object_id)
        if kind == KIND_ALBUM:
            album_link = canonical
        else:
            track_link = canonical
        if bridge_info and kind == KIND_TRACK and not bridge_info.album_link:
            album_link = ""
    elif engine_wins:
        album_link = pick(engine and engine.album_link, album_link, "")
        track_link = pick(engine and engine.track_link, track_link, "")

    is_track = bool(
        (bridge_info and bridge_info.kind == KIND_TRACK)
        or (engine and getattr(engine, "kind", None) == KIND_TRACK)
    )
    # For a single track, `title` is the song and `album_title` the parent
    # release; for an album both are the release name.
    title = pick(
        engine and is_track and engine.title if engine_wins else bridge_info and is_track and _clean_metadata_text(bridge_info.title),
        bridge_info and is_track and _clean_metadata_text(bridge_info.title) if engine_wins else engine and is_track and engine.title,
        album_title,
    )

    tracks_api: list = []
    if bridge_info and bridge_info.tracks:
        tracks_api = [
            {
                "id": t.id,
                "title": t.title,
                "track_position": t.number,
                "contributors": [{"name": t.artist}] if t.artist else [],
            }
            for t in bridge_info.tracks
        ]

    artists_list = [artist] if artist else ["Unknown Artist"]

    return {
        "id": pick(bridge_info and bridge_info.object_id, "") or "",
        "title": title,
        "artist": artist,
        "album_title": album_title,
        "artists_list": artists_list,
        "release_date": release_date or year or "00/00/0000",
        "year": year or "0000",
        "album_link": album_link,
        "track_link": track_link,
        "cover_data": cover,
        # Delivery provenance and measured audio belong to the engine result,
        # never to the public metadata source used for enrichment.
        "source_label": getattr(engine, "source_label", "Direct Engine") if engine else "Direct Engine",
        "quality_label": getattr(engine, "quality_label", "") if engine else "",
        "tracks_api_data": tracks_api,
        "isrcs": pick(bridge_info and bridge_info.isrcs, engine and engine.isrcs, []) or [],
        "siblings": (bridge_info.siblings if bridge_info else {}) or {},
        "clean_artist": clean_filename(artist),
        "clean_album_title": clean_filename(album_title),
        "clean_title": clean_filename(title),
    }
