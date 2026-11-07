"""Public, credential-free metadata resolution across platforms.

The download engines know how to get bytes; they are not a reliable source for
*what* those bytes are. A CLI tool writes whatever folder template it likes,
and its track list does not always match the platform the user linked. So the canonical
release description (title, artist, album, year, cover, tracklist, ISRCs) is
resolved here first, from endpoints that need no account:

* **iTunes Lookup** (`https://itunes.apple.com/lookup`) — Apple Music albums,
  songs and artists, with full tracklists and artwork URLs. No auth.
* **Deezer public API** (`https://api.deezer.com`) — albums, tracks, and
  `search/isrc:<isrc>` cross-matching. No auth.
* **Spotify oEmbed** (`https://open.spotify.com/oembed`) — title and artwork
  for a track/album/plural link. No auth.
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
from urllib.parse import quote

import aiohttp
import certifi

from dl_utils.link_resolver import (
    KIND_ALBUM,
    KIND_TRACK,
    PLATFORM_AMAZON,
    PLATFORM_APPLE,
    PLATFORM_DEEZER,
    PLATFORM_QOBUZ,
    PLATFORM_SPOTIFY,
    PLATFORM_TIDAL,
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


async def _fetch_text(url: str, *, params: dict | None = None) -> str | None:
    session = await _SESSION.get()
    last_error: Exception | None = None
    for attempt in range(_ATTEMPTS):
        try:
            async with session.get(url, params=params) as resp:
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


async def _fetch_json(url: str, *, params: dict | None = None) -> dict | list | None:
    text = await _fetch_text(url, params=params)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[meta] bad JSON from {url}: {e}")
        return None


async def fetch_bytes(url: str) -> bytes | None:
    """Download an image (album art) with the shared session."""
    session = await _SESSION.get()
    for attempt in range(_ATTEMPTS):
        try:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.read()
                if data:
                    return data
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"[meta] art fetch failed ({url}), attempt {attempt + 1}: {e}")
            await asyncio.sleep(0.8 * (attempt + 1))


_ITUNES_SEARCH = "https://itunes.apple.com/search"
_QOBUZ_API = "https://play.qobuz.com/api.json/0.2"


def search_hint_from_url(url: str) -> str:
    """Extract human-readable Qobuz slug words for public catalogue searches."""
    match = re.search(r"/(?:album|track)/([^/]+)/[A-Za-z0-9]+(?:[/?#]|$)", url, re.I)
    if not match:
        return ""
    from urllib.parse import unquote

    return unquote(match.group(1)).replace("-", " ").strip()


async def qobuz_public(kind: str, object_id: str) -> ReleaseInfo | None:
    """Read Qobuz's public catalogue API when web pages block scraping."""
    endpoint = "album/get" if kind == KIND_ALBUM else "track/get"
    payload = await _fetch_json(
        f"{_QOBUZ_API}/{endpoint}", params={f"{kind}_id": object_id}
    )
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    if isinstance(payload.get(kind), dict):
        payload = payload[kind]
    if not payload.get("title"):
        return None

    album = payload.get("album") or {}
    artist_data = payload.get("artist") or album.get("artist") or {}
    artist = artist_data.get("name") or artist_data.get("display") or ""
    album_title = album.get("title") or (payload.get("title", "") if kind == KIND_ALBUM else "")
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
    title = payload.get("title") or album_title
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
        cover_url=image.get("large") or image.get("extralarge") or image.get("small") or "",
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
        cover_url=item.get("cover_xl") or item.get("cover_big") or album.get("cover_xl", ""),
        track_link=link if kind == KIND_TRACK else album.get("link", ""),
        album_link=link if kind == KIND_ALBUM else album.get("link", ""),
        source="deezer-search",
        siblings={"deezer": link},
    )


async def _cross_match_public(info: ReleaseInfo, term: str, kind: str, storefront=None) -> ReleaseInfo:
    """Add public sibling links and fill incomplete page-title metadata."""
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
        if info.artist in ("", "Unknown Artist") or not (info.title or info.album_title):
            return candidate
    return info


async def public_search_metadata(
    platform: str, kind: str, object_id: str, url: str, storefront=None
) -> ReleaseInfo | None:
    """Resolve Qobuz/Amazon metadata without relying on api.song.link."""
    info = (
        await qobuz_public(kind, object_id) if platform == PLATFORM_QOBUZ else None
    )
    if info is None:
        info = await page_metadata(url, platform, kind, object_id)
    term = " ".join(
        part for part in (
            info.artist if info and info.artist != "Unknown Artist" else "",
            info.title if kind == KIND_TRACK else info.album_title if info else "",
        ) if part
    ).strip() or search_hint_from_url(url)
    if not term:
        return info
    return await _cross_match_public(info, term, kind, storefront) if info else await itunes_search(term, kind, storefront) or await deezer_search(term, kind)

    return None


# ---------------------------------------------------------------------------
# iTunes Lookup (Apple Music)
# ---------------------------------------------------------------------------
def _apple_artwork(url: str, size: int = _APPLE_ART_MAX) -> str:
    """Request the largest JPEG the CDN will serve for an artwork URL."""
    if not url:
        return ""
    return re.sub(r"/\d+x\d+(bb|png|jpg)?", f"/{size}x{size}", url)


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
def _deezer_art(cover: str, size: int = 1000) -> str:
    if not cover:
        return ""
    return re.sub(r"/\d+x\d+", f"{size}x{size}", cover)


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
        cover_url=_deezer_art(album.get("cover_xl") or album.get("cover_big") or ""),
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
    return {
        "artist": parts[0] if len(parts) > 1 else "",
        "title": parts[-1] if parts else title,
        "thumbnail": payload.get("thumbnail_url", "") or "",
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
        "artwork": artwork,
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
                structured_artist = structured_artist or artist_data.get("name", "")
            if kind == KIND_ALBUM and item.get("@type") in ("MusicAlbum", "Product"):
                structured_title = structured_title or item.get("name", "")
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
    title = re.sub(r"\s+(?:-|–|—|\|)\s*(?:Qobuz|TIDAL|Amazon Music).*$", "", title, flags=re.I)
    artist, release_title = "", title
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
    artist = artist or structured_artist
    release_title = release_title or structured_title
    if not release_title:
        return None
    return ReleaseInfo(
        platform=platform,
        kind=kind,
        object_id=object_id,
        title=release_title if kind == KIND_TRACK else "",
        artist=artist or "Unknown Artist",
        album_title="" if kind == KIND_TRACK else release_title,
        year=_year_from(structured_date),
        release_date=structured_date[:10],
        cover_url=_meta_content(text, "og:image") or structured_cover,
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
    elif platform in (PLATFORM_QOBUZ, PLATFORM_AMAZON) and url:
        info = await public_search_metadata(
            platform, kind, object_id, url, storefront
        )
    elif platform == PLATFORM_TIDAL and url:
        info = await _from_odesli(url, kind, object_id, platform)

    if info is None and url:
        # Last resort that still needs no credentials: ask song.link who this is.
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
    is_track = kind == KIND_TRACK
    return ReleaseInfo(
        platform=platform,
        kind=kind,
        object_id=object_id,
        title=data["title"] if is_track else "",
        artist=data["artist"] or "Unknown Artist",
        album_title="" if is_track else data["title"],
        cover_url=data.get("artwork", ""),
        track_link=url if is_track else "",
        album_link="" if is_track else url,
        isrcs=[data["isrc"]] if data.get("isrc") else [],
        source="odesli",
        siblings=data.get("platforms", {}),
    )


async def artist_album_links(platform: str, object_id: str, storefront: str | None = None):
    """(artist_name, [release dicts]) for a discography request, or (None, []).

    Apple has a free endpoint that lists an artist's catalogue directly. Every
    other platform's artist page needs an authenticated API, so the release
    list comes from that platform's own public web page when available;
    artist discography downloads remain restricted to Deezer.
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
    """Combine bridge metadata with whatever the engine inferred from the tree.

    `engine_metadata` is a `provider_engines.ReleaseMetadata` (or None). The
    bridge wins for the fields a public API reports reliably — artist, album,
    year, release date, cover, links — because an engine's folder name is a
    template artefact, not a source of truth. Everything else falls back to the
    engine, so a metadata outage never costs the user audio that already
    downloaded.

    Returns the dict shape `archive_delivery.build_release_entries` and the
    caption builders expect.
    """
    from dl_utils.deezer_utils import clean_filename

    def pick(*values):
        for value in values:
            if value:
                return value
        return None

    engine = engine_metadata
    artist = pick(bridge_info and bridge_info.artist, engine and engine.artist, "Unknown Artist")
    album_title = pick(
        bridge_info and bridge_info.album_title,
        engine and engine.album_title,
        "Unknown Album",
    )
    year = pick(bridge_info and bridge_info.year, engine and engine.year, "")
    release_date = pick(bridge_info and bridge_info.release_date, engine and engine.release_date, "")
    cover = pick(bridge_info and bridge_info.cover_data, engine and engine.cover_data)
    album_link = pick(bridge_info and bridge_info.album_link, engine and engine.album_link, "")
    track_link = pick(bridge_info and bridge_info.track_link, engine and engine.track_link, "")

    is_track = bool(
        (bridge_info and bridge_info.kind == KIND_TRACK)
        or (engine and getattr(engine, "kind", None) == KIND_TRACK)
    )
    # For a single track, `title` is the song and `album_title` the parent
    # release; for an album both are the release name.
    title = pick(
        bridge_info and is_track and bridge_info.title,
        engine and is_track and engine.title,
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
