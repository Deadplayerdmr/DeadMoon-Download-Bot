"""Spotify public metadata and private credential staging for dotify-cli."""

import functools
import json
import os
import re
import shutil
from html import unescape
from pathlib import Path

import requests

from dl_utils import platform_config as pcfg
from utils import get_env_or_token_file

_TRACK_CONTEXT: dict[str, dict] = {}


def embed_entity(kind: str, object_id: str, timeout: int = 20) -> dict:
    """Read Spotify's public embed metadata without using account credentials."""
    response = requests.get(
        f"https://open.spotify.com/embed/{kind}/{object_id}", timeout=timeout
    )
    response.raise_for_status()
    match = re.search(
        r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
        response.text,
        re.S,
    )
    if not match:
        raise ValueError(f"Spotify embed {kind} metadata was not present")
    page = json.loads(unescape(match.group(1)))
    entity = page["props"]["pageProps"]["state"]["data"]["entity"]
    if not isinstance(entity, dict):
        raise ValueError(f"Spotify embed {kind} returned invalid metadata")
    return entity


def _public_track_context(track_id: str, timeout: int = 20, kind: str = "track") -> dict:
    response = requests.get(
        f"https://open.spotify.com/{kind}/{track_id}", timeout=timeout
    )
    response.raise_for_status()
    description = re.search(
        r'<meta\s+property="og:description"\s+content="([^"]*)"', response.text
    )
    release_date = re.search(
        r'<meta\s+name="music:release_date"\s+content="([^"]*)"', response.text
    )
    if not description:
        raise ValueError("Spotify public track metadata was not present")
    fields = unescape(description.group(1)).split(" · ")
    if len(fields) < 2:
        raise ValueError("Spotify public track album context was not present")
    context = {
        "name": fields[1] if kind == "track" else "",
        "release_date": unescape(release_date.group(1)) if release_date else "",
    }
    if kind == "album":
        context["artist"] = fields[0]
    return context


def _artist(entity: dict) -> str:
    artist = entity.get("subtitle") or entity.get("artist") or ""
    artists = entity.get("artists") or []
    if artists:
        first = artists[0]
        if isinstance(first, str):
            artist = artist or first
        elif isinstance(first, dict):
            artist = artist or first.get("name") or first.get("title") or ""
    if not artist or str(artist).strip().casefold() == "unknown artist":
        artist = next(
            (
                str(track.get("subtitle") or track.get("artist") or "").strip()
                for track in entity.get("trackList") or []
                if str(track.get("subtitle") or track.get("artist") or "").strip()
            ),
            "",
        )
    return str(artist).strip() or "Unknown Artist"


def _artist_objects(entity: dict) -> list[dict]:
    return [
        {"name": name.strip(), "id": "", "href": "", "genres": []}
        for name in _artist(entity).split(",")
        if name.strip()
    ] or [{"name": "Unknown Artist", "id": "", "href": "", "genres": []}]


def _integer(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _images(entity: dict) -> list[dict]:
    from dl_utils.metadata_bridge import upgrade_cover_url

    images = entity.get("visualIdentity", {}).get("image") or []
    images = [image for image in images if image.get("url")]
    if not images:
        return []
    image = max(images, key=lambda item: _integer(
        item.get("maxWidth") or item.get("width"), 0
    ))
    return [{
        "url": upgrade_cover_url(image["url"]),
        "width": image.get("maxWidth") or image.get("width") or 0,
        "height": image.get("maxHeight") or image.get("height") or 0,
    }]


def _release_date(entity: dict) -> str:
    release_date = entity.get("releaseDate") or ""
    if isinstance(release_date, dict):
        if release_date.get("isoString"):
            return str(release_date["isoString"])[:10]
        year = release_date.get("year") or "2020"
        return (
            f"{year:04}-{int(release_date.get('month') or 1):02}-"
            f"{int(release_date.get('day') or 1):02}"
        )
    return str(release_date) or "2020-01-01"


def _album(entity: dict, album_id: str) -> dict:
    return {
        "id": album_id,
        "name": entity.get("title") or entity.get("name") or album_id,
        "artists": _artist_objects(entity),
        "release_date": _release_date(entity),
        "images": _images(entity),
        "tracks": {"items": []},
    }


def _track(entity: dict, track_id: str, album: dict | None = None) -> dict:
    album = album or {
        "id": "",
        "name": entity.get("album") or "Unknown Album",
        "artists": _artist_objects(entity),
        "release_date": _release_date(entity),
        "images": _images(entity),
    }
    album = dict(album)
    album.pop("tracks", None)
    album.setdefault("id", "")
    album["name"] = album.get("name") or "Unknown Album"
    album["artists"] = album.get("artists") or _artist_objects(entity)
    album["release_date"] = album.get("release_date") or "2020-01-01"
    album.setdefault("images", [])
    return {
        "id": track_id,
        "name": entity.get("title") or entity.get("name") or track_id,
        "artists": _artist_objects(entity),
        "album": album,
        "disc_number": _integer(entity.get("disc_number", entity.get("discNumber")), 1),
        "track_number": _integer(
            entity.get("track_number", entity.get("trackNumber")), 1
        ),
        "duration_ms": _integer(entity.get("duration_ms", entity.get("duration")), 0),
        "explicit": bool(entity.get("explicit", False)),
        "is_playable": bool(entity.get("is_playable", entity.get("isPlayable", True))),
        "external_ids": dict(entity.get("external_ids") or {}),
        "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
    }


@functools.lru_cache(maxsize=32)
def album_metadata(album_id: str) -> dict:
    entity = embed_entity("album", album_id)
    if _artist(entity) == "Unknown Artist":
        try:
            context = _public_track_context(album_id, kind="album")
            if context.get("artist"):
                entity = {**entity, "subtitle": context["artist"]}
        except Exception as error:
            print(f"[spotify] public album artist fallback failed: {error}")
    album = _album(entity, album_id)
    tracks = [
        item
        for item in entity.get("trackList") or []
        if item.get("uri", "").startswith("spotify:track:")
    ]
    album["tracks"]["items"] = [
        _track({**item, "trackNumber": item.get("trackNumber", index)}, track_id, album)
        for index, item in enumerate(tracks, 1)
        for track_id in [item["uri"].rsplit(":", 1)[-1]]
    ]
    for item in album["tracks"]["items"]:
        _TRACK_CONTEXT[item["id"]] = item
    return album


def track_metadata(track_id: str) -> dict:
    cached = _TRACK_CONTEXT.get(track_id)
    if cached is not None:
        return cached
    entity = embed_entity("track", track_id)
    related = entity.get("relatedEntityUri") or ""
    if related.startswith("spotify:album:"):
        try:
            album = album_metadata(related.rsplit(":", 1)[-1])
            cached = _TRACK_CONTEXT.get(track_id)
            if cached is not None:
                return cached
            track = _track(entity, track_id, album)
            _TRACK_CONTEXT[track_id] = track
            return track
        except Exception:
            pass
    context = _public_track_context(track_id)
    entity = {**entity, "album": context["name"], "releaseDate": context["release_date"]}
    track = _track(entity, track_id)
    _TRACK_CONTEXT[track_id] = track
    return track


def expected_tracks(metadata: dict) -> list[dict]:
    """Convert public Web-shaped metadata to archive issue-summary entries."""
    return [
        {
            "SNG_ID": track["id"],
            "id": track["id"],
            "SNG_TITLE": track["name"],
            "title": track["name"],
            "ART_NAME": (track.get("artists") or [{}])[0].get("name", "Unknown Artist"),
            "artist_name": (track.get("artists") or [{}])[0].get("name", "Unknown Artist"),
            "TRACK_NUMBER": str(track.get("track_number") or index),
            "DISC_NUMBER": str(track.get("disc_number") or 1),
            "is_playable": track.get("is_playable", True),
        }
        for index, track in enumerate(metadata.get("tracks", {}).get("items", []), 1)
    ]


def _private_copy(source: Path, destination: Path) -> None:
    with source.open("rb") as src:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as dst:
            os.fchmod(dst.fileno(), 0o600)
            shutil.copyfileobj(src, dst)


def stage_cookies(workdir: Path) -> Path:
    """Stage Netscape cookies, or adapt a JSON bundle containing sp_dc."""
    destination = workdir / "spotify_cookies.txt"
    configured = get_env_or_token_file("SPOTIFY_COOKIES_PATH", "").strip()
    if configured:
        source = Path(configured).expanduser()
        if pcfg.spotify_cookie_values(source).get("sp_dc"):
            try:
                _private_copy(source, destination)
            except OSError as error:
                raise ValueError(f"SPOTIFY: could not stage cookies: {error}") from error
            return destination

    legacy = get_env_or_token_file("SPOTIFY_CREDENTIALS_PATH", "").strip()
    data = pcfg.spotify_credentials_data(Path(legacy).expanduser()) if legacy else None
    sp_dc = (data or {}).get("sp_dc", "").strip()
    if sp_dc:
        body = (
            "# Netscape HTTP Cookie File\n"
            f".spotify.com\tTRUE\t/\tTRUE\t0\tsp_dc\t{sp_dc}\n"
        )
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(body)
        return destination
    raise ValueError(pcfg.spotify_credentials_error())
