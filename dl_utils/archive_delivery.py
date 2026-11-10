"""Shared packaging, delivery and auto-delete helpers.

This module holds the battle-tested archive workflow that used to live entirely
inside `handlers/deezer.py`. It was extracted — not rewritten — so every
platform produces byte-identical output:

* single / album:  `<Artist> - <Album> [Year]/NN - <Artist> - <Title>.<ext>`
                   plus `cover.jpg` in the same folder
* artist:          `<Artist>/<Artist> - <Album> [Year]/NN - <Artist> - <Title>.<ext>`
* archive name:    `<Clean_Artist>_-_<Clean_Title>_(<8-char md5>).zip`
                   written to COPY_FILES_PATH and linked through
                   FILE_LINK_TEMPLATE, or split into <48 MB parts and sent
                   directly when no hosting directory is configured

`handlers/deezer.py` imports everything from here and re-exports the original
names, so existing call sites, tests and monkeypatching keep working.

Contract for the two data structures every engine must produce
--------------------------------------------------------------
`metadata` (a release):
    artist, title, album_title, year, release_date, clean_artist,
    clean_album_title, clean_title, cover_data (bytes), album_link,
    track_link, artists_list (list[str]), tracks_api_data (list[dict], optional)

`dl_tracks_info` (list, one dict per downloaded track):
    song_path (str), download_dir (str), song_name (str), artist_name (str),
    file_extension (str, e.g. ".flac"), TRACK_NUMBER (str|int, optional),
    SNG_ID (str, optional)

Any new provider engine only has to normalise its output into those two shapes
to reuse the whole zip-and-link pipeline.
"""

import asyncio
import hashlib
import html
import math
import re
import time
import traceback
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image
from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, FSInputFile, InputMediaAudio
from unidecode import unidecode

from dl_utils import platform_config
from dl_utils.deezer_utils import clean_filename, get_audio_duration
from utils import TMP_DIR, __

# Telegram caps a bot's file upload at 50 MB; 48 MB leaves room for the
# container overhead that pushes an exactly-50 MB archive over the limit.
TELEGRAM_MAX_UPLOAD_BYTES = 48 * 1024 * 1024


class _ProgressEditor:
    """Throttled Telegram text editor shared by every download handler."""

    def __init__(self, message, interval: float = 2.0):
        self.message = message
        self.interval = interval
        self.last_text = ""
        self.last_edit = 0.0

    async def __call__(self, text: str, force: bool = False) -> None:
        if not self.message or text == self.last_text:
            return
        now = time.monotonic()
        if not force and now - self.last_edit < self.interval:
            return
        try:
            await self.message.edit_text(text)
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).lower():
                return
        except Exception as error:
            print(f"[progress] Telegram edit failed: {error}")
            return
        self.last_text = text
        self.last_edit = now




def format_file_size(num_bytes: int) -> str:
    """Format a byte count for concise Telegram captions."""
    size = max(0, float(num_bytes or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def prepare_cover_bytes(cover_data: Optional[bytes]) -> Optional[bytes]:
    """Keep full-resolution artwork within Telegram's photo limits."""
    if not cover_data or not isinstance(cover_data, (bytes, bytearray)):
        return None
    data = bytes(cover_data)
    try:
        image = Image.open(BytesIO(data))
        image.load()
        if max(image.size) > 2560:
            image.thumbnail((2560, 2560))
        image = image.convert("RGB")
        output = BytesIO()
        for quality in (92, 85, 75, 65):
            output.seek(0)
            output.truncate()
            image.save(output, format="JPEG", quality=quality)
            if output.tell() <= 9_500_000:
                return output.getvalue()
        while max(image.size) > 320:
            image.thumbnail((int(image.width * 0.8), int(image.height * 0.8)))
            output.seek(0)
            output.truncate()
            image.save(output, format="JPEG", quality=65)
            if output.tell() <= 9_500_000:
                return output.getvalue()
        return None
    except Exception:
        # Some provider fixtures contain a JPEG signature but truncated data.
        return data if data.startswith((b"\xff\xd8\xff", b"\xff\xd8")) else None


_prepare_cover_bytes = prepare_cover_bytes


def _meta_dict(metadata) -> dict:
    if isinstance(metadata, dict):
        return dict(metadata)
    return {
        key: getattr(metadata, key)
        for key in (
            "title", "artist", "album_title", "year", "release_date",
            "clean_artist", "clean_album_title", "clean_title", "cover_data",
            "album_link", "track_link", "artists_list", "tracks_api_data",
            "source_label", "quality_label", "caption_suffix",
        )
        if hasattr(metadata, key)
    }


def _valid_artist(value) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("title")
    artist = str(value or "").strip()
    return "" if artist.casefold() in ("", "unknown artist") else artist


def _metadata_with_track_artist(metadata, tracks) -> dict:
    resolved = _meta_dict(metadata)

    def track_artist(track):
        return _valid_artist(track.get("artist_name")) or _valid_artist(
            track.get("ART_NAME")
        ) or _valid_artist(track.get("artist"))

    if not _valid_artist(resolved.get("artist")):
        artist = next(
            (artist for track in tracks or [] if (artist := track_artist(track))),
            "",
        )
        if artist:
            resolved["artist"] = artist
            resolved["clean_artist"] = clean_filename(artist)
            resolved["artists_list"] = [artist]
    resolved.setdefault("artist", "Unknown Artist")
    return resolved


def _track_extension(track: dict) -> str:
    extension = str(track.get("file_extension") or "").strip().lower()
    if not extension:
        extension = Path(str(track.get("song_path") or "")).suffix.lower()
    return f".{extension.lstrip('.')}" if extension else ""


def _track_metadata(metadata, track: dict) -> dict:
    track_meta = _meta_dict(metadata)
    track_meta["title"] = str(
        track.get("song_name") or track.get("title") or track.get("SNG_TITLE")
        or track_meta.get("title") or "Unknown Track"
    ).strip()
    track_artist = _valid_artist(track.get("artist_name")) or _valid_artist(
        track.get("ART_NAME")
    ) or _valid_artist(track.get("artist")) or _valid_artist(track_meta.get("artist"))
    track_meta["artist"] = track_artist or "Unknown Artist"
    return track_meta


def _audio_caption(metadata, kind: str, size: int, source: str = "", extension: str = "") -> str:
    get = metadata.get if isinstance(metadata, dict) else lambda key, default="": getattr(metadata, key, default)
    title = get("title") if kind == "Track" else get("album_title") or get("title", "")
    artist = _valid_artist(get("artist", "")) or "Unknown Artist"
    heading = f"<b>{kind}: {html.escape(str(title))}</b>"
    suffix = f" ({extension})" if extension else ""
    caption = (
        f"🌙 {heading}\n🎧 {html.escape(str(artist))}\n"
        f"📦 <b>Size:</b> {format_file_size(size)}{suffix}"
    )
    if source:
        caption += f"\nSource: {html.escape(str(source))}"
    return caption




def _audio_thumbnail(metadata):
    data = metadata.get("cover_data") if isinstance(metadata, dict) else getattr(metadata, "cover_data", None)
    if not data:
        return None
    try:
        image = Image.open(BytesIO(data)).convert("RGB")
        image.thumbnail((320, 320))
        output = BytesIO()
        image.save(output, format="JPEG", quality=85)
        return BufferedInputFile(output.getvalue(), filename="cover.jpg")
    except (OSError, ValueError):
        return None


def _audio_duration(metadata, track, path: Path) -> int | None:
    value = track.get("duration") or track.get("duration_ms")
    if not value and isinstance(metadata, dict):
        for row in metadata.get("tracks_api_data", []):
            if str(row.get("id", "")) == str(track.get("SNG_ID", "")):
                value = row.get("duration")
                break
    if value:
        value = int(value)
        return value // 1000 if value > 1000 else value
    try:
        return get_audio_duration(str(path))
    except Exception:
        return None
def _thumbnail(cover_data):
    if not cover_data:
        return None
    return BufferedInputFile(cover_data, filename="cover.jpg")


async def send_track_audio(event, metadata, track):
    """Upload one original-format audio attachment without recompression."""
    path = Path(track["song_path"])
    track_meta = _track_metadata(_metadata_with_track_artist(metadata, [track]), track)
    caption = _audio_caption(
        track_meta, "Track", path.stat().st_size, extension=_track_extension(track)
    )
    audio = await event.answer_audio(
        FSInputFile(path),
        title=track_meta["title"], performer=track_meta["artist"],
        thumbnail=_thumbnail(make_audio_thumbnail(track_meta.get("cover_data"))),
        duration=_audio_duration(track_meta, track, path),
        caption=caption, parse_mode="HTML",
    )
    if audio:
        await schedule_messages_deletion([audio], platform_config.auto_delete_seconds())
    return audio


async def send_album_audio(event, metadata, dl_tracks_info):
    """Send an album header followed by tracks in their original formats."""
    metadata = _metadata_with_track_artist(metadata, dl_tracks_info)
    size = sum(Path(track["song_path"]).stat().st_size for track in dl_tracks_info)
    extensions = ", ".join(dict.fromkeys(
        extension for track in dl_tracks_info
        if (extension := _track_extension(track))
    ))
    caption = _audio_caption(metadata, "Album", size, extension=extensions)
    cover = metadata.get("cover_data")
    if cover and platform_config.send_album_cover():
        prepared_cover = prepare_cover_bytes(cover)
    else:
        prepared_cover = None
    if prepared_cover:
        try:
            header = await event.answer_photo(
                BufferedInputFile(prepared_cover, filename="cover.jpg"),
                caption=caption,
                parse_mode="HTML",
            )
        except Exception as error:
            print(f"Error sending album cover: {error}")
            header = await event.answer(caption, parse_mode="HTML")
    else:
        header = await event.answer(caption, parse_mode="HTML")
    messages = [header] if header else []
    for track in dl_tracks_info:
        audio = await send_track_audio(event, metadata, track)
        if audio:
            messages.append(audio)
    if header:
        await schedule_messages_deletion([header], platform_config.auto_delete_seconds())
    return messages


async def send_artist_audio(event, metadata, artist_name, artist_dir):
    """Deliver a discography through the shared release audio workflow."""
    await event.answer(
        f"🌕 <b>Artist: {html.escape(artist_name)}</b>\n"
        f"📦 <b>Size:</b> {format_file_size(sum(p.stat().st_size for p in artist_dir.rglob('*') if p.is_file()))}",
        parse_mode="HTML",
    )
    messages = []
    from dl_utils import provider_engines

    for release in provider_engines.discover_releases(artist_dir):
        cover_data = provider_engines.read_cover(release, metadata.artist)
        release_metadata = provider_engines.infer_release_from_paths(
            release, metadata.artist, artist_dir=artist_dir
        )
        tracks = provider_engines.tracks_from_paths(release, release_metadata)
        if cover_data:
            release_metadata.cover_data = cover_data
        messages.extend(await send_album_audio(event, release_metadata, tracks))
    return messages

# ---------------------------------------------------------------------------
# Auto-delete
# ---------------------------------------------------------------------------
async def schedule_message_deletion(message: types.Message, delay_seconds: int):
    """Delete a bot message after `delay_seconds`, honouring AUTO_DELETE_ENABLED."""
    if not platform_config.auto_delete_enabled():
        return

    async def delete_after_delay():
        await asyncio.sleep(delay_seconds)
        try:
            await message.delete()
            print(f"Auto-deleted message {message.message_id} after {delay_seconds} seconds")
        except Exception as e:
            print(f"Failed to auto-delete message {message.message_id}: {e}")

    asyncio.create_task(delete_after_delay())


async def schedule_file_deletion(file_path: Path, delay_seconds: int):
    """Delete a hosted server file after `delay_seconds`.

    This is what keeps the ephemeral-acquisition promise: an archive written to
    COPY_FILES_PATH is removed on the same timer as the Telegram message that
    linked to it, so nothing outlives AUTO_DELETE_SECONDS.
    """
    if not platform_config.auto_delete_enabled():
        return

    async def delete_after_delay():
        await asyncio.sleep(delay_seconds)
        try:
            path = Path(file_path)
            if path.exists():
                path.unlink()
                print(f"Auto-deleted server file {path} after {delay_seconds} seconds")
        except Exception as e:
            print(f"Failed to auto-delete server file {file_path}: {e}")

    asyncio.create_task(delete_after_delay())


async def schedule_messages_deletion(messages: list, delay_seconds: int):
    """Schedule every message in `messages` for auto-deletion."""
    if not platform_config.auto_delete_enabled():
        return
    for message in messages:
        await schedule_message_deletion(message, delay_seconds)


async def _safe_delete(message):
    """Delete a progress message, ignoring the usual 'already deleted' errors."""
    if message is None:
        return
    try:
        await message.delete()
    except Exception as e:
        print(f"Could not delete progress message: {e}")


# ---------------------------------------------------------------------------
# Captions & thumbnails
# ---------------------------------------------------------------------------
def summarize_track_issues(tracks, expected_tracks=None) -> tuple[list[str], list[str]]:
    """Build deterministic per-track fallback/failure summary lines."""
    tracks = [track for track in (tracks or []) if isinstance(track, dict) and track]
    expected = [track for track in (expected_tracks or []) if isinstance(track, dict) and track]
    def track_id(track):
        return str(track.get("SNG_ID") or track.get("id") or "")

    def artist_name(track):
        artist = track.get("artist_name") or track.get("ART_NAME") or track.get("artist")
        return artist.get("name", "Unknown Artist") if isinstance(artist, dict) else (artist or "Unknown Artist")

    def track_title(track, index):
        return track.get("song_name") or track.get("SNG_TITLE") or track.get("title") or f"Track {index}"

    by_id = {track_id(track): track for track in tracks if track_id(track)}
    fallbacks, failures = [], []
    for index, item in enumerate(expected, 1):
        actual = by_id.get(track_id(item))
        info = actual or item
        artist = artist_name(info)
        title = track_title(info, index)
        prefix = f"Track {index:02d} - {artist} - {title}"
        if actual is None:
            failures.append(f"Failures: {prefix} was not available")
        elif actual.get("format_fallback"):
            fallbacks.append(f"Fallback: {prefix} was not available as lossless quality on Deezer")
    if not expected:
        for index, track in enumerate(tracks, 1):
            if not track.get("format_fallback"):
                continue
            number = int(track.get("TRACK_NUMBER") or index)
            artist = track.get("artist_name") or track.get("ART_NAME") or "Unknown Artist"
            title = track.get("song_name") or track.get("SNG_TITLE") or f"Track {number}"
            fallbacks.append(
                f"Fallback: Track {number:02d} - {artist} - {title} "
                "was not available as lossless quality on Deezer"
            )
    return fallbacks, failures


def append_issue_summary(caption: str, tracks, expected_tracks=None) -> str:
    fallbacks, failures = summarize_track_issues(tracks, expected_tracks)
    if not fallbacks and not failures:
        return caption
    lines = (fallbacks + failures)
    return caption.rstrip() + "\n\n" + "\n".join(html.escape(line, quote=True) for line in lines)


def make_audio_thumbnail(cover_data: bytes) -> bytes | None:
    """Build a Telegram-compliant audio thumbnail from cover bytes.

    Telegram's server-side extraction of embedded album art is unreliable
    (it intermittently shows no cover for some tracks), so an explicit
    thumbnail is passed instead. Telegram requires JPEG, <= 320x320, < 200 kB.
    """
    if not cover_data:
        return None
    try:
        img = Image.open(BytesIO(cover_data))
        img.thumbnail((320, 320))
        buf = BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        print(f"Warning: could not build audio thumbnail: {e}")
        return None


def _source_quality(metadata) -> str:
    source = metadata.get("source_label") or "Deezer"
    quality = metadata.get("quality_label") or ""
    return f"{source} · {quality}" if quality else source


def get_track_caption(metadata):
    """Caption for a single track."""
    links = "\n".join(
        link
        for link in (
            f'<a href="{metadata["album_link"]}">{__("album_link")}</a>'
            if metadata.get("album_link")
            else "",
            f'<a href="{metadata["track_link"]}">{__("track_link")}</a>'
            if metadata.get("track_link")
            else "",
        )
        if link
    )
    return (
        "<b>Track: {title}</b>\n"
        "{artist} - {release_date}\n"
        "Source: " + _source_quality(metadata)
    ).format(**metadata) + (f"\n{links}" if links else "") + str(
        metadata.get("caption_suffix", "")
    )


def get_album_caption(metadata):
    """Caption for an album."""
    return (
        "<b>Album: {title}</b>\n"
        "{artist} - {release_date}\n"
        "Source: " + _source_quality(metadata) + "\n"
        '<a href="{album_link}">' + __("album_link") + "</a>"
    ).format(**metadata) + str(metadata.get("caption_suffix", ""))


def get_artist_caption(artist_metadata, release_count: int, track_count: int):
    """Caption for a full artist discography."""
    return (
        f"<b>Artist: {artist_metadata['name']}</b>\n"
        f"{release_count} release(s), {track_count} track(s)\n"
        f'<a href="{artist_metadata["link"]}">' + __("artist_link") + "</a>"
    )


def get_user_infos(event: types.Message):
    """(user_id, username, first_name) for the message's sender."""
    user_id = username = first_name = None
    if event.from_user:
        user_id = event.from_user.id
        username = event.from_user.username
        first_name = event.from_user.first_name
    return user_id, username, first_name


# ---------------------------------------------------------------------------
# Archive layout
# ---------------------------------------------------------------------------
def build_release_entries(metadata, dl_tracks_info, parent_dir: str | None = None) -> dict:
    """Map downloaded songs to their destination path inside a zip archive.

    Layout: an '<Artist> - <Album> [Year]' folder holding
    'NN - Artist - Title.ext' files plus cover.jpg. Passing parent_dir nests
    every entry one level deeper, which is how artist downloads get their
    per-artist top folder without changing the per-release naming at all.

    Returns {absolute_source_path: path_inside_zip}.
    """
    if not dl_tracks_info:
        raise ValueError("No downloaded tracks provided for zipping.")

    # All tracks of one release share the same download directory.
    source_dir_str = next(
        (t["download_dir"] for t in dl_tracks_info if "download_dir" in t), None
    )
    if not source_dir_str:
        raise ValueError(
            "Could not determine source directory from downloaded tracks info."
        )
    source_dir = Path(source_dir_str)

    cover_path = source_dir / "cover.jpg"  # Standardized cover name

    try:
        with open(cover_path, "wb") as f:
            f.write(metadata["cover_data"])
    except IOError as e:
        print(f"Error writing cover file {cover_path}: {e}")
        cover_path = None  # Proceed without cover
    except TypeError as e:
        print(f"Error with cover data type: {e}. Cover data: {metadata.get('cover_data')}")
        cover_path = None

    internal_dir_name = clean_filename(
        f"{metadata['clean_artist']} - {metadata['clean_album_title']} [{metadata['year']}]"
    )
    if parent_dir:
        internal_dir_name = f"{clean_filename(parent_dir)}/{internal_dir_name}"

    files_to_zip = {}

    if cover_path and cover_path.exists():
        files_to_zip[str(cover_path)] = f"{internal_dir_name}/cover.jpg"
    else:
        print("Cover file not available or not written, skipping inclusion in zip.")

    try:
        dl_tracks_info.sort(
            key=lambda t: int(t.get("TRACK_NUMBER", "999"))
            if str(t.get("TRACK_NUMBER", "999")).isdigit()
            else 999
        )
    except Exception as sort_e:
        print(
            "Warning: Could not sort tracks by track number for zipping. "
            f"Proceeding in potentially unsorted order. Error: {sort_e}"
        )

    api_tracks_by_id = {
        str(t.get("id", "")): t for t in metadata.get("tracks_api_data", [])
    }

    for i, track in enumerate(dl_tracks_info):
        track_num_val = track.get("TRACK_NUMBER")
        track_num_str = (
            str(track_num_val).zfill(2)
            if track_num_val and str(track_num_val).isdigit()
            else str(i + 1).zfill(2)
        )
        # Precedence for the artist string: API contributors (per-track
        # endpoint) > the artist built at download time from the source's own
        # contributor list > the album artist. The album-level endpoints carry
        # no per-track contributors and would silently drop featured credits.
        potential_id = Path(track.get("song_path", "")).stem
        api_track = api_tracks_by_id.get(
            str(track.get("SNG_ID", ""))
        ) or api_tracks_by_id.get(potential_id)

        title = track.get("song_name", f"Track {track_num_str}")
        if api_track:
            title = api_track.get("title", title)
            contributors = [
                c["name"] for c in api_track.get("contributors", []) if c.get("name")
            ]
            if contributors:
                artist_name = ", ".join(contributors)
            else:
                artist_name = track.get("artist_name") or metadata.get(
                    "artist", "Unknown Artist"
                )
        else:
            artist_name = track.get("artist_name", "Unknown Artist")

        file_extension = track.get("file_extension")
        song_path = track.get("song_path")

        if not song_path or not Path(song_path).exists():
            print(
                f"Warning: Missing or non-existent 'song_path' for track {i} "
                f"('{title}'), skipping zip inclusion."
            )
            continue

        file_name_inside_zip = clean_filename(
            f"{track_num_str} - {artist_name} - {title}{file_extension}"
        )
        destination_path = f"{internal_dir_name}/{file_name_inside_zip}"
        files_to_zip[song_path] = destination_path
        print(f"[Zip Prep] Mapping {song_path} -> {destination_path}")

    return files_to_zip


# ---------------------------------------------------------------------------
# Zip writing (blocking; always call through asyncio.to_thread)
# ---------------------------------------------------------------------------
def _write_zip_sync(zip_path: Path, entries: list) -> None:
    """Compress `entries` (list of (src, dest) pairs) into a single zip file."""
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zipf:
        for src, dest in entries:
            if Path(src).exists():
                zipf.write(src, dest)
                print(f"  Adding {src} as {dest}")
            else:
                print(f"  Warning: Source file not found, skipping: {src}")


def _write_multipart_zips_sync(
    output_base_path: Path, entries: list, max_size_bytes: int
) -> list:
    """Split `entries` into several <max_size_bytes zips so Telegram accepts each.

    Returns the list of zip files actually created.
    """
    zip_files_created = []
    files_remaining = list(entries)
    current_part = 1
    # Generous upper bound: stops the loop if progress stalls for any reason.
    max_parts = math.ceil(
        sum(Path(s).stat().st_size for s, _ in entries if Path(s).exists())
        / max_size_bytes
    ) * 2 + 2

    while files_remaining:
        if current_part > max_parts:
            print("Error: Exceeded expected number of parts. Aborting multi-part zip.")
            break

        zip_path = Path(f"{output_base_path}_part{current_part}.zip")
        current_zip_size = 0
        files_in_this_part = []
        indices_processed_in_part = set()

        try:
            with ZipFile(zip_path, "w", ZIP_DEFLATED) as zipf:
                for idx, (src, dest) in enumerate(files_remaining):
                    if not Path(src).exists():
                        print(f"  Skipping non-existent file: {src}")
                        indices_processed_in_part.add(idx)
                        continue

                    file_size = Path(src).stat().st_size
                    if file_size > max_size_bytes:
                        print(
                            f"Error: File {src} ({file_size / (1024 * 1024):.2f} MB) "
                            f"exceeds max part size {max_size_bytes / (1024 * 1024):.2f} MB. Skipping."
                        )
                        indices_processed_in_part.add(idx)
                        continue

                    if current_zip_size + file_size <= max_size_bytes:
                        zipf.write(src, dest)
                        current_zip_size += file_size
                        files_in_this_part.append((src, dest))
                        indices_processed_in_part.add(idx)
                    else:
                        # Does not fit in this part; leave it for the next.
                        continue

            if files_in_this_part:
                zip_files_created.append(zip_path)
                print(
                    f"  Created part {current_part}: {zip_path.name} "
                    f"(Size: {current_zip_size / (1024 * 1024):.2f} MB, "
                    f"{len(files_in_this_part)} files)"
                )
            else:
                print(f"  Warning: Part {current_part} was not created (no files added).")
                if zip_path.exists():
                    zip_path.unlink()

            files_remaining = [
                item
                for idx, item in enumerate(files_remaining)
                if idx not in indices_processed_in_part
            ]

            if not files_remaining:
                break

            current_part += 1

        except Exception as e:
            print(f"Error creating zip part {current_part}: {e}")
            if zip_path.exists():
                try:
                    zip_path.unlink()
                except OSError:
                    pass
            # Discard earlier parts too, otherwise they are orphaned in TMP_DIR
            # once the exception propagates past the caller's tracking list.
            for created in zip_files_created:
                if created.exists():
                    try:
                        created.unlink()
                    except OSError:
                        pass
            raise

    if files_remaining:
        print(f"Warning: {len(files_remaining)} files could not be added to any zip part.")
        for src, _ in files_remaining:
            print(
                f"  - Unadded: {src} "
                f"(Size: {Path(src).stat().st_size if Path(src).exists() else 'N/A'})"
            )

    return zip_files_created


def hashed_archive_basename(hash_title: str, copy_zip_name: str) -> str:
    """One hashed name for every delivery mode.

    Spaces become underscores and an 8-char md5 of the release title is
    appended, which keeps archives off search-engine indexes when they are
    served out of a public directory.
    """
    md5_hash = hashlib.md5(hash_title.encode()).hexdigest()[:8]
    return re.sub(r"[^.a-zA-Z0-9()_&',-]", "_", f"{copy_zip_name} ({md5_hash})")


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
async def deliver_zip(
    event: types.Message,
    files_to_zip: dict,
    caption: str,
    hash_title: str,
    copy_zip_name: str,
    cover_data: bytes | None = None,
):
    """Zip the prepared files, hand them to the user, then schedule cleanup.

    In copy mode (COPY_FILES_PATH set) the archive is written to the hosted
    directory and the user gets a download link; otherwise the archive — split
    at Telegram's upload limit — is sent as a document. Compression runs in a
    worker thread so a multi-GB discography never blocks the event loop.
    """
    user_id, username, first_name = get_user_infos(event)
    sent_messages = []
    server_files_to_delete = []
    auto_delete_seconds = platform_config.auto_delete_seconds()

    caption_sent = False
    if cover_data and platform_config.send_album_cover():
        prepared_cover = prepare_cover_bytes(cover_data)
        if prepared_cover:
            try:
                cover_msg = await event.answer_photo(
                    BufferedInputFile(prepared_cover, filename="cover.jpg"),
                    caption=caption,
                    parse_mode="HTML",
                )
                sent_messages.append(cover_msg)
                caption_sent = True
            except Exception as photo_e:
                print(f"Error sending cover photo: {photo_e}")
    if not caption_sent and caption:
        caption_msg = await event.answer(caption, parse_mode="HTML")
        sent_messages.append(caption_msg)
        caption_sent = True

    if not files_to_zip:
        print("No valid files found to add to the zip archive.")
        await event.answer("❌ No files could be added to the zip archive.")
        return sent_messages

    safe_base_name = hashed_archive_basename(hash_title, copy_zip_name)
    copy_files_path = platform_config.copy_files_path()
    file_link_template = platform_config.file_link_template()

    if copy_files_path:
        print("Using Copy Mode for Zip")
        final_zip_path = Path(copy_files_path) / f"{safe_base_name}.zip"

        try:
            final_zip_path.parent.mkdir(parents=True, exist_ok=True)
            print(f"Creating zip file at: {final_zip_path}")
            await asyncio.to_thread(
                _write_zip_sync, final_zip_path, list(files_to_zip.items())
            )

            if file_link_template:
                file_link = file_link_template.format(quote(final_zip_path.name))
                print(
                    f"USER_DEBUG: Sending download link to user_id={user_id} "
                    f"username={username} first_name={first_name}"
                )
                link_msg = await event.answer(
                    f"📦 <b>Archive Size:</b> {format_file_size(final_zip_path.stat().st_size)} (.zip)\n"
                    f"Download link: {html.escape(file_link)}",
                    parse_mode="HTML",
                )
            else:
                print(
                    "USER_DEBUG: FILE_LINK_TEMPLATE is empty; "
                    "reporting hosted filename instead of a URL"
                )
                link_msg = await event.answer(
                    f"📦 <b>Archive Size:</b> {format_file_size(final_zip_path.stat().st_size)} (.zip)\n"
                    f"Archived on server: {html.escape(final_zip_path.name)}",
                    parse_mode="HTML",
                )
            sent_messages.append(link_msg)
            print(f"Sent download link: {final_zip_path.name}")
            server_files_to_delete.append(final_zip_path)

        except Exception as e:
            print(f"Error creating zip in copy mode: {e}")
            await event.answer(f"❌ Error creating zip file: {e}")
            if final_zip_path.exists():
                try:
                    final_zip_path.unlink()
                except OSError:
                    pass

    else:
        print("Using Direct Send Mode for Zip")
        entries = list(files_to_zip.items())
        total_size = sum(
            Path(f).stat().st_size for f, _ in entries if Path(f).exists()
        )
        output_base_path = Path(TMP_DIR) / safe_base_name
        zip_files_created = []

        try:
            if total_size <= TELEGRAM_MAX_UPLOAD_BYTES:
                zip_path = Path(f"{output_base_path}.zip")
                print(
                    f"Creating single zip: {zip_path} "
                    f"(Total size: {total_size / (1024 * 1024):.2f} MB)"
                )
                await asyncio.to_thread(_write_zip_sync, zip_path, entries)
                zip_files_created.append(zip_path)
            else:
                num_parts = math.ceil(total_size / TELEGRAM_MAX_UPLOAD_BYTES)
                print(
                    f"Creating multi-part zip ({num_parts} parts estimate, "
                    f"Total size: {total_size / (1024 * 1024):.2f} MB)"
                )
                zip_files_created = await asyncio.to_thread(
                    _write_multipart_zips_sync,
                    output_base_path,
                    entries,
                    TELEGRAM_MAX_UPLOAD_BYTES,
                )
        except Exception as e:
            print(f"Error creating zip: {e}")
            await event.answer(f"❌ Error creating zip file: {e}")
            zip_files_created = []

        if zip_files_created:
            print(f"Sending {len(zip_files_created)} zip file(s)...")
            num_sent = len(zip_files_created)
            for idx, zip_file in enumerate(zip_files_created):
                try:
                    part_caption = (
                        f"📦 <b>Part {idx + 1}/{num_sent} Size:</b> "
                        if num_sent > 1 else "📦 <b>Archive Size:</b> "
                    ) + format_file_size(zip_file.stat().st_size) + " (.zip)"
                    print(
                        f"USER_DEBUG: Sending zip file {idx + 1}/{num_sent} to "
                        f"user_id={user_id} username={username} first_name={first_name}"
                    )
                    doc_msg = await event.answer_document(
                        FSInputFile(zip_file),
                        caption=f"{caption}\n{part_caption}" if not caption_sent else part_caption,
                        parse_mode="HTML",
                        disable_notification=True,
                    )
                    sent_messages.append(doc_msg)
                    print(f"Sent {zip_file.name}")
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"Error sending zip file {zip_file.name}: {e}")
                    await event.answer(f"❌ Error sending file: {zip_file.name}")
        else:
            print("No zip files were created or finalized to send.")
            await event.answer("❌ Failed to create any zip files.")

        print("Cleaning up temporary zip files...")
        for zip_file in zip_files_created:
            if zip_file.exists():
                try:
                    zip_file.unlink()
                    print(f"  Removed {zip_file.name}")
                except OSError as e:
                    print(f"  Error removing {zip_file.name}: {e}")

    await schedule_messages_deletion(sent_messages, auto_delete_seconds)
    for server_file in server_files_to_delete:
        await schedule_file_deletion(server_file, auto_delete_seconds)

    return sent_messages


def release_zip_names(metadata, is_album: bool) -> tuple[str, str]:
    """(hash_title, copy_zip_name) for a single release."""
    return (
        f"{metadata['artist']} - {metadata['title']} [{metadata['year']}]",
        f"{unidecode(metadata['clean_artist'])} - {unidecode(metadata['clean_title'])}",
    )


async def create_and_send_zip(
    event: types.Message, metadata, dl_tracks_info, is_album: bool
):
    """Send native audio only in unhosted audio mode; otherwise deliver a ZIP."""
    metadata = _metadata_with_track_artist(metadata, dl_tracks_info)
    audio_only = (
        platform_config.output_format() != "zip"
        and not platform_config.copy_files_path()
    )
    if audio_only:
        if not is_album and len(dl_tracks_info) == 1:
            audio = await send_track_audio(event, metadata, dl_tracks_info[0])
            return [audio] if audio else []
        return await send_album_audio(event, metadata, dl_tracks_info)
    user_id, username, first_name = get_user_infos(event)
    print(
        f"USER_DEBUG: Creating and sending zip to user_id={user_id} "
        f"username={username} first_name={first_name} is_album={is_album}"
    )
    files_to_zip = build_release_entries(metadata, dl_tracks_info)
    caption = get_album_caption(metadata) if is_album else get_track_caption(metadata)
    hash_title, copy_zip_name = release_zip_names(metadata, is_album)
    return await deliver_zip(
        event,
        files_to_zip,
        caption=caption,
        hash_title=hash_title,
        copy_zip_name=copy_zip_name,
        cover_data=metadata.get("cover_data"),
    )


async def create_and_send_artist_zip(
    event: types.Message, artist_metadata, downloaded_releases
):
    """Bundle an entire artist discography into zip archive(s) and send it.

    `downloaded_releases` is a list of (release, metadata, dl_tracks_info)
    tuples. Every release keeps the usual per-release layout in archive mode;
    audio mode sends each release header followed by its original tracks.
    """
    audio_only = (
        platform_config.output_format() != "zip"
        and not platform_config.copy_files_path()
    )
    if audio_only:
        total = sum(
            Path(track["song_path"]).stat().st_size
            for _, _, tracks in downloaded_releases for track in tracks
        )
        summary = await event.answer(
            f"🌕 <b>Artist: {html.escape(artist_metadata['name'])}</b>\n"
            f"📦 <b>Size:</b> {format_file_size(total)}",
            parse_mode="HTML",
        )
        messages = [summary] if summary else []
        for _, metadata, tracks in downloaded_releases:
            messages.extend(await send_album_audio(event, metadata, tracks))
        if summary:
            await schedule_messages_deletion([summary], platform_config.auto_delete_seconds())
        return messages
    user_id, username, first_name = get_user_infos(event)
    artist_name = artist_metadata["name"]
    clean_artist_name = artist_metadata["clean_name"]
    print(
        f"USER_DEBUG: Creating and sending artist zip to user_id={user_id} "
        f"username={username} first_name={first_name} "
        f"releases={len(downloaded_releases)}"
    )

    files_to_zip = {}
    for release, metadata, dl_tracks_info in downloaded_releases:
        try:
            files_to_zip.update(
                build_release_entries(
                    metadata, dl_tracks_info, parent_dir=clean_artist_name
                )
            )
        except Exception as e:
            print(f"Warning: could not prepare zip entries for {release['title']}: {e}")

    if not files_to_zip:
        raise ValueError("No tracks could be added to the artist zip archive.")

    total_tracks = sum(len(tracks) for _, _, tracks in downloaded_releases)
    caption = get_artist_caption(artist_metadata, len(downloaded_releases), total_tracks)

    return await deliver_zip(
        event,
        files_to_zip,
        caption=caption,
        hash_title=f"{artist_name} - Discography [{len(downloaded_releases)} releases]",
        copy_zip_name=f"{unidecode(clean_artist_name)} - Discography",
        cover_data=artist_metadata.get("cover_data"),
    )


# ---------------------------------------------------------------------------
# Direct audio sending (FORMAT != zip)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Generic request-failure reporting
# ---------------------------------------------------------------------------
def describe_error(e: Exception) -> str:
    """Short, human-readable error text for a chat reply."""
    text = str(e).strip()
    return text if text else e.__class__.__name__


def log_failure(context: str, e: Exception) -> None:
    print(f"{context}: {e}")
    traceback.print_exc()


__all__ = [
    "TELEGRAM_MAX_UPLOAD_BYTES",
    "_safe_delete",
    "build_release_entries",
    "create_and_send_artist_zip",
    "create_and_send_zip",
    "deliver_zip",
    "describe_error",
    "get_album_caption",
    "get_artist_caption",
    "get_track_caption",
    "get_user_infos",
    "hashed_archive_basename",
    "log_failure",
    "make_audio_thumbnail",
    "release_zip_names",
    "schedule_file_deletion",
    "schedule_message_deletion",
    "schedule_messages_deletion",
    "send_album_audio",
    "send_track_audio",
]
