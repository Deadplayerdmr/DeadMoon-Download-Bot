import asyncio
import functools
import html
import re
import ssl
import time
import traceback
from pathlib import Path
from urllib.parse import quote

import aiohttp
import aioshutil
import certifi  # SSL certificates
import requests
from aiogram import F, Router, types
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

from bot import bot
from dl_utils.deezer_download import (
    TYPE_ALBUM,
    TYPE_TRACK,
    Deezer403Exception,
    DeezerApiException,
    deezer_search,
    download_song,
    get_artists,
    get_file_format,
    get_song_infos_from_deezer_website,
    init_deezer_session,
)
from dl_utils.deezer_utils import clean_filename
from utils import (
    TMP_DIR,
    __,
    announce_if_queued,
    get_bool_env,
    release_download,
    request_download,
)

from dl_utils import platform_config as pcfg

# Packaging, delivery and auto-delete now live in dl_utils.archive_delivery so
# every platform produces byte-identical archives. They are re-exported here
# under their original names, so existing call sites keep working unchanged.
from dl_utils.archive_delivery import (  # noqa: F401
    TELEGRAM_MAX_UPLOAD_BYTES,
    _ProgressEditor,
    append_issue_summary,
    summarize_track_issues,
    _safe_delete,
    build_release_entries,
    create_and_send_artist_zip,
    create_and_send_zip,
    deliver_zip,
    get_album_caption,
    get_artist_caption,
    get_track_caption,
    get_user_infos,
    hashed_archive_basename,
    make_audio_thumbnail,
    release_zip_names,
    schedule_file_deletion,
    schedule_message_deletion,
    schedule_messages_deletion,
    send_album_audio,
    send_track_audio,
)

deezer_router = Router()


class TelegramNetworkError(Exception):
    pass


DEFAULT_QUALITY = "flac" if pcfg.flac_enabled() else "mp3"
DEEZER_PROXY = pcfg.get_env_or_token_file("DEEZER_PROXY", "").strip()
print("Default quality: " + DEFAULT_QUALITY)
if DEEZER_PROXY:
    print(f"Using Deezer proxy: {DEEZER_PROXY}")
init_deezer_session(DEEZER_PROXY, DEFAULT_QUALITY)

DEEZER_SESSION_REINIT_THRESHOLD = max(
    pcfg.get_int_env("DEEZER_SESSION_REINIT_THRESHOLD", 3), 0
)
print(
    f"Session auto-refresh threshold: {DEEZER_SESSION_REINIT_THRESHOLD or 'disabled'}"
)

MAX_RETRIES = pcfg.max_retries(5)
print("Max retries: " + str(MAX_RETRIES))

SEND_ALBUM_COVER = get_bool_env("SEND_ALBUM_COVER", True)
print("Send album cover: " + str(SEND_ALBUM_COVER))

# Auto-delete configuration (custom feature). Captured at import for the startup
# banner; the delivery helpers re-read the live value on every request.
AUTO_DELETE_ENABLED = pcfg.auto_delete_enabled()
AUTO_DELETE_SECONDS = pcfg.auto_delete_seconds()

print(f"Auto-delete enabled: {AUTO_DELETE_ENABLED}")
print(f"Auto-delete seconds: {AUTO_DELETE_SECONDS}")

# Constants
DEEZER_URL = "https://deezer.com"
API_URL = "https://api.deezer.com"
API_TRACK = API_URL + "/track/%s"
API_ALBUM = API_URL + "/album/%s"
API_ARTIST = API_URL + "/artist/%s"
API_ARTIST_ALBUMS = API_URL + "/artist/%s/albums"

TRACK_REGEX = r"https?://(?:www\.)?deezer\.com/([a-z]*/)?track/(\d+)/?$"
ALBUM_REGEX = r"https?://(?:www\.)?deezer\.com/([a-z]*/)?album/(\d+)/?$"
ARTIST_REGEX = r"https?://(?:www\.)?deezer\.com/([a-z]*/)?artist/(\d+)/?$"
PLAYLIST_REGEX = r"https?://(?:www\.)?deezer\.com/([a-z]*/)?playlist/(\d+)/?$"  # Note: Playlist handling not fully implemented

COPY_FILES_PATH = pcfg.copy_files_path()
FILE_LINK_TEMPLATE = pcfg.file_link_template()


if COPY_FILES_PATH and not FILE_LINK_TEMPLATE:
    print(
        "NOTE: COPY_FILES_PATH is set but FILE_LINK_TEMPLATE is empty. "
        "Archives will still be written to the hosting directory, but users "
        "get the filename instead of a download URL."
    )

# Artist discography limits (anti-spam)
MAX_ARTIST_RELEASES = pcfg.max_artist_releases()
print("Max artist releases: " + str(MAX_ARTIST_RELEASES))

ARTIST_API_DELAY = pcfg.artist_api_delay()
print("Artist API delay: " + str(ARTIST_API_DELAY))

TOO_MANY_RELEASES_MESSAGE = (
    "Error: This artist page has too many releases. To prevent server overload, "
    "it cannot be processed."
)

_session_refresh_lock = None  # Lazily initialized asyncio.Lock
_bot_username = None


async def _get_bot_username() -> str:
    global _bot_username
    if _bot_username is None:
        me = await bot.get_me()
        _bot_username = me.username
    return _bot_username


def _get_session_refresh_lock():
    global _session_refresh_lock
    if _session_refresh_lock is None:
        _session_refresh_lock = asyncio.Lock()
    return _session_refresh_lock


async def refresh_deezer_session(reason: str):
    """Reinitialize Deezer session in a thread-safe way."""
    lock = _get_session_refresh_lock()
    if lock.locked():
        print(f"Waiting for ongoing Deezer session refresh ({reason})")
    async with lock:
        print(f"Reinitializing Deezer session ({reason})...")
        await asyncio.to_thread(init_deezer_session, DEEZER_PROXY, DEFAULT_QUALITY)
        print("Deezer session refreshed.")


async def maybe_refresh_deezer_session(
    attempt_count: int, retries: int, context: str, error: Exception
):
    """
    Refresh the Deezer session automatically if a session-related error keeps happening.

    attempt_count is 1-based and represents how many consecutive attempts have failed.
    """
    if (
        DEEZER_SESSION_REINIT_THRESHOLD <= 0
        or attempt_count >= retries
        or attempt_count % DEEZER_SESSION_REINIT_THRESHOLD != 0
        or not isinstance(error, (DeezerApiException, Deezer403Exception))
    ):
        return

    await refresh_deezer_session(
        f"{context}: failure #{attempt_count} ({type(error).__name__})"
    )




async def download_track(track_id, retries=MAX_RETRIES, progress_cb=None, track_number=None):
    """Downloads a single track from Deezer using imported functions."""
    tmp_track_base_dir = (
        None  # Define outside the try/except to avoid "possibly unbound" errors
    )

    for attempt in range(retries):
        try:
            # Fetch track metadata from Deezer website (may include download details)
            track_infos = await asyncio.to_thread(
                get_song_infos_from_deezer_website, "track", track_id
            )
            if not track_infos:
                print(f"Attempt {attempt + 1}: Could not get track info for {track_id}")
                if attempt < retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
                else:
                    raise ValueError(
                        f"Failed to get track info for {track_id} after {retries} attempts."
                    )

            # Make sure track_infos is a dictionary, not a list
            if isinstance(track_infos, list):
                if len(track_infos) > 0:
                    track_infos = track_infos[0]  # Take the first item if it's a list
                else:
                    raise ValueError(
                        f"Empty track info list received for track {track_id}"
                    )

            file_extension, deezer_format = get_file_format(track_infos)

            # Create a temporary directory for this track
            tmp_track_base_dir = Path(TMP_DIR) / "deezer" / "track" / str(track_id)
            tmp_track_base_dir.mkdir(parents=True, exist_ok=True)

            # Determine the expected final file path within our base dir
            song_path = tmp_track_base_dir / f"{track_id}{file_extension}"

            # Perform the actual download
            await asyncio.to_thread(
                download_song, track_infos, deezer_format, str(song_path)
            )  # download_song expects string path

            # Check if download was successful (e.g., file exists and has size)
            if not song_path.exists() or song_path.stat().st_size == 0:
                # Clean up potentially empty file before retrying
                if song_path.exists():
                    try:
                        song_path.unlink()
                    except OSError:
                        pass
                raise IOError(f"Downloaded file {song_path} is missing or empty.")

            if progress_cb and pcfg.flac_enabled() and file_extension.lower() != ".flac":
                prefix = f"Track {track_number:02d} - " if track_number else ""
                await progress_cb(
                    f"⚠️ Fallback: {prefix}{track_infos.get('ART_NAME', 'Unknown Artist')} - "
                    f"{track_infos.get('SNG_TITLE', f'Track {track_id}')} was not available as "
                    "lossless quality on Deezer (using MP3)"
                )

            # Add download-specific details to the track_infos dictionary
            # Create a deep copy to avoid modifying the original if it's not a dict
            track_info_dict = {}
            if isinstance(track_infos, dict):
                track_info_dict = track_infos.copy()

            track_info_dict["song_path"] = str(song_path)  # Store as string
            track_info_dict["song_name"] = (
                track_infos.get("SNG_TITLE", f"Track {track_id}")
                if isinstance(track_infos, dict)
                else f"Track {track_id}"
            )
            track_info_dict["artist_name"] = (
                track_infos.get("ART_NAME", "Unknown Artist")
                if isinstance(track_infos, dict)
                else "Unknown Artist"
            )
            track_info_dict["file_extension"] = file_extension
            track_info_dict["format_fallback"] = bool(
                pcfg.flac_enabled() and file_extension.lower() != ".flac"
            )
            track_info_dict["download_dir"] = str(
                tmp_track_base_dir
            )  # Store base dir path
            # Carry over track number if present in original info
            if isinstance(track_infos, dict) and "TRACK_NUMBER" in track_infos:
                track_info_dict["TRACK_NUMBER"] = track_infos["TRACK_NUMBER"]

            print(f"Successfully downloaded track {track_id} to {song_path}")
            return track_info_dict  # Success, return details

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(
                f"Error downloading track {track_id} on attempt {attempt + 1}/{retries}: {e}"
            )
            # No specific directory per attempt to clean here, as we use a consistent base dir.
            # The failed/empty file is handled above before raising IOError.

            if attempt < retries - 1:
                await maybe_refresh_deezer_session(
                    attempt + 1, retries, f"track {track_id}", e
                )
                sleep_time = 1 * (attempt + 1)
                print(f"Retrying in {sleep_time} seconds...")
                await asyncio.sleep(sleep_time)
            else:
                print(f"Failed to download track {track_id} after {retries} attempts.")
                # Clean up the base directory if the track ultimately failed
                if tmp_track_base_dir and tmp_track_base_dir.exists():
                    await aioshutil.rmtree(tmp_track_base_dir, ignore_errors=True)
                raise  # Re-raise the last exception

    # This part should ideally not be reached if retries are exhausted (exception raised)
    # Clean up the base directory if we somehow exit the loop without success
    if tmp_track_base_dir and tmp_track_base_dir.exists():
        await aioshutil.rmtree(tmp_track_base_dir, ignore_errors=True)
    return None


async def download_album(album_id, retries=MAX_RETRIES, download_base_dir=None, progress_cb=None):
    """Downloads all tracks from a Deezer album using imported functions, with per-track retries.

    download_base_dir overrides the usual 'deezer/album/<id>' location, which is
    how artist downloads nest every release inside a per-artist folder.
    """
    album_info_attempt = 0
    album_tracks_infos = None
    tmp_download_dir = None  # Define outside the loop for cleanup

    # --- Retry fetching album metadata ---
    while album_info_attempt < retries:
        try:
            album_tracks_infos = await asyncio.to_thread(
                get_song_infos_from_deezer_website, "album", album_id
            )
            if not album_tracks_infos:
                raise ValueError(
                    f"Could not get album info for {album_id} (empty list received)"
                )

            # Create a temporary directory for this album download ONCE after successful metadata fetch
            tmp_download_dir = download_base_dir or (
                Path(TMP_DIR) / "deezer" / "album" / str(album_id)
            )
            tmp_download_dir.mkdir(parents=True, exist_ok=True)
            print(
                f"Album metadata fetched successfully. Download dir: {tmp_download_dir}"
            )
            break  # Exit metadata retry loop on success

        except asyncio.CancelledError:
            raise
        except Exception as e:
            album_info_attempt += 1
            print(
                f"Attempt {album_info_attempt}/{retries}: Error fetching album info for {album_id}: {e}"
            )
            if album_info_attempt < retries:
                await maybe_refresh_deezer_session(
                    album_info_attempt, retries, f"album metadata {album_id}", e
                )
                sleep_time = 1 * album_info_attempt
                print(f"Retrying album info fetch in {sleep_time} seconds...")
                await asyncio.sleep(sleep_time)
            else:
                print(
                    f"Failed to get album info for {album_id} after {retries} attempts."
                )
                # Clean up dir if it was created in a previous failed attempt (unlikely here, but safe)
                if tmp_download_dir and tmp_download_dir.exists():
                    await aioshutil.rmtree(tmp_download_dir, ignore_errors=True)
                raise  # Re-raise the last exception related to fetching album info

    if not album_tracks_infos or not tmp_download_dir:
        # This should not happen if the loop above works correctly, but as a safeguard:
        print(f"Failed to initialize album download for {album_id}.")
        return None  # Indicate failure

    # --- Download individual tracks with retries ---
    downloaded_tracks_details = []
    tasks = []

    # Prepare download tasks for each track
    for i, track_infos in enumerate(album_tracks_infos):
        track_sng_id = track_infos.get("SNG_ID", f"album_{album_id}_track_{i}")
        file_extension, deezer_format = get_file_format(track_infos)
        # Define the final path within the album's download directory
        song_path = tmp_download_dir / f"{track_sng_id}{file_extension}"

        # Create a closure to capture loop variables correctly for async task
        # This inner function now includes the retry logic for a single track
        async def download_single_with_retry(ti, fe, df, sp, track_retries=MAX_RETRIES):
            track_id = ti.get("SNG_ID", "N/A")
            try:
                index = next(index for index, item in enumerate(album_tracks_infos, 1) if item is ti)
            except StopIteration:
                index = 0
            artist = get_artists(ti) or ti.get("ART_NAME", "Unknown Artist")
            title = ti.get("SNG_TITLE", f"Track {track_id}")
            if progress_cb:
                await progress_cb(
                    f"⬇️ Downloading track {index}/{len(album_tracks_infos)}: {artist} - {title}..."
                )
            if pcfg.flac_enabled() and fe.lower() != ".flac":
                ti["format_fallback"] = True
                if progress_cb:
                    await progress_cb(
                        f"⚠️ Fallback: Track {index:02d} - {artist} - {title} was not available "
                        "as lossless quality on Deezer (using MP3)"
                    )
            for attempt in range(track_retries):
                try:
                    # download_song does blocking network I/O plus Blowfish
                    # decryption and mutagen tagging, so keep it off the loop.
                    await asyncio.to_thread(download_song, ti, df, str(sp))

                    if not sp.exists() or sp.stat().st_size == 0:
                        # Clean up potentially empty file before retrying
                        if sp.exists():
                            try:
                                sp.unlink()
                            except OSError:
                                pass
                        raise IOError(f"Downloaded file {sp} is missing or empty.")

                    # Add details after successful download
                    ti_copy = ti.copy()  # Work on a copy
                    ti_copy["song_path"] = str(sp)
                    ti_copy["song_name"] = title
                    ti_copy["artist_name"] = artist
                    ti_copy["file_extension"] = fe
                    ti_copy["format_fallback"] = bool(pcfg.flac_enabled() and fe.lower() != ".flac")
                    # Keep track number if available
                    if "TRACK_NUMBER" in ti:
                        ti_copy["TRACK_NUMBER"] = ti["TRACK_NUMBER"]

                    print(
                        f"Successfully downloaded track {track_id} to {sp} (attempt {attempt + 1})"
                    )
                    return ti_copy  # Success for this track

                except asyncio.CancelledError:
                    raise
                except Exception as track_e:
                    print(
                        f"Error downloading track {track_id} (attempt {attempt + 1}/{track_retries}): {track_e}"
                    )
                    # Clean up potentially failed/partial file for this attempt
                    if sp.exists():
                        try:
                            sp.unlink()
                        except OSError:
                            pass

                    if attempt < track_retries - 1:
                        await maybe_refresh_deezer_session(
                            attempt + 1,
                            track_retries,
                            f"album track {track_id}",
                            track_e,
                        )
                        sleep_time = 1 * (
                            attempt + 1
                        )  # Exponential backoff might be better
                        print(f"Retrying track {track_id} in {sleep_time} seconds...")
                        await asyncio.sleep(sleep_time)
                    else:
                        print(
                            f"Failed to download track {track_id} after {track_retries} attempts."
                        )
                        return None  # Indicate failure for this specific track after all retries

            return None  # Should not be reached, but indicates failure

        tasks.append(
            download_single_with_retry(
                track_infos, file_extension, deezer_format, song_path
            )
        )  # Pass original dict and path

    # Run downloads concurrently
    results = await asyncio.gather(*tasks)

    # Filter out failed downloads (None results)
    downloaded_tracks_details = []
    for index, (info, result) in enumerate(zip(album_tracks_infos, results), 1):
        if result is not None:
            downloaded_tracks_details.append(result)
            continue
        artist = get_artists(info) or info.get("ART_NAME", "Unknown Artist")
        title = info.get("SNG_TITLE", f"Track {info.get('SNG_ID', index)}")
        if progress_cb:
            await progress_cb(
                f"❌ Failure: Track {index:02d} - {artist} - {title} was not available"
            )

    # Check if *any* tracks were successfully downloaded
    if not downloaded_tracks_details:
        print(f"Failed to download any tracks for album {album_id}.")
        # Clean up the main album directory as the entire process failed
        if tmp_download_dir and tmp_download_dir.exists():
            await aioshutil.rmtree(tmp_download_dir, ignore_errors=True)
        # Raising an exception here will be caught by the handler's main try/except
        raise Exception(
            f"Failed to download any tracks for album {album_id} after retries."
        )

    # Add the download directory to all *successful* track dicts
    # This is needed for zipping later
    for track_detail in downloaded_tracks_details:
        track_detail["download_dir"] = str(tmp_download_dir)

    print(
        f"Successfully downloaded {len(downloaded_tracks_details)} out of {len(album_tracks_infos)} tracks for album {album_id} to {tmp_download_dir}"
    )
    # Return list of successfully downloaded track details
    # The calling handler will manage cleanup of the tmp_download_dir
    return downloaded_tracks_details


def get_track_metadata_from_api(track_id):
    """Gets track metadata from the official Deezer API."""
    try:
        response = requests.get(API_TRACK % quote(str(track_id)))
        response.raise_for_status()  # Raise an exception for bad status codes
        track_json = response.json()

        if "error" in track_json:
            raise ValueError(f"API Error for track {track_id}: {track_json['error']}")

        # Extract cover URL, preferring larger sizes
        cover_url = (
            track_json.get("album", {}).get("cover_xl")
            or track_json.get("album", {}).get("cover_big")
            or track_json.get("album", {}).get("cover_medium")
            or f"https://e-cdns-images.dzcdn.net/images/cover/{track_json.get('album', {}).get('md5_image', '')}/1200x0-000000-100-0-0.jpg"
        )

        # Fetch cover image data
        cover_response = requests.get(cover_url, stream=True)
        cover_response.raise_for_status()
        cover_data = cover_response.content  # Read content directly

        # Extract other metadata
        artists = [
            c["name"] for c in track_json.get("contributors", []) if c.get("name")
        ]
        release_date_str = track_json.get("release_date", "0000-00-00")
        try:
            year = release_date_str.split("-")[0]
            # Format date as DD/MM/YYYY
            release_date_formatted = "/".join(reversed(release_date_str.split("-")))
        except:
            year = "0000"
            release_date_formatted = "00/00/0000"

        title = track_json.get("title", f"Track {track_id}")
        artist_name = track_json.get("artist", {}).get("name", "Unknown Artist")
        album_title = track_json.get("album", {}).get("title", "Unknown Album")
        album_link = track_json.get("album", {}).get("link", "")
        track_link = track_json.get("link", "")

        # Clean names for file system use
        clean_title = clean_filename(title)
        clean_artist = clean_filename(artist_name)
        clean_album_title = clean_filename(album_title)

        # Prepare metadata dictionary
        metadata = {
            "id": track_id,
            "title": title,
            "artist": artist_name,
            "album_title": album_title,
            "artists_list": artists,
            "release_date": release_date_formatted,
            "year": year,
            "album_link": album_link,
            "track_link": track_link,
            "cover_data": cover_data,
            "api_json": track_json,  # Keep original json if needed
            "tracks_api_data": [
                {
                    "id": track_id,
                    "SNG_ID": track_id,
                    "title": title,
                    "SNG_TITLE": title,
                    "artist": {"name": artist_name},
                    "ART_NAME": artist_name,
                }
            ],
            # For zip naming/structure
            "clean_artist": clean_artist,
            "clean_album_title": clean_album_title,
            "clean_title": clean_title,
        }
        print(f"Fetched metadata for track {track_id}: {artist_name} - {title}")
        return metadata

    except requests.exceptions.RequestException as e:
        print(f"Network error fetching metadata for track {track_id}: {e}")
        raise
    except Exception as e:
        print(f"Error processing metadata for track {track_id}: {e}")
        raise


def get_album_metadata_from_api(album_id):
    """Gets album and its tracks' metadata from the official Deezer API."""
    try:
        # Fetch main album info
        album_response = requests.get(API_ALBUM % quote(str(album_id)))
        album_response.raise_for_status()
        album_json = album_response.json()
        if "error" in album_json:
            raise ValueError(f"API Error for album {album_id}: {album_json['error']}")

        # Fetch track list (handle pagination if necessary, though 1000 limit is high)
        tracks_response = requests.get(
            API_ALBUM % quote(str(album_id)) + "/tracks?limit=1000"
        )
        tracks_response.raise_for_status()
        tracks_json = tracks_response.json()
        if "error" in tracks_json:
            # Might happen if album is empty or restricted
            print(
                f"API Error fetching tracks for album {album_id}: {tracks_json['error']}"
            )
            tracks_data = []
        else:
            tracks_data = tracks_json.get("data", [])

        # Extract cover URL
        cover_url = (
            album_json.get("cover_xl")
            or album_json.get("cover_big")
            or album_json.get("cover_medium")
            or f"https://e-cdns-images.dzcdn.net/images/cover/{album_json.get('md5_image', '')}/1200x0-000000-100-0-0.jpg"
        )

        # Fetch cover image data
        cover_response = requests.get(cover_url, stream=True)
        cover_response.raise_for_status()
        cover_data = cover_response.content

        # Extract album metadata
        release_date_str = album_json.get("release_date", "0000-00-00")
        try:
            year = release_date_str.split("-")[0]
            release_date_formatted = "/".join(reversed(release_date_str.split("-")))
        except:
            year = "0000"
            release_date_formatted = "00/00/0000"

        album_title = album_json.get("title", f"Album {album_id}")
        artist_name = album_json.get("artist", {}).get("name", "Unknown Artist")
        album_link = album_json.get("link", "")

        # Clean names for file system use
        clean_artist = clean_filename(artist_name)
        clean_album_title = clean_filename(album_title)

        # Prepare metadata dictionary
        metadata = {
            "id": album_id,
            "title": album_title,  # Album title
            "artist": artist_name,  # Main album artist
            "release_date": release_date_formatted,
            "year": year,
            "album_link": album_link,
            "track_link": None,  # No single track link for album
            "cover_data": cover_data,
            "api_json": album_json,
            "tracks_api_data": tracks_data,  # List of track dicts from API
            # For zip naming/structure
            "clean_artist": clean_artist,
            "clean_album_title": clean_album_title,
            "clean_title": clean_album_title,  # Use album title for clean_title contextually
        }
        print(f"Fetched metadata for album {album_id}: {artist_name} - {album_title}")
        return metadata

    except requests.exceptions.RequestException as e:
        print(f"Network error fetching metadata for album {album_id}: {e}")
        raise
    except Exception as e:
        print(f"Error processing metadata for album {album_id}: {e}")
        raise


# Only these Deezer record types make up an artist's own discography. The
# website also lists "Featured In" and "Appears On" releases, which are albums
# owned by other artists; /artist/{id}/albums does not return those, and this
# allowlist keeps them out even if that endpoint ever starts including extras.
ARTIST_RELEASE_TYPES = frozenset({"album", "ep", "single", "compile"})

# Deezer answers with HTTP 429 when hit too often, which surfaces as a
# RequestException. Retrying after a pause is the only sane response.
API_RATE_LIMIT_STATUS = 429


def _get_deezer_json(url: str, params: dict | None = None, retries: int = 4) -> dict:
    """Fetch JSON from the Deezer API, backing off on rate limiting.

    Runs with the blocking `requests` client, so callers must invoke it through
    asyncio.to_thread. Raises the last exception if every attempt fails.
    """
    last_error = None
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, timeout=30)
            if response.status_code == API_RATE_LIMIT_STATUS:
                # Honour Retry-After when Deezer sends it, else back off.
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = max(float(retry_after), 1.0) if retry_after else 2.0 * (attempt + 1)
                except (TypeError, ValueError):
                    wait = 2.0 * (attempt + 1)
                print(f"Rate limited by Deezer API ({url}), waiting {wait}s")
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"API request failed ({url}), attempt {attempt + 1}/{retries}: {e}")
            time.sleep(1.0 * (attempt + 1))

    raise last_error or RuntimeError(f"Failed to fetch {url}")


def get_artist_metadata_from_api(artist_id) -> dict:
    """Gets basic artist info (name, picture) from the official Deezer API."""
    artist_json = _get_deezer_json(API_ARTIST % quote(str(artist_id)))
    if "error" in artist_json:
        raise ValueError(
            f"API Error for artist {artist_id}: {artist_json['error']}"
        )

    artist_name = artist_json.get("name", f"Artist {artist_id}")
    picture_url = (
        artist_json.get("picture_xl")
        or artist_json.get("picture_big")
        or artist_json.get("picture_medium")
    )

    cover_data = None
    if picture_url:
        try:
            picture_response = requests.get(picture_url, timeout=30)
            picture_response.raise_for_status()
            cover_data = picture_response.content
        except requests.exceptions.RequestException as e:
            print(f"Warning: could not download picture for artist {artist_id}: {e}")

    print(f"Fetched metadata for artist {artist_id}: {artist_name}")
    return {
        "id": artist_id,
        "name": artist_name,
        "clean_name": clean_filename(artist_name),
        "link": artist_json.get("link", f"{DEEZER_URL}/artist/{artist_id}"),
        "cover_data": cover_data,
        "api_json": artist_json,
    }


def fetch_artist_releases_sync(artist_id) -> list:
    """List every release belonging to an artist, oldest-last.

    Walks the paginated /artist/{id}/albums endpoint, keeps only the release
    types that make up the artist's own discography, and sleeps between pages
    so Deezer does not start answering with HTTP 429.
    """
    releases = []
    seen_ids = set()
    url = API_ARTIST_ALBUMS % quote(str(artist_id))
    params = {"limit": 500}

    while url:
        page = _get_deezer_json(url, params=params)
        if "error" in page and "type" not in page:
            raise ValueError(f"API Error listing albums for artist {artist_id}: {page['error']}")

        for item in page.get("data", []):
            release_id = item.get("id")
            record_type = item.get("record_type")
            if not release_id or release_id in seen_ids:
                continue
            seen_ids.add(release_id)
            if record_type not in ARTIST_RELEASE_TYPES:
                print(
                    f"Skipping release {release_id} ('{item.get('title')}') "
                    f"with unsupported record_type {record_type!r}"
                )
                continue
            releases.append(
                {
                    "id": release_id,
                    "title": item.get("title", f"Release {release_id}"),
                    "record_type": record_type,
                    "release_date": item.get("release_date", ""),
                    "year": (item.get("release_date") or "0000-00-00").split("-")[0],
                }
            )

        url = page.get("next") or None
        params = None  # `next` already carries the pagination query string
        if url:
            # Throttle paginated walks to stay under Deezer's rate limit.
            time.sleep(ARTIST_API_DELAY)

    # Newest first, matching the order Deezer shows on the artist page.
    releases.sort(key=lambda r: r["release_date"], reverse=True)
    print(
        f"Artist {artist_id}: {len(releases)} valid release(s) found "
        f"({', '.join(sorted({r['record_type'] for r in releases})) or 'none'})"
    )
    return releases


async def fetch_artist_releases(artist_id) -> list:
    """Async wrapper around fetch_artist_releases_sync (blocking requests calls)."""
    return await asyncio.to_thread(fetch_artist_releases_sync, artist_id)















async def download_artist_releases(artist_id, releases, artist_base_dir: Path, progress_cb=None, artist_name="Deezer Artist"):
    """Download every release of an artist into one shared parent folder.

    Releases are processed one at a time: they are independent albums, and
    hammering Deezer in parallel is what triggers HTTP 429 in the first place.
    Returns the list of (release, metadata, tracks) tuples that succeeded, so a
    single unreadable release does not sink the whole discography.
    """
    downloaded = []

    for index, release in enumerate(releases, start=1):
        release_id = release["id"]
        release_title = release["title"]
        print(
            f"Artist {artist_id}: downloading release {index}/{len(releases)} "
            f"- {release_title} ({release['record_type']}, {release_id})"
        )
        prefix = (
            f"⬇️ {artist_name} Discography (Release {index}/{len(releases)} — "
            f"{release['record_type']}): {release_title} [{release.get('year', '')}]"
        )
        if progress_cb:
            await progress_cb(prefix + "...")

        # Keep each release in its own folder under the artist directory.
        release_dir = artist_base_dir / str(release_id)

        try:
            # Pause between metadata requests so Deezer does not rate limit us.
            if index > 1 and ARTIST_API_DELAY:
                await asyncio.sleep(ARTIST_API_DELAY)

            metadata = await asyncio.to_thread(get_album_metadata_from_api, release_id)
            async def release_progress(text, prefix=prefix):
                if text.startswith("⬇️ Downloading track "):
                    text = text[len("⬇️ Downloading "):]
                    text = " (" + text[:-3] + ")..." if text.endswith("...") else text
                await progress_cb(prefix + " " + text)

            dl_tracks_info = await download_album(
                release_id,
                download_base_dir=release_dir,
                progress_cb=release_progress if progress_cb else None,
            )
            if not dl_tracks_info:
                print(f"Artist {artist_id}: no tracks downloaded for {release_title}")
                continue

            downloaded.append((release, metadata, dl_tracks_info))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(
                f"Artist {artist_id}: failed to download release {release_title} "
                f"({release_id}): {e}"
            )
            traceback.print_exc()
            if release_dir.exists():
                await aioshutil.rmtree(release_dir, ignore_errors=True)

    if not downloaded:
        raise Exception(
            f"Failed to download any releases for artist {artist_id} after retries."
        )

    print(
        f"Artist {artist_id}: downloaded {len(downloaded)}/{len(releases)} releases"
    )
    return downloaded




# --- Message Handlers ---


@deezer_router.message(F.text.regexp(TRACK_REGEX))
async def handle_track_link(event: types.Message, real_link=None):
    """Handles Deezer track links using imported utils and functions."""
    if not pcfg.platform_enabled(pcfg.DEEZER):
        await event.answer(pcfg.platform_disabled_message(pcfg.DEEZER))
        return

    user_id, username, first_name = get_user_infos(event)
    print(
        f"USER_DEBUG: Track link request from user_id={user_id} username={username} first_name={first_name}"
    )
    print(f"User {user_id}: Received track link: {event.text}")
    link_to_process = real_link or event.text and event.text.strip() or None
    if not link_to_process:
        await event.answer("Invalid track link format.")
        return
    track_match = re.search(TRACK_REGEX, link_to_process)
    if not track_match:
        await event.answer("Invalid track link format.")
        return
    track_id = track_match.group(2)

    slot = request_download(user_id, getattr(getattr(event, "chat", None), "id", None))
    slot.register_workdir(Path(TMP_DIR) / "deezer" / "track" / str(track_id))
    if not slot.granted:
        print(
            f"USER_DEBUG: Rejected download request from user_id={user_id} username={username} first_name={first_name} - status={slot.status}"
        )
        await event.answer(
            __("running_download"), reply_markup=types.ReplyKeyboardRemove()
        )
        return

    tmp_msg = None
    download_dir_to_clean = None  # Store the path to clean up

    try:
        await announce_if_queued(event, slot)
        tmp_msg = await event.answer(__("downloading"))

        # Download the track
        progress = _ProgressEditor(tmp_msg)
        dl_track_info = await download_track(track_id, progress_cb=progress)
        if not dl_track_info or "song_path" not in dl_track_info:
            raise ValueError("Track download failed or did not return path.")

        # Store the directory path for cleanup *after* successful download
        if "download_dir" in dl_track_info:
            download_dir_to_clean = Path(dl_track_info["download_dir"])

        # Fetch metadata (can happen after download)
        metadata = await asyncio.to_thread(get_track_metadata_from_api, track_id)

        if dl_track_info.get("format_fallback"):
            metadata = metadata.copy()
            metadata["caption_suffix"] = "\n\n" + html.escape(
                summarize_track_issues([dl_track_info])[0][0]
            )

        # Send based on format preference
        if pcfg.output_format() == "zip":
            await create_and_send_zip(event, metadata, [dl_track_info], is_album=False)
        else:
            await send_track_audio(event, metadata, dl_track_info)

        await _safe_delete(tmp_msg)
        # Delete the original user message after successful processing
        try:
            await event.delete()
            print(
                f"USER_DEBUG: Deleted original message from user_id={user_id} username={username} first_name={first_name}"
            )
            print(f"Deleted original message from user {user_id}")
        except Exception as delete_e:
            print(f"Could not delete original message: {delete_e}")

    except asyncio.CancelledError:
        if not slot.cancelled:
            raise
        await _safe_delete(tmp_msg)
    except Exception as e:
        print(
            f"USER_DEBUG: Error processing track download for user_id={user_id} username={username} first_name={first_name}: {e}"
        )
        print(f"Error processing track {track_id}: {e}")
        print(traceback.format_exc())
        await _safe_delete(tmp_msg)
        error_message = str(e) if str(e) else "An unknown error occurred."
        await event.answer(f"{__('download_error')} {error_message}")
    finally:
        release_download(slot)
        # Cleanup the download directory if it was set
        if download_dir_to_clean and download_dir_to_clean.exists():
            try:
                print(f"Cleaning up download directory: {download_dir_to_clean}")
                await aioshutil.rmtree(download_dir_to_clean)
            except Exception as cleanup_e:
                print(
                    f"Error cleaning up directory {download_dir_to_clean}: {cleanup_e}"
                )


@deezer_router.message(F.text.regexp(ALBUM_REGEX))
async def handle_album_link(event: types.Message, real_link=None):
    """Handles Deezer album links using imported utils and functions."""
    if not pcfg.platform_enabled(pcfg.DEEZER):
        await event.answer(pcfg.platform_disabled_message(pcfg.DEEZER))
        return

    user_id, username, first_name = get_user_infos(event)
    print(
        f"USER_DEBUG: Album link request from user_id={user_id} username={username} first_name={first_name}"
    )
    print(f"User {user_id}: Received album link: {event.text}")
    link_to_process = real_link or event.text and event.text.strip()
    if not link_to_process:
        await event.answer("Invalid album link format.")
        return
    album_match = re.search(ALBUM_REGEX, link_to_process)
    if not album_match:
        await event.answer("Invalid album link format.")
        return
    album_id = album_match.group(2)

    slot = request_download(user_id, getattr(getattr(event, "chat", None), "id", None))
    slot.register_workdir(Path(TMP_DIR) / "deezer" / "album" / str(album_id))
    if not slot.granted:
        print(
            f"USER_DEBUG: Rejected album download request from user_id={user_id} username={username} first_name={first_name} - status={slot.status}"
        )
        await event.answer(
            __("running_download"), reply_markup=types.ReplyKeyboardRemove()
        )
        return

    tmp_msg = None
    download_dir_to_clean = None  # Store the path to clean up

    try:
        await announce_if_queued(event, slot)
        tmp_msg = await event.answer(__("downloading"))

        # Download the album tracks (with internal retries per track)
        progress = _ProgressEditor(tmp_msg)
        dl_tracks_info = await download_album(album_id, progress_cb=progress)
        if not dl_tracks_info:  # Check if *any* tracks were successfully downloaded
            raise ValueError("Album download failed or returned no successful tracks.")

        # Determine the download directory from the first successful track for cleanup
        # All successful tracks should share the same base directory
        if dl_tracks_info and "download_dir" in dl_tracks_info[0]:
            download_dir_to_clean = Path(dl_tracks_info[0]["download_dir"])
        else:
            print(
                "Warning: Could not determine download directory for cleanup from track info."
            )
            # Attempt to construct the expected path as a fallback
            download_dir_to_clean = Path(TMP_DIR) / "deezer" / "album" / str(album_id)

        # Fetch album metadata (can happen after download)
        metadata = await asyncio.to_thread(get_album_metadata_from_api, album_id)
        metadata = metadata.copy()
        issue_lines = [
            line
            for lines in summarize_track_issues(dl_tracks_info, metadata.get("tracks_api_data"))
            for line in lines
        ]
        metadata["caption_suffix"] = (
            "\n\n" + "\n".join(html.escape(line) for line in issue_lines)
            if issue_lines
            else ""
        )

        # Send based on format preference
        if pcfg.output_format() == "zip":
            await create_and_send_zip(event, metadata, dl_tracks_info, is_album=True)
        else:
            await send_album_audio(event, metadata, dl_tracks_info)

        await _safe_delete(tmp_msg)
        # Delete the original user message after successful processing
        try:
            await event.delete()
            print(
                f"USER_DEBUG: Deleted original message from user_id={user_id} username={username} first_name={first_name}"
            )
            print(f"Deleted original message from user {user_id}")
        except Exception as delete_e:
            print(f"Could not delete original message: {delete_e}")

    except asyncio.CancelledError:
        if not slot.cancelled:
            raise
        await _safe_delete(tmp_msg)
    except Exception as e:
        print(
            f"USER_DEBUG: Error processing album download for user_id={user_id} username={username} first_name={first_name}: {e}"
        )
        print(f"Error processing album {album_id}: {e}")
        print(traceback.format_exc())
        await _safe_delete(tmp_msg)
        error_message = str(e) if str(e) else "An unknown error occurred."
        await event.answer(f"{__('download_error')} {error_message}")
    finally:
        release_download(slot)
        # Cleanup the download directory if it was set or constructed
        if download_dir_to_clean and download_dir_to_clean.exists():
            try:
                print(f"Cleaning up download directory: {download_dir_to_clean}")
                await aioshutil.rmtree(download_dir_to_clean)
            except Exception as cleanup_e:
                print(
                    f"Error cleaning up directory {download_dir_to_clean}: {cleanup_e}"
                )


@deezer_router.message(F.text.regexp(ARTIST_REGEX))
async def handle_artist_link(event: types.Message, real_link=None):
    """Handles Deezer artist links by downloading their full discography."""
    if not pcfg.platform_enabled(pcfg.DEEZER):
        await event.answer(pcfg.platform_disabled_message(pcfg.DEEZER))
        return

    user_id, username, first_name = get_user_infos(event)
    print(
        f"USER_DEBUG: Artist link request from user_id={user_id} username={username} first_name={first_name}"
    )
    print(f"User {user_id}: Received artist link: {event.text}")
    link_to_process = real_link or (event.text and event.text.strip())
    if not link_to_process:
        await event.answer("Invalid artist link format.")
        return
    artist_match = re.search(ARTIST_REGEX, link_to_process)
    if not artist_match:
        await event.answer("Invalid artist link format.")
        return
    artist_id = artist_match.group(2)

    # Checked before a slot is taken: refusing a disabled feature must not push
    # another user's request out of the queue for nothing.
    if not pcfg.artist_download_enabled(pcfg.DEEZER):
        print(f"User {user_id}: Deezer artist downloads are disabled; refusing {artist_id}")
        await event.answer(pcfg.artist_disabled_message(pcfg.DEEZER))
        return

    slot = request_download(user_id, getattr(getattr(event, "chat", None), "id", None))
    if not slot.granted:
        print(
            f"USER_DEBUG: Rejected artist download request from user_id={user_id} "
            f"username={username} first_name={first_name} - status={slot.status}"
        )
        await event.answer(
            __("running_download"), reply_markup=types.ReplyKeyboardRemove()
        )
        return

    tmp_msg = None
    artist_dir_to_clean = Path(TMP_DIR) / "deezer" / "artist" / str(artist_id)
    slot.register_workdir(artist_dir_to_clean)

    try:
        await announce_if_queued(event, slot)
        tmp_msg = await event.answer(__("downloading"))

        try:
            # Count the releases first: an oversized discography must be refused
            # before any other request is made, let alone any byte downloaded.
            releases = await fetch_artist_releases(artist_id)
            if len(releases) > MAX_ARTIST_RELEASES:
                await _safe_delete(tmp_msg)
                print(
                    f"Artist {artist_id} has {len(releases)} releases, above the "
                    f"limit of {MAX_ARTIST_RELEASES}. Aborting."
                )
                await event.answer(TOO_MANY_RELEASES_MESSAGE)
                return

            if not releases:
                await _safe_delete(tmp_msg)
                await event.answer(
                    f"{__('download_error')} No downloadable releases found for this artist."
                )
                return

            artist_metadata = await asyncio.to_thread(
                get_artist_metadata_from_api, artist_id
            )

            artist_dir_to_clean.mkdir(parents=True, exist_ok=True)
            downloaded_releases = await download_artist_releases(
                artist_id,
                releases,
                artist_dir_to_clean,
                progress_cb=_ProgressEditor(tmp_msg),
                artist_name=artist_metadata.get("name", "Deezer Artist"),
            )
            await create_and_send_artist_zip(
                event, artist_metadata, downloaded_releases
            )

            await _safe_delete(tmp_msg)
            # Delete the original user message after successful processing
            try:
                await event.delete()
                print(
                    f"USER_DEBUG: Deleted original message from user_id={user_id} username={username} first_name={first_name}"
                )
            except Exception as delete_e:
                print(f"Could not delete original message: {delete_e}")

        except Exception as e:
            print(
                f"USER_DEBUG: Error processing artist download for user_id={user_id} "
                f"username={username} first_name={first_name}: {e}"
            )
            print(f"Error processing artist {artist_id}: {e}")
            print(traceback.format_exc())
            await _safe_delete(tmp_msg)
            error_message = str(e) if str(e) else "An unknown error occurred."
            await event.answer(f"{__('download_error')} {error_message}")
        finally:
            if artist_dir_to_clean.exists():
                try:
                    print(f"Cleaning up artist directory: {artist_dir_to_clean}")
                    await aioshutil.rmtree(artist_dir_to_clean, ignore_errors=True)
                except Exception as cleanup_e:
                    print(
                        f"Error cleaning up directory {artist_dir_to_clean}: {cleanup_e}"
                    )
    finally:
        release_download(slot)


@deezer_router.message(
    F.text.regexp(
        r"^https?://(?:www\.)?(?:deezer|dzr)\.page\.link/.*$|^https?://(?:www\.)?link\.deezer\.com/.*$"
    )
)
async def handle_shortlink(event: types.Message):
    """Handles Deezer shortlinks by resolving them, with improved SSL handling."""
    user_id, username, first_name = get_user_infos(event)
    print(
        f"USER_DEBUG: Shortlink request from user_id={user_id} username={username} first_name={first_name}"
    )
    print(f"User {user_id}: Received shortlink: {event.text}")

    # Gate before the resolve: the delegated handlers check this too, but only
    # after a round trip to the wrapper service.
    if not pcfg.platform_enabled(pcfg.DEEZER):
        await event.answer(pcfg.platform_disabled_message(pcfg.DEEZER))
        return

    tmp_msg = await event.answer("🔗 Resolving shortlink...")
    ssl_context = ssl.create_default_context(cafile=certifi.where())  # Use certifi CAs
    connector = aiohttp.TCPConnector(ssl=ssl_context)

    if not event.text:
        await event.answer("Invalid shortlink format.")
        return

    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.head(
                event.text.strip(),
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                real_link = str(response.url).split("?")[0]
                print(f"Resolved shortlink to: {real_link}")

        await tmp_msg.delete()

        if re.match(TRACK_REGEX, real_link):
            await handle_track_link(event, real_link)
        elif re.match(ALBUM_REGEX, real_link):
            await handle_album_link(event, real_link)
        elif re.match(ARTIST_REGEX, real_link):
            await handle_artist_link(event, real_link)
        # Add playlist handling here if needed
        # elif re.match(PLAYLIST_REGEX, real_link):
        #     await handle_playlist_link(event, real_link)
        else:
            print("Unknown link type after resolving: " + real_link)
            await event.answer(
                __("download_error") + " Unsupported link type after resolving."
            )

    except aiohttp.ClientConnectorCertificateError as e:
        await tmp_msg.delete()
        print(f"SSL Certificate Verification Error resolving {event.text}: {e}")
        await event.answer(
            f"{__('resolve_error')} {__('ssl_error')}. Please check system certificates or network configuration."
        )
    except asyncio.TimeoutError:
        await tmp_msg.delete()
        print(f"Timeout resolving shortlink {event.text}")
        await event.answer(f"{__('resolve_error')} Timeout while resolving the link.")
    except aiohttp.ClientError as e:
        await tmp_msg.delete()
        print(f"Error resolving shortlink {event.text}: {e}")
        await event.answer(f"{__('resolve_error')} Could not resolve shortlink: {e}")
    except Exception as e:
        await tmp_msg.delete()
        print(f"Unexpected error handling shortlink {event.text}: {e}")
        print(traceback.format_exc())
        await event.answer(f"{__('resolve_error')} Unexpected error: {e}")


# --- Inline Query ---


@deezer_router.inline_query()
async def inline_search_handler(inline_query: InlineQuery):
    """
    Handles inline queries to search Deezer using the imported deezer_search function.
    Runs the synchronous search function in an executor to avoid blocking.
    """
    user_id = inline_query.from_user.id
    username = inline_query.from_user.username
    first_name = inline_query.from_user.first_name
    query = inline_query.query.strip()
    if not pcfg.platform_enabled(pcfg.DEEZER):
        # Nothing to offer: an empty answer keeps the inline session quiet
        # rather than listing results the user then cannot download.
        await inline_query.answer([], cache_time=60)
        return
    print(
        f"USER_DEBUG: Inline search from user_id={user_id} username={username} first_name={first_name} query='{query}'"
    )

    items = []
    search_type = TYPE_TRACK  # Default search type

    if not query:
        await bot.answer_inline_query(
            inline_query.id,
            results=[],
            cache_time=10,
            switch_pm_text="Type to search Deezer...",
            switch_pm_parameter="inline_help",
        )
        return

    # Determine search type based on prefixes (case-insensitive)
    words = query.split(maxsplit=1)
    first_word = words[0].lower()

    if first_word == "album":
        search_type = TYPE_ALBUM
        query = words[1] if len(words) > 1 else ""
    elif first_word == "track":
        search_type = TYPE_TRACK
        query = words[1] if len(words) > 1 else ""

    if not query:
        await bot.answer_inline_query(inline_query.id, results=[], cache_time=10)
        return

    print(
        f"Inline query: '{inline_query.query}', Parsed: query='{query}', type='{search_type}'"
    )

    try:
        loop = asyncio.get_running_loop()
        search_results = await loop.run_in_executor(
            None, functools.partial(deezer_search, query, search_type)
        )
        bot_username = await _get_bot_username()
        download_button_text = "📥 " + __("download_button")

        for item_data in search_results[:20]:  # Limit results sent
            try:
                result_id = item_data.get("id", None)
                id_type = item_data.get(
                    "id_type", TYPE_TRACK
                )  # Assume track if missing
                thumb_url = item_data.get("img_url", "")  # Use empty string if missing

                if not result_id:
                    print(f"Skipping item due to missing ID: {item_data}")
                    continue

                if id_type == TYPE_ALBUM:
                    title = item_data.get("album", f"Album {result_id}")
                    artist = item_data.get("artist", "Unknown Artist")
                    description = f"Album by {artist}"
                    link = DEEZER_URL + "/album/%s" % quote(str(result_id))
                elif id_type == TYPE_TRACK:
                    title = item_data.get("title", f"Track {result_id}")
                    artist = item_data.get("artist", "Unknown Artist")
                    album_title = item_data.get("album", "")
                    description = f"Track by {artist}"
                    if album_title:
                        description += f" - {album_title}"
                    link = DEEZER_URL + "/track/%s" % quote(str(result_id))
                else:
                    print(f"Skipping item with unhandled id_type: {id_type}")
                    continue

                payload = f"{id_type}_{result_id}"
                deep_link = f"https://t.me/{bot_username}?start={payload}"
                reply_markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=download_button_text, url=deep_link
                            )
                        ]
                    ]
                )

                article = InlineQueryResultArticle(
                    id=payload,
                    title=title,
                    description=description,
                    thumb_url=thumb_url if thumb_url else None,  # Pass None if empty
                    input_message_content=InputTextMessageContent(message_text=link),
                    reply_markup=reply_markup,
                )
                items.append(article)
            except Exception as item_e:
                print(
                    f"Error processing individual search result item {item_data.get('id', 'N/A')}: {item_e}"
                )

    # Handle potential DeezerApiException from deezer_search
    except DeezerApiException as e:
        print(f"Deezer API exception during search: {e}")
        print(traceback.format_exc())
        await bot.answer_inline_query(
            inline_query.id,
            results=[],
            cache_time=10,
            switch_pm_text=__("search_error"),
            switch_pm_parameter="search_error_api",
        )
        return
    except Exception as e:
        print(f"Error running or processing deezer_search: {e}")
        print(traceback.format_exc())
        await bot.answer_inline_query(
            inline_query.id,
            results=[],
            cache_time=10,
            switch_pm_text=__("search_error"),
            switch_pm_parameter="search_error_generic",
        )
        return

    try:
        await bot.answer_inline_query(inline_query.id, results=items, cache_time=10)
    except Exception as e:
        # Catch potential Telegram API errors during sending results
        print(f"Error sending inline query results to Telegram: {e}")
        # Optionally, try sending an empty result set as a fallback
        try:
            await bot.answer_inline_query(inline_query.id, results=[], cache_time=10)
        except Exception as fallback_e:
            print(f"Error sending fallback empty inline query results: {fallback_e}")
