import asyncio
import hashlib
import io
import os
import re
import shutil
import traceback
from pathlib import Path
from urllib.parse import quote
from zipfile import ZipFile, ZIP_DEFLATED

import requests
import yt_dlp
from PIL import Image
from aiogram import F, types
from aiogram.types import BufferedInputFile, FSInputFile
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
from yt_dlp import YoutubeDL
from aiogram import Router
from unidecode import unidecode

# Assuming utils provides these functions and constants
from utils import (
    TMP_DIR,
    __,
    announce_if_queued,
    release_download,
    request_download,
)
from dl_utils import platform_config as pcfg
from dl_utils.archive_delivery import _ProgressEditor, create_and_send_zip, schedule_messages_deletion
from dl_utils.deezer_utils import clean_filename


def _write_zip_sync(zip_path: Path, entries: list) -> None:
    """Compress entries (list of (src, dest) pairs) into one zip file.

    Blocking deflate work; callers must run it via asyncio.to_thread so the
    other concurrent download is not stalled.
    """
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zipf:
        for src_path, dest in entries:
            if Path(src_path).exists():
                zipf.write(src_path, dest)
                print(f"  Adding {src_path} as {dest}")

print("yt-dlp version: ", yt_dlp.version.__version__)

youtube_router = Router()
soundcloud_router = Router()

YOUTUBE_URL_REGEX = re.compile(
    r"https?://(?:"
    r"(?:www\.|m\.|music\.)?youtube\.com/(?:"
    r"watch\?(?:[^\s&]+&)*v=[A-Za-z0-9_-]{11}(?:&[^\s]*)?"
    r"|(?:shorts|embed|live|v)/[A-Za-z0-9_-]{11}(?:[?&#][^\s]*)?"
    r")"
    r"|(?:www\.)?youtu\.be/[A-Za-z0-9_-]{11}(?:[?&#][^\s]*)?"
    r")",
    re.I,
)
SOUNDCLOUD_URL_REGEX = re.compile(
    r"https?://(?:"
    r"(?:www\.|m\.)?soundcloud\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+(?:[/?#][^\s]*)?"
    r"|on\.soundcloud\.com/[A-Za-z0-9_-]+(?:[?#][^\s]*)?"
    r")",
    re.I,
)


def _matched_media_url(text: str, pattern: re.Pattern) -> str:
    match = pattern.search(text or "")
    return match.group(0).rstrip(").,;'\"") if match else ""


_cookies_path = pcfg._credential_file_path("COOKIES_PATH")
COOKIES_PATH = str(_cookies_path) if _cookies_path else None

# Upstream: configurable YouTube player clients
YT_PLAYER_CLIENT = [
    c.strip()
    for c in pcfg.get_env_or_token_file(
        "YT_PLAYER_CLIENT", "tv,web_safari,web_embedded,android_vr"
    ).split(",")
    if c.strip()
]

# Custom: SoundCloud OAuth token
SOUNDCLOUD_OAUTH_TOKEN = pcfg.get_env_or_token_file("SOUNDCLOUD_OAUTH_TOKEN", "").strip()

# Custom: File hosting settings (same as Deezer)
COPY_FILES_PATH = pcfg.copy_files_path()
FILE_LINK_TEMPLATE = pcfg.file_link_template()
FORMAT = pcfg.output_format()  # "zip" to enable zipping

# Custom: Auto-delete configuration
AUTO_DELETE_ENABLED = pcfg.auto_delete_enabled()
AUTO_DELETE_SECONDS = pcfg.auto_delete_seconds()

print(f"Auto-delete enabled: {AUTO_DELETE_ENABLED}")
print(f"Auto-delete seconds: {AUTO_DELETE_SECONDS}")
print(f"Copy files path: {COPY_FILES_PATH}")
print(f"File link template: {FILE_LINK_TEMPLATE}")
print(f"Format mode: {FORMAT}")

if SOUNDCLOUD_OAUTH_TOKEN:
    print("SoundCloud OAuth token configured for HQ downloads")
else:
    print("No SoundCloud OAuth token - using default quality")


async def _delete_original_message(event: types.Message) -> None:
    if not pcfg.auto_delete_enabled():
        return
    try:
        await event.delete()
    except Exception as delete_error:
        print(f"Could not delete original message: {delete_error}")
        return
    user_id, username, first_name = get_user_infos(event)
    print(
        f"USER_DEBUG: Deleted original message from user_id={user_id} "
        f"username={username} first_name={first_name}"
    )
    print(f"Deleted original message from user {user_id}")


# Define separate temp directories
YT_TMP_DIR = Path(TMP_DIR, "yt")
SC_TMP_DIR = Path(TMP_DIR, "sc")

# Ensure temp directories exist
YT_TMP_DIR.mkdir(parents=True, exist_ok=True)
SC_TMP_DIR.mkdir(parents=True, exist_ok=True)


def crop_center(pil_img, crop_width, crop_height):
    img_width, img_height = pil_img.size
    return pil_img.crop(
        (
            (img_width - crop_width) // 2,
            (img_height - crop_height) // 2,
            (img_width + crop_width) // 2,
            (img_height + crop_height) // 2,
        )
    )


async def schedule_message_deletion(message: types.Message, delay_seconds: int):
    """Schedule a message for deletion after specified delay."""
    if not AUTO_DELETE_ENABLED:
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
    """Schedule a file for deletion after specified delay."""
    if not AUTO_DELETE_ENABLED:
        return
    
    async def delete_after_delay():
        await asyncio.sleep(delay_seconds)
        try:
            if file_path.exists():
                file_path.unlink()
                print(f"Auto-deleted server file {file_path} after {delay_seconds} seconds")
        except Exception as e:
            print(f"Failed to auto-delete server file {file_path}: {e}")
    
    asyncio.create_task(delete_after_delay())


async def schedule_messages_deletion(messages: list, delay_seconds: int):
    """Schedule multiple messages for deletion after specified delay."""
    if not AUTO_DELETE_ENABLED:
        return
    
    for message in messages:
        await schedule_message_deletion(message, delay_seconds)


def tag_mp3_file(file_path: str, title: str, artist: str, album: str = None, cover_data: bytes = None):
    """Tag MP3 file with proper ID3v2.4 metadata using mutagen."""
    try:
        # Try to load existing tags or create new ones
        try:
            audio = MP3(file_path, ID3=ID3)
        except ID3NoHeaderError:
            audio = MP3(file_path)
            audio.add_tags()
        
        # Add metadata tags
        if title:
            audio.tags.add(TIT2(encoding=3, text=title))
        if artist:
            audio.tags.add(TPE1(encoding=3, text=artist))
        if album:
            audio.tags.add(TALB(encoding=3, text=album))
        
        # Add album art
        if cover_data:
            audio.tags.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,  # Front cover
                    desc="Cover",
                    data=cover_data
                )
            )
        
        audio.save(v2_version=3)  # Save as ID3v2.3 for maximum compatibility
        print(f"Tagged MP3 file: {title} - {artist}")
    except Exception as e:
        print(f"Warning: Could not write MP3 tags: {e}")


async def create_and_send_zip_yt(event: types.Message, track_title: str, uploader: str, 
                                  audio_path: Path, cover_data: bytes = None, 
                                  upload_date: str = None, webpage_url: str = None):
    """Create a zip archive for YouTube/SoundCloud track and send it."""
    
    sent_messages = []
    server_files_to_delete = []
    
    # Prepare metadata for zip naming
    clean_title = clean_filename(track_title)
    clean_artist = clean_filename(uploader)
    year = upload_date[:4] if upload_date and len(upload_date) >= 4 else "Unknown"
    metadata = {
        "artist": uploader, "title": track_title, "album_title": track_title,
        "year": year, "release_date": upload_date or "",
        "clean_artist": clean_artist, "clean_album_title": clean_title,
        "clean_title": clean_title, "cover_data": cover_data,
        "album_link": webpage_url or "", "track_link": webpage_url or "",
        "artists_list": [uploader], "tracks_api_data": [],
    }
    track = {
        "song_path": str(audio_path), "download_dir": str(audio_path.parent),
        "song_name": track_title, "artist_name": uploader,
        "file_extension": audio_path.suffix, "TRACK_NUMBER": 1,
    }
    return await create_and_send_zip(event, metadata, [track], False)


async def get_youtube_audio(event: types.Message):
    if not event.from_user:
        return

    if not pcfg.platform_enabled(pcfg.YOUTUBE):
        await event.answer(pcfg.platform_disabled_message(pcfg.YOUTUBE))
        return

    print(f"Processing YouTube link from user {event.from_user.id}")
    slot = request_download(event.from_user.id, getattr(getattr(event, "chat", None), "id", None))
    if slot.granted:
        tmp_msg = None
        request_dir = YT_TMP_DIR / "requests" / str(slot.key[1]) / str(event.from_user.id)
        request_dir.mkdir(parents=True, exist_ok=True)
        slot.register_workdir(request_dir)
        try:
            await announce_if_queued(event, slot)
            tmp_msg = await event.answer(__("downloading"))
            ydl_opts = {
                "outtmpl": str(request_dir / "%(id)s.%(ext)s"),
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "320",
                    }
                ],
                "quiet": True,  # Suppress yt-dlp console output
                "no_warnings": True,
                "extractor_args": {"youtube": {"player_client": YT_PLAYER_CLIENT}},
            }

            # if cookies.txt exists, use it
            if (
                COOKIES_PATH is not None
                and os.path.exists(COOKIES_PATH)
                and os.path.isfile(COOKIES_PATH)
                and os.path.getsize(COOKIES_PATH) > 0
            ):
                print("Using cookies for YouTube")
                ydl_opts["cookiefile"] = COOKIES_PATH

            # Download file
            ydl = YoutubeDL(ydl_opts)
            # Run in executor to avoid blocking asyncio loop
            dict_info = await asyncio.to_thread(
                ydl.extract_info,
                _matched_media_url(event.text, YOUTUBE_URL_REGEX),
                download=True,
            )

            if not dict_info:
                print("No information found")
                return

            thumb_url = dict_info.get("thumbnail")
            track_title = dict_info.get("title", "Unknown Title")
            uploader = dict_info.get("uploader", "Unknown Artist")
            track_id = dict_info.get("id", "unknown_id")
            webpage_url = dict_info.get(
                "webpage_url", event.text
            )  # Use original link if webpage_url is missing
            upload_date = dict_info.get("upload_date")  # YYYYMMDD

            # Get thumb
            image_bytes = None
            thumb_data_for_tagging = None
            if thumb_url:
                try:
                    content = requests.get(thumb_url).content
                    image_bytes = io.BytesIO(content)
                    thumb_data_for_tagging = content
                except Exception as img_err:
                    print(f"Error downloading/processing thumbnail: {img_err}")

            location = request_dir / f"{track_id}.mp3"

            # Check if file exists
            if not location.exists():
                raise FileNotFoundError(f"Expected audio file not found at {location}")

            # TAG audio with proper ID3 tags
            tag_mp3_file(
                str(location),
                title=track_title,
                artist=uploader,
                album=None,
                cover_data=thumb_data_for_tagging
            )

            # Check if we should zip or send directly
            if FORMAT == "zip":
                # Use zip mode like Deezer
                await create_and_send_zip_yt(
                    event, track_title, uploader, location,
                    cover_data=thumb_data_for_tagging,
                    upload_date=upload_date,
                    webpage_url=webpage_url
                )
            else:
                # Original direct send mode
                upload_date_str = "Unknown date"
                if upload_date and len(upload_date) == 8:
                    try:
                        upload_date_str = (
                            f"{upload_date[6:8]}/{upload_date[4:6]}/{upload_date[0:4]}"
                        )
                    except Exception:
                        pass

                # Send cover
                if image_bytes:
                    try:
                        await event.answer_photo(
                            BufferedInputFile(image_bytes.getvalue(), filename="cover.jpg"),
                            caption=(
                                "<b>Track: {}</b>"
                                '\n{} - {}\n\n<a href="{}">' + __("track_link") + "</a>"
                            ).format(
                                track_title,
                                uploader,
                                upload_date_str,
                                webpage_url,
                            ),
                            parse_mode="HTML",
                        )
                        image_bytes.seek(0)
                    except Exception as photo_err:
                        print(f"Error sending photo: {photo_err}")
                else:
                    await event.answer(
                        (
                            "<b>Track: {}</b>"
                            '\n{} - {}\n\n<a href="{}">' + __("track_link") + "</a>"
                        ).format(
                            track_title,
                            uploader,
                            upload_date_str,
                            webpage_url,
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )

                # Create smaller thumb for sending with audio message
                thumb_for_sending = None
                if image_bytes:
                    try:
                        image_bytes.seek(0)
                        roi_img = crop_center(Image.open(image_bytes), 80, 80)
                        img_byte_arr = io.BytesIO()
                        if roi_img.mode in ("RGBA", "P"):
                            roi_img = roi_img.convert("RGB")
                        roi_img.save(img_byte_arr, format="jpeg")
                        thumb_for_sending = BufferedInputFile(
                            img_byte_arr.getvalue(), filename="thumb.jpg"
                        )
                    except Exception as thumb_proc_err:
                        print(f"Error processing thumbnail for sending: {thumb_proc_err}")

                # Send audio
                audio_message = await event.answer_audio(
                    FSInputFile(location),
                    title=track_title,
                    performer=uploader,
                    thumbnail=thumb_for_sending,
                    disable_notification=True,
                )
                
                # Schedule auto-deletion of audio message
                await schedule_message_deletion(audio_message, AUTO_DELETE_SECONDS)
            
            await _delete_original_message(event)

            # Clean up temp file
            try:
                os.remove(location)
            except FileNotFoundError:
                pass
                
        except yt_dlp.utils.DownloadError as dl_err:
            print(f"yt-dlp download error: {dl_err}")
            await event.answer(__("download_error_specific").format(str(dl_err)))
        except asyncio.CancelledError:
            if not slot.cancelled:
                raise
        except Exception as e:
            traceback.print_exc()
            await event.answer(
                __("download_error") + f"\n<code>{e}</code>", parse_mode="HTML"
            )
        finally:
            if tmp_msg is not None:
                await tmp_msg.delete()
            release_download(slot)
            shutil.rmtree(request_dir, ignore_errors=True)
    else:
        tmp_err_msg = await event.answer(__("running_download"))
        await event.delete()
        await asyncio.sleep(2)
        await tmp_err_msg.delete()


@soundcloud_router.message(F.text.regexp(SOUNDCLOUD_URL_REGEX))
async def get_soundcloud_audio(event: types.Message):
    if not event.from_user:
        return

    if not pcfg.platform_enabled(pcfg.SOUNDCLOUD):
        await event.answer(pcfg.platform_disabled_message(pcfg.SOUNDCLOUD))
        return

    print(f"Processing SoundCloud link from user {event.from_user.id}")
    slot = request_download(event.from_user.id, getattr(getattr(event, "chat", None), "id", None))
    if slot.granted:
        tmp_msg = None
        request_dir = SC_TMP_DIR / "requests" / str(slot.key[1]) / str(event.from_user.id)
        request_dir.mkdir(parents=True, exist_ok=True)
        slot.register_workdir(request_dir)
        try:
            await announce_if_queued(event, slot)
            tmp_msg = await event.answer(__("downloading"))
            ydl_opts = {
                "outtmpl": str(request_dir / "%(id)s.%(ext)s"),
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "320",
                    }
                ],
                "quiet": True,  # Suppress yt-dlp console output
                "no_warnings": True,
            }

            # Add SoundCloud OAuth token if available for HQ downloads
            if SOUNDCLOUD_OAUTH_TOKEN:
                print("Using SoundCloud OAuth token for HQ downloads")
                ydl_opts["username"] = "oauth"
                ydl_opts["password"] = SOUNDCLOUD_OAUTH_TOKEN

            # Download file
            ydl = YoutubeDL(ydl_opts)
            # Run in executor to avoid blocking asyncio loop
            dict_info = await asyncio.to_thread(
                ydl.extract_info,
                _matched_media_url(event.text, SOUNDCLOUD_URL_REGEX),
                download=True,
            )

            if not dict_info:
                print("No information found")
                return

            # Extract metadata (keys might differ slightly from YouTube)
            thumb_url = dict_info.get("thumbnail")
            # SoundCloud often has 'track' and 'artist' instead of 'title' and 'uploader'
            track_title = dict_info.get("track") or dict_info.get(
                "title", "Unknown Title"
            )
            uploader = dict_info.get("artist") or dict_info.get(
                "uploader", "Unknown Artist"
            )
            track_id = dict_info.get("id", "unknown_sc_id")
            webpage_url = dict_info.get("webpage_url", event.text)
            upload_date = dict_info.get("upload_date")  # YYYYMMDD if available

            # Get thumb
            image_bytes = None
            thumb_data_for_tagging = None
            if thumb_url:
                try:
                    content = requests.get(thumb_url).content
                    image_bytes = io.BytesIO(content)
                    thumb_data_for_tagging = content
                except Exception as img_err:
                    print(
                        f"Error downloading/processing SoundCloud thumbnail: {img_err}"
                    )

            location = request_dir / f"{track_id}.mp3"

            # Check if file exists
            if not location.exists():
                raise FileNotFoundError(f"Expected audio file not found at {location}")

            # TAG audio with proper ID3 tags
            tag_mp3_file(
                str(location),
                title=track_title,
                artist=uploader,
                album=None,
                cover_data=thumb_data_for_tagging
            )

            # Check if we should zip or send directly
            if FORMAT == "zip":
                # Use zip mode like Deezer
                await create_and_send_zip_yt(
                    event, track_title, uploader, location,
                    cover_data=thumb_data_for_tagging,
                    upload_date=upload_date,
                    webpage_url=webpage_url
                )
            else:
                # Original direct send mode
                # Send cover
                if image_bytes:
                    try:
                        await event.answer_photo(
                            BufferedInputFile(image_bytes.getvalue(), filename="cover.jpg"),
                            caption=(
                                "<b>Track: {}</b>"
                                '\nArtist: {}\n\n<a href="{}">' + __("track_link") + "</a>"
                            ).format(
                                track_title,
                                uploader,
                                webpage_url,
                            ),
                            parse_mode="HTML",
                        )
                        image_bytes.seek(0)
                    except Exception as photo_err:
                        print(f"Error sending SoundCloud photo: {photo_err}")
                else:
                    await event.answer(
                        (
                            "<b>Track: {}</b>"
                            '\nArtist: {}\n\n<a href="{}">' + __("track_link") + "</a>"
                        ).format(
                            track_title,
                            uploader,
                            webpage_url,
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )

                # Create smaller thumb for sending with audio message
                thumb_for_sending = None
                if image_bytes:
                    try:
                        image_bytes.seek(0)
                        roi_img = crop_center(Image.open(image_bytes), 80, 80)
                        img_byte_arr = io.BytesIO()
                        if roi_img.mode in ("RGBA", "P"):
                            roi_img = roi_img.convert("RGB")
                        roi_img.save(img_byte_arr, format="jpeg")
                        thumb_for_sending = BufferedInputFile(
                            img_byte_arr.getvalue(), filename="thumb.jpg"
                        )
                    except Exception as thumb_proc_err:
                        print(
                            f"Error processing SoundCloud thumbnail for sending: {thumb_proc_err}"
                        )

                # Send audio
                audio_message = await event.answer_audio(
                    FSInputFile(location),
                    title=track_title,
                    performer=uploader,
                    thumbnail=thumb_for_sending,
                    disable_notification=True,
                )
                
                # Schedule auto-deletion of audio message
                await schedule_message_deletion(audio_message, AUTO_DELETE_SECONDS)
            
            await _delete_original_message(event)

            # Clean up temp file
            try:
                os.remove(location)
            except FileNotFoundError:
                pass
                
        except yt_dlp.utils.DownloadError as dl_err:
            print(f"yt-dlp download error (SoundCloud): {dl_err}")
            await event.answer(__("download_error_specific").format(str(dl_err)))
        except asyncio.CancelledError:
            if not slot.cancelled:
                raise
        except Exception as e:
            traceback.print_exc()
            await event.answer(
                __("download_error") + f"\n<code>{e}</code>", parse_mode="HTML"
            )
        finally:
            if tmp_msg is not None:
                await tmp_msg.delete()
            release_download(slot)
            shutil.rmtree(request_dir, ignore_errors=True)
    else:
        tmp_err_msg = await event.answer(__("running_download"))
        await event.delete()
        await asyncio.sleep(2)
        await tmp_err_msg.delete()
