"""Multi-platform link router: Qobuz, Tidal, Amazon Music, Spotify, Apple Music.

Every handler here obeys the same contract, which is the one `utils.py`
documents and `handlers/deezer.py` implements:

    slot = request_download(user_id, getattr(getattr(event, "chat", None), "id", None))
    if not slot.granted:
        await event.answer(__("running_download"), reply_markup=ReplyKeyboardRemove())
        return
    try:
        await announce_if_queued(event, slot)
        ...engine call, zip, deliver...
    finally:
        release_download(slot)
        cleanup_dir(workdir)

so a refused request never touches the queue, a queued user always hears about
it, the slot is always returned even when an engine throws, and the temp tree
under `TMP_DIR/<platform>/<kind>/<id>` is always removed.

Link routing works in two stages. `handle_streaming_shortlink` expands mobile
wrappers (spotify.link, amzn.to, apple.co, ...) through
`dl_utils.link_resolver`, then re-dispatches to the concrete handler for the
platform that survived. Each concrete handler is also registered on its own
canonical regex, so a link that was already canonical never pays for a network
round trip.

Delivery reuses `dl_utils.archive_delivery` verbatim — the same zip layout, the
same hashed archive name, the same copy-mode link, the same multipart split and
the same auto-delete timers as Deezer. Adding a platform must never mean adding
a second packaging path.
"""

import asyncio
import html
import re
import time
import traceback
from pathlib import Path

from aiogram import Router, types
from aiogram.filters import BaseFilter

from dl_utils import metadata_bridge
from dl_utils import platform_config as pcfg
from dl_utils.archive_delivery import (
    _ProgressEditor,  # noqa: F401 - compatibility re-export
    _safe_delete,
    append_issue_summary,
    create_and_send_artist_zip,
    create_and_send_zip,
    get_user_infos,
)
from dl_utils.deezer_utils import clean_filename
from dl_utils.link_resolver import (
    KIND_ALBUM,
    KIND_ARTIST,
    KIND_TRACK,
    PLATFORM_AMAZON,
    PLATFORM_APPLE,
    PLATFORM_QOBUZ,
    PLATFORM_SPOTIFY,
    PLATFORM_TIDAL,
    ResolvedLink,
    extract_url,
    is_shortlink,
    parse_link,
    resolve_link,
)
from dl_utils.provider_engines import (
    EngineError,
    cleanup_dir,
    download_artist_discography,
    download_release,
    discover_releases,
    infer_release_from_paths,
    read_cover,
    staging_dir,
    tracks_from_paths,
)
from utils import (
    __,
    announce_if_queued,
    release_download,
    request_download,
    scheduler_snapshot,
)

streaming_router = Router(name="streaming")

TOO_MANY_RELEASES_MESSAGE = (
    "Error: This artist page has too many releases. To prevent server overload, "
    "it cannot be processed."
)

# Platforms that publish an artist catalogue without any authentication, so a
# discography can be enumerated release by release before a byte is fetched.
# Everywhere else the engine resolves the artist URL in one go.
FREE_CATALOGUE_PLATFORMS = frozenset({pcfg.DEEZER})

# ---------------------------------------------------------------------------
# Hosts this router claims
# ---------------------------------------------------------------------------
# Canonical hosts plus their mobile wrappers. Deezer, YouTube and SoundCloud are
# deliberately absent: those belong to handlers/deezer.py and handlers/yt_dlp.py.
# Object-id parsing itself lives in link_resolver.classify(), which is the single
# source of truth shared with the shortlink path — this router never keeps a
# second copy of those regexes.
CLAIM_HOSTS = (
    r"(?:(?:www|open|play)\.)?qobuz\.com|"
    r"(?:(?:www|listen)\.)?tidal\.com|tidal\.page\.link|t\.idal\.com|"
    r"music\.amazon\.[a-z.]+|amzn\.to|a\.co|"
    r"open\.spotify\.com|spotify\.link|spoti\.fi|"
    r"(?:music|itunes)\.apple\.com|apple\.co"
)

ANY_LINK_RE = re.compile(rf"^https?://(?:{CLAIM_HOSTS})/", re.I)

#: Platforms this router owns. Deezer, YouTube and SoundCloud are handled by
#: their own routers and must never appear here.
ROUTED_PLATFORMS = (
    PLATFORM_QOBUZ,
    PLATFORM_TIDAL,
    PLATFORM_AMAZON,
    PLATFORM_SPOTIFY,
    PLATFORM_APPLE,
)


class _StreamingLinkFilter(BaseFilter):
    """Match a message whose text contains a link on one of the routed platforms.

    The URL is pulled out of the message rather than assumed to be the whole
    message: a link pasted with a caption is the common case, and a regex
    filter on F.text would silently drop it.
    """

    async def __call__(self, message: types.Message) -> bool:
        url = extract_url(message.text)
        if not url:
            return False
        # Deezer links belong to handlers/deezer.py; never intercept them here.
        if re.search(r"deezer\.com|deezer\.page\.link|dzr\.page\.link", url, re.I):
            return False
        return bool(ANY_LINK_RE.match(url))


# ---------------------------------------------------------------------------
# Shared request body
# ---------------------------------------------------------------------------
def _failure_text(platform: str, kind: str, url: str, error: Exception) -> str:
    label = pcfg.PLATFORM_LABELS.get(platform, platform)
    detail = " ".join((str(error).strip() or error.__class__.__name__).splitlines())
    if len(detail) > 600:
        detail = detail[:600] + "…"
    engine = _direct_engine_label(platform)
    return (
        f"❌ {label} {kind.title()} Download Failed\n"
        f"• Requested: {url}\n"
        f"• Engine: {engine}\n"
        f"• Fallback: {'enabled' if pcfg.fallback_enabled() else 'disabled'} "
        f"(FALLBACK_ENABLED={int(pcfg.fallback_enabled())})\n"
        f"• Reason: {detail}"
    )


def _direct_engine_label(platform: str) -> str:
    return pcfg.DIRECT_ENGINES.get(platform, "direct")


async def _reply_error(event, platform: str, error: Exception, kind: str = "track", url: str = "") -> None:
    await event.answer(_failure_text(platform, kind, url, error))


def _effective_kind(link: ResolvedLink) -> str:
    """KIND_TRACK when the link means a single track, else its own kind."""
    if link.kind == KIND_ARTIST:
        return KIND_ARTIST
    if link.track_id or link.kind == KIND_TRACK:
        return KIND_TRACK
    return link.kind


def _download_url(link: ResolvedLink) -> str:
    """The URL handed to the engine, preserving Apple album-track semantics."""
    if link.platform == PLATFORM_AMAZON and link.track_id:
        return f"https://music.amazon.com/tracks/{link.track_id}"
    return link.url


# ---------------------------------------------------------------------------
# The one handler for track and album requests
# ---------------------------------------------------------------------------
async def _handle_release(event: types.Message, link: ResolvedLink) -> None:
    """Download one track or album on `link.platform` and deliver the archive."""
    platform = link.platform
    kind = _effective_kind(link)
    url = _download_url(link)
    user_id, username, first_name = get_user_infos(event)
    print(
        f"USER_DEBUG: {platform} {kind} request from user_id={user_id} "
        f"username={username} first_name={first_name} -> {url}"
    )

    slot = request_download(user_id, getattr(getattr(event, "chat", None), "id", None))
    if not slot.granted:
        print(
            f"USER_DEBUG: rejected {platform} {kind} request user_id={user_id} "
            f"status={slot.status}"
        )
        await event.answer(
            __("running_download"), reply_markup=types.ReplyKeyboardRemove()
        )
        return

    workdir = staging_dir(platform, kind, link.object_id)
    slot.register_workdir(workdir)
    tmp_msg = None
    try:
        await announce_if_queued(event, slot)
        tmp_msg = await event.answer(__("downloading"))
        progress = _ProgressEditor(tmp_msg)
        label = pcfg.PLATFORM_LABELS.get(platform, platform)
        print(f"[progress] 🔍 Resolving {label} {kind} metadata...")
        await progress(f"🔍 Resolving {label} {kind} metadata...", force=True)

        # Metadata first, audio second: the bridge is cheap and its result names
        # the archive, the caption and the folder layout. A failure here is not
        # fatal — the engine's own reading of the file tree is the fallback.
        info = None
        try:
            info = await metadata_bridge.release_info(
                platform=platform,
                kind=kind,
                object_id=link.object_id,
                track_id=link.track_id,
                storefront=link.storefront,
                url=link.original_url or url,
            )
        except Exception as e:
            print(f"[streaming] {platform} metadata lookup failed: {e}")

        result = await download_release(
            platform, url, kind, workdir, progress_cb=progress
        )
        metadata = metadata_bridge.merge_info(result.metadata, info)
        metadata["quality_label"] = getattr(result.metadata, "quality_label", "")
        metadata["source_label"] = getattr(
            result.metadata, "source_label", "Direct Engine"
        )
        if kind == KIND_TRACK and not metadata.get("track_link"):
            metadata["track_link"] = url
        elif kind == KIND_ALBUM and not metadata.get("album_link"):
            metadata["album_link"] = url
        from dl_utils.archive_delivery import summarize_track_issues

        issue_lines = [
            line
            for lines in summarize_track_issues(result.tracks, result.metadata.tracks_api_data)
            for line in lines
        ]
        metadata["caption_suffix"] = (
            "\n\n" + "\n".join(html.escape(line) for line in issue_lines)
            if issue_lines
            else ""
        )

        await progress(f"📦 Packaging {result.track_count} track(s) into ZIP archive...", force=True)
        await _safe_delete(tmp_msg)
        tmp_msg = None
        await create_and_send_zip(event, metadata, result.tracks, is_album=kind == KIND_ALBUM)
        print(
            f"[streaming] {platform} {kind} {link.object_id}: "
            f"{result.track_count} track(s) delivered via {result.provider}"
        )
    except asyncio.CancelledError:
        if not slot.cancelled:
            raise
        await _safe_delete(tmp_msg)
    except EngineError as e:
        print(f"[streaming] {platform} {kind} engine error: {e}")
        await _safe_delete(tmp_msg)
        await _reply_error(event, platform, e, kind, url)
    except Exception as e:
        traceback.print_exc()
        await _safe_delete(tmp_msg)
        await _reply_error(event, platform, e, kind, url)
    finally:
        release_download(slot)
        cleanup_dir(workdir)


# ---------------------------------------------------------------------------
# The one handler for artist requests
# ---------------------------------------------------------------------------
async def _handle_artist(event: types.Message, link: ResolvedLink) -> None:
    """Download a full discography on `link.platform` and deliver one archive."""
    platform = link.platform
    user_id, username, first_name = get_user_infos(event)
    print(
        f"USER_DEBUG: {platform} artist request from user_id={user_id} "
        f"username={username} first_name={first_name} -> {link.url}"
    )

    # Before a slot is taken: refusing a disabled feature must not push another
    # user's request out of the queue for nothing.
    if not pcfg.artist_download_enabled(platform):
        await event.answer(pcfg.artist_disabled_message(platform))
        return

    try:
        artist_data, releases = await metadata_bridge.artist_album_links(
            platform, link.object_id, link.storefront
        )
    except Exception as e:
        print(f"[streaming] {platform} artist catalogue lookup failed: {e}")
        artist_data, releases = None, []

    if not releases and platform in FREE_CATALOGUE_PLATFORMS:
        # A free catalogue exists and came back empty: the artist really has no
        # albums there, so say so rather than starting a doomed bulk download.
        await event.answer("⚠️ That artist has no releases on this platform.")
        return

    if releases and len(releases) > pcfg.max_artist_releases():
        print(
            f"{platform} artist {link.object_id} has {len(releases)} releases, "
            f"above the limit of {pcfg.max_artist_releases()}. Aborting."
        )
        await event.answer(TOO_MANY_RELEASES_MESSAGE)
        return

    slot = request_download(user_id, getattr(getattr(event, "chat", None), "id", None))
    if not slot.granted:
        print(
            f"USER_DEBUG: rejected {platform} artist request user_id={user_id} "
            f"status={slot.status}"
        )
        await event.answer(
            __("running_download"), reply_markup=types.ReplyKeyboardRemove()
        )
        return

    workdir = staging_dir(platform, "artist", link.object_id)
    slot.register_workdir(workdir)
    tmp_msg = None
    try:
        await announce_if_queued(event, slot)
        if releases:
            count = f"{len(releases)} release(s)"
        else:
            count = "the full discography"
        tmp_msg = await event.answer(
            f"⏳ Queued {count} from {pcfg.PLATFORM_LABELS.get(platform, platform)}. "
            "This takes a while."
        )
        progress = _ProgressEditor(tmp_msg)

        if releases:
            downloaded = await download_artist_discography(
                platform, link.url, workdir, releases, progress_cb=progress
            )
        else:
            downloaded = await _download_discography_in_one_go(
                platform, link, workdir
            )

        artist_metadata = await _artist_identity(link, artist_data)
        await _safe_delete(tmp_msg)
        tmp_msg = None
        await create_and_send_artist_zip(event, artist_metadata, downloaded)
        print(
            f"[streaming] {platform} artist {link.object_id}: "
            f"{len(downloaded)} release(s) delivered"
        )
    except asyncio.CancelledError:
        if not slot.cancelled:
            raise
        await _safe_delete(tmp_msg)
    except EngineError as e:
        print(f"[streaming] {platform} artist engine error: {e}")
        await _safe_delete(tmp_msg)
        await _reply_error(event, platform, e)
    except Exception as e:
        traceback.print_exc()
        await _safe_delete(tmp_msg)
        await _reply_error(event, platform, e)
    finally:
        release_download(slot)
        cleanup_dir(workdir)


async def _download_discography_in_one_go(
    platform: str, link: ResolvedLink, workdir: Path
):
    """Let the engine resolve the whole discography, then split it per release.

    When a direct engine returns a multi-release tree, each release folder is
    split back apart here so the archive keeps the canonical per-release layout
    instead of arriving as one flattened blob.
    """
    result = await download_release(platform, link.url, KIND_ARTIST, workdir)
    groups = discover_releases(Path(result.workdir or workdir))

    if len(groups) <= 1:
        # A single release folder or a flat tree: keep the whole result as one
        # entry rather than inventing a split.
        release = {"id": link.object_id, "title": result.metadata.album_title}
        return [(release, result.metadata, result.tracks)]

    downloaded = []
    for release_dir, audio in groups:
        # workdir has to be passed: without it infer_release_from_paths cannot
        # tell a real release folder from a staging path component, so every
        # release in the discography collapses to "Unknown Album".
        artist, album, year = infer_release_from_paths(
            audio,
            fallback_artist=result.metadata.artist,
            workdir=Path(result.workdir or workdir),
        )
        cover_path = None
        for candidate in ("cover.jpg", "cover.jpeg", "folder.jpg"):
            probe = release_dir / candidate
            if probe.exists():
                cover_path = str(probe)
                break
        metadata = type(result.metadata)(
            artist=artist,
            title=album,
            album_title=album,
            year=year,
            release_date=year,
            kind=KIND_ALBUM,
            album_link=link.url,
            cover_data=read_cover(cover_path) or result.metadata.cover_data,
            artists_list=[artist],
        )
        downloaded.append(
            (
                {"id": release_dir.name, "title": album},
                metadata,
                tracks_from_paths(audio, release_dir, artist_fallback=artist),
            )
        )
    if not downloaded:
        raise EngineError(f"{platform}: discography produced no releasable folders")
    return downloaded


async def _artist_identity(link: ResolvedLink, artist_data: dict | None) -> dict:
    """{name, clean_name, link, cover_data} for the artist zip caption."""
    artist_data = artist_data or {}
    name = artist_data.get("name") or "Unknown Artist"
    cover = None
    picture = artist_data.get("picture")
    if picture:
        try:
            cover = await metadata_bridge.fetch_bytes(picture)
        except Exception as e:
            print(f"[streaming] artist picture fetch failed: {e}")
    return {
        "id": link.object_id,
        "name": name,
        "clean_name": clean_filename(name),
        "link": artist_data.get("link") or link.url,
        "cover_data": cover,
    }


# ---------------------------------------------------------------------------
# Concrete handlers, one per platform
# ---------------------------------------------------------------------------
class _PlatformLinkFilter(BaseFilter):
    """Match messages whose link classifies to one platform, offline.

    Classification is regex-only here, so a canonical URL is routed without any
    network call. Wrapper links fall through to
    `handle_streaming_shortlink`, which is registered last.
    """

    def __init__(self, platform: str):
        self.platform = platform

    async def __call__(self, message: types.Message) -> bool:
        url = extract_url(message.text)
        if not url or not ANY_LINK_RE.match(url):
            return False
        link = parse_link(url)
        return link is not None and link.platform == self.platform


def _register_platform_handler(platform: str) -> None:
    @streaming_router.message(_PlatformLinkFilter(platform))
    async def platform_handler(event: types.Message, platform=platform):
        url = extract_url(event.text)
        link = parse_link(url)
        if link is None:
            return
        if not pcfg.platform_enabled(platform):
            await _refuse_disabled_platform(event, platform)
            return
        if link.kind == KIND_ARTIST:
            await _handle_artist(event, link)
        else:
            await _handle_release(event, link)


for _platform in (
    PLATFORM_QOBUZ,
    PLATFORM_TIDAL,
    PLATFORM_AMAZON,
    PLATFORM_SPOTIFY,
    PLATFORM_APPLE,
):
    _register_platform_handler(_platform)


# ---------------------------------------------------------------------------
# Shortlink expansion, then dispatch
# ---------------------------------------------------------------------------
@streaming_router.message(_StreamingLinkFilter())
async def handle_streaming_shortlink(event: types.Message):
    """Expand a mobile wrapper link and route it to the right platform.

    Reached only when the offline pass above did not claim the message, i.e. for
    spotify.link / amzn.to / apple.co / t.idal.com style shares and for
    canonical shapes classify() could not read.
    """
    raw = extract_url(event.text)
    user_id, username, first_name = get_user_infos(event)

    if not is_shortlink(raw):
        # Canonical but unread by the concrete pass: classify() has broader
        # patterns (e.g. www.qobuz.com/.../album/slug/upc), so try it once more
        # before giving up. Nothing to resolve, so no network call.
        link = parse_link(raw)
        if link is None or link.platform not in ROUTED_PLATFORMS:
            return
        await _dispatch(event, link)
        return

    try:
        link = await resolve_link(raw)
    except Exception as e:
        print(f"[streaming] shortlink resolution failed for {raw}: {e}")
        await event.answer(__("resolve_error"))
        return

    if link is None or link.platform not in ROUTED_PLATFORMS:
        print(
            f"USER_DEBUG: unresolved shortlink from user_id={user_id} "
            f"username={username} first_name={first_name}: {raw}"
        )
        await event.answer(
            "⚠️ I could not recognise that link. Send a track, album or artist "
            "link from Deezer, Qobuz, Tidal, Amazon Music, Spotify or Apple Music."
        )
        return

    print(f"[streaming] {raw} -> {link.url} ({link.platform}/{link.kind})")
    await _dispatch(event, link)


async def _refuse_disabled_platform(event: types.Message, platform: str) -> None:
    label = pcfg.PLATFORM_LABELS.get(platform, platform)
    await event.answer(
        f"⚠️ {label} downloads are currently disabled on this bot. "
        "Send a link for an enabled platform instead."
    )


async def _dispatch(event: types.Message, link: ResolvedLink) -> None:
    # Ownership check first: Deezer, YouTube and SoundCloud have their own
    # routers and engines. Without this, any future caller that hands _dispatch
    # an unrouted link would push it through the streaming engines.
    if link is None or link.platform not in ROUTED_PLATFORMS:
        return
    if not pcfg.platform_enabled(link.platform):
        await _refuse_disabled_platform(event, link.platform)
        return
    if link.kind == KIND_ARTIST:
        await _handle_artist(event, link)
    else:
        await _handle_release(event, link)


# ---------------------------------------------------------------------------
# Public helpers used by main.py
# ---------------------------------------------------------------------------
def supported_platforms() -> list[str]:
    """Labels of every enabled platform, in the order /start should list them.

    YouTube and SoundCloud are included: they are handled by handlers/yt_dlp.py
    but the user sees one bot, not three routers.
    """
    return [
        pcfg.PLATFORM_LABELS[p] for p in pcfg.PLATFORMS if pcfg.platform_enabled(p)
    ]


def status_report() -> str:
    """Multi-line HTML text for the whitelisted /status command."""
    lines = ["<b>Platform status</b>", ""]
    for platform in pcfg.PLATFORMS:
        state = pcfg.platform_status(platform)
        flag = "✅" if state["enabled"] else "▫️"
        enabled = "enabled" if state["enabled"] else "disabled"
        creds = "creds ✓" if state["has_credentials"] else "no creds"
        line = (
            f"{flag} <b>{html.escape(state['label'])}</b> — {enabled} · engine "
            f"<code>{html.escape(state['engine'])}</code> · {creds}"
        )
        if platform == pcfg.DEEZER:
            line += " · artist ✓" if state["artist_download"] else " · artist ✗"
        lines.append(line)

    snap = scheduler_snapshot()
    lines += [
        "",
        f"<b>Downloads</b> — {snap['active_count']}/{snap['max_concurrent']} active, "
        f"{snap['queued_count']}/{snap['max_queued']} queued",
    ]
    if snap["active_users"]:
        lines.append("Active user IDs: " + ", ".join(str(u) for u in snap["active_users"]))
    if snap["queued_users"]:
        lines.append("Queued user IDs: " + ", ".join(str(u) for u in snap["queued_users"]))

    lines += [
        "",
        f"Lossless (ENABLE_FLAC): {'on' if pcfg.flac_enabled() else 'off'}",
        f"Cross-platform fallback (FALLBACK_ENABLED): {'on' if pcfg.fallback_enabled() else 'off'}",
        f"Auto-delete: {'on' if pcfg.auto_delete_enabled() else 'off'} "
        f"({pcfg.auto_delete_seconds()}s)",
        f"Hosting: {'COPY_FILES_PATH' if pcfg.copy_files_path() else 'direct Telegram upload'}",
        f"Max artist releases: {pcfg.max_artist_releases()}",
    ]
    return "\n".join(lines)


__all__ = ["status_report", "streaming_router", "supported_platforms"]
