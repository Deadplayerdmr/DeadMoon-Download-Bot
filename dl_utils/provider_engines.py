# dl_utils/provider_engines.py
"""Direct multi-platform download engines with optional Deezer ARL fallback."""

import asyncio
import contextvars
import functools
import importlib.util
import inspect
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import utils
from dl_utils import metadata_bridge, platform_config as pcfg
from dl_utils.link_resolver import KIND_ALBUM, KIND_TRACK, classify
from utils import (
    TMP_DIR,
    current_download_slot,
    get_bool_env,
    get_env_or_token_file,
)

AUDIO_EXTENSIONS = frozenset(
    {".flac", ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".aiff", ".wv", ".tta"}
)

DEFAULT_SUBPROCESS_TIMEOUT = 3600
SPOTIFY_RATE_LIMIT_COOLDOWN_SECONDS = 62.0
ARTIST_RELEASE_TYPES = frozenset({"album", "ep", "single", "compile"})
_progress_callback = contextvars.ContextVar("deadmoon_engine_progress", default=None)


class EngineError(Exception):
    """A download engine failed in a way worth telling the user about."""


@dataclass
class ReleaseMetadata:
    artist: str = "Unknown Artist"
    title: str = "Unknown Album"
    album_title: str = ""
    year: str = ""
    release_date: str = ""
    kind: str = KIND_ALBUM
    album_link: str = ""
    track_link: str = ""
    cover_data: bytes | None = None
    source_label: str = "Deezer"
    quality_label: str = ""
    artists_list: list = field(default_factory=list)
    tracks_api_data: list = field(default_factory=list)
    isrcs: list = field(default_factory=list)
    caption_suffix: str = ""

    def __post_init__(self) -> None:
        if not self.album_title:
            self.album_title = self.title
        if not self.artists_list:
            self.artists_list = [self.artist]
        if not self.release_date:
            self.release_date = self.year or "00/00/0000"


@dataclass
class EngineResult:
    metadata: ReleaseMetadata
    tracks: list
    mode: str
    platform: str
    provider: str = ""
    workdir: str | None = None

    @property
    def track_count(self) -> int:
        return len(self.tracks)


def staging_dir(platform: str, kind: str, object_id) -> Path:
    path = Path(TMP_DIR) / platform / kind / str(object_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_dir(path) -> None:
    if not path:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError as e:
        print(f"[engine] could not clean up {path}: {e}")


def _engine_env(workdir: Path | None = None) -> dict:
    env = dict(os.environ)
    if workdir is not None:
        runtime_dir = Path(
            tempfile.mkdtemp(prefix=".dotify-runtime-", dir=workdir)
        )
        for name in ("home", "config", "cache"):
            (runtime_dir / name).mkdir(mode=0o700)
        env["HOME"] = str(runtime_dir / "home")
        env["XDG_CONFIG_HOME"] = str(runtime_dir / "config")
        env["XDG_CACHE_HOME"] = str(runtime_dir / "cache")
    try:
        import certifi

        env.setdefault("SSL_CERT_FILE", certifi.where())
        env.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except Exception:
        pass
    env["PYTHONUNBUFFERED"] = "1"
    project_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (project_root, env.get("PYTHONPATH", "")))
    )
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    env["COLUMNS"] = "120"
    return env


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_DOTIFY_TRANSIENT_ERRORS = (
    "[errno -3] try again",
    "eai_again",
    "temporary failure in name resolution",
    "connection reset by peer",
    "serverdisconnectederror",
    "timeouterror",
)


def _strip_ansi(value: str) -> str:
    return _ANSI_ESCAPE.sub("", value)


def _dotify_transient_failure(output: str) -> bool:
    normalized = _strip_ansi(output).casefold()
    return any(marker in normalized for marker in _DOTIFY_TRANSIENT_ERRORS)


_TRACK_OUTPUT = re.compile(
    r"(?:downloading|fetching|preparing)(?:\s+track)?[\s:]*(.+?)(?:\s*\(\d+/\d+\))?$",
    re.I,
)


_PROGRESS_ERROR_MARKERS = (
    "401", "403", "429", "unauthorized", "forbidden", "too many requests",
    "error", "exception", "traceback", ".py:", "│", "╭", "╰", "╪", "─",
)


def _progress_from_output(line: str, seen_files: set[Path], cwd: Path) -> str | None:
    line = _strip_ansi(line)
    lowered = line.casefold()
    if any(marker in lowered for marker in _PROGRESS_ERROR_MARKERS):
        return None
    match = _TRACK_OUTPUT.search(line.strip())
    if match:
        title = match.group(1).strip(" .#")
        if title and len(title) < 300:
            return f"⬇️ Downloading: {title}..."
    current = set(find_audio(cwd))
    new = sorted(current - seen_files, key=_natural_key)
    seen_files.update(current)
    if new:
        number, artist, title = _split_track_stem(new[-1].stem)
        prefix = f"Track {number} - " if number else ""
        return f"⬇️ Downloading: {prefix}{artist} - {title}..."
    return None


async def _run_subprocess(
    cmd: list,
    cwd: Path,
    timeout: int | None = None,
    progress_cb=None,
) -> tuple[int, str]:
    timeout = timeout or pcfg.get_int_env("ENGINE_TIMEOUT_SECONDS", DEFAULT_SUBPROCESS_TIMEOUT)
    progress_cb = progress_cb or _progress_callback.get()
    print(f"[engine] {' '.join(cmd[:3])} <{len(cmd) - 3} args> in {cwd}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(cwd),
        env=_engine_env(cwd if cmd and Path(cmd[0]).name == "dotify" else None),
        **({"start_new_session": True} if hasattr(os, "killpg") else {}),
    )
    slot = current_download_slot()
    if slot is not None:
        slot.process = proc
    deadline = asyncio.get_running_loop().time() + timeout
    tail = []
    seen_files = set(find_audio(cwd))
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            tail.append(text)
            del tail[:-25]
            if progress_cb:
                update = _progress_from_output(text, seen_files, cwd)
                if update:
                    await _report(progress_cb, update)
        await asyncio.wait_for(proc.wait(), timeout=max(0.001, deadline - asyncio.get_running_loop().time()))
    except asyncio.TimeoutError:
        _kill_process_tree(proc)
        await proc.wait()
        raise EngineError(f"engine timed out after {timeout}s")
    except (asyncio.CancelledError, Exception):
        if proc.returncode is None:
            _kill_process_tree(proc)
        await proc.wait()
        raise
    finally:
        if slot is not None and slot.process is proc:
            slot.process = None

    return int(proc.returncode or 0), "\n".join(tail)


def _kill_process_tree(proc) -> None:
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _which(*names: str) -> str | None:
    local = Path(__file__).resolve().parent.parent / "bin"
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        candidate = local / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _natural_key(path: Path):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", str(path))
    ]


def find_audio(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS),
        key=_natural_key,
    )


_DISC_DIR = re.compile(r"^(?:disc|cd|disk)\s*\d+$", re.I)
_AUDIO_TAG_CACHE: dict[tuple[str, int, int], dict] = {}


def _first_tag(tags, *keys) -> str:
    if not tags:
        return ""
    for key in keys:
        try:
            value = tags.get(key)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if not isinstance(value, str):
            value = getattr(value, "text", value)
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _audio_tags(path: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return {}
    cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
    if cache_key in _AUDIO_TAG_CACHE:
        return dict(_AUDIO_TAG_CACHE[cache_key])
    if path.suffix.lower() not in {".flac", ".m4a", ".mp3"}:
        return {}

    try:
        import mutagen

        tagged = mutagen.File(str(path), easy=False)
    except Exception:
        tagged = None
    if tagged is None:
        return {}
    tags = getattr(tagged, "tags", None)
    values = {
        "album": _first_tag(tags, "album", "TALB", "\xa9alb"),
        "albumartist": _first_tag(
            tags, "albumartist", "album artist", "TPE2", "TXXX:albumartist",
            "TXXX:album artist", "TXXX:ALBUMARTIST", "aART",
        ),
        "artist": _first_tag(tags, "artist", "TPE1", "\xa9ART"),
        "title": _first_tag(tags, "title", "TIT2", "TXXX:title", "\xa9nam"),
        "date": _first_tag(
            tags, "date", "year", "originaldate", "TDRC", "TDOR", "TYER",
            "TXXX:date", "TXXX:year", "\xa9day",
        ),
        "tracknumber": _first_tag(
            tags, "tracknumber", "track", "TRCK", "TXXX:tracknumber", "trkn",
        ),
        "discnumber": _first_tag(
            tags, "discnumber", "disc", "TPOS", "TXXX:discnumber", "disk",
        ),
    }
    if not values["date"] and tags is not None:
        try:
            timestamp = tags.get("TDRL")
            values["date"] = str(timestamp.text[0]) if timestamp and timestamp.text else ""
        except (AttributeError, IndexError, TypeError):
            pass
    if len(_AUDIO_TAG_CACHE) >= 2048:
        _AUDIO_TAG_CACHE.clear()
    _AUDIO_TAG_CACHE[cache_key] = values
    return dict(values)


def _tag_number(value: str) -> str:
    return value.split("/", 1)[0].strip()


def _tag_year(value: str) -> str:
    match = re.search(r"(?:19|20)\d{2}", value or "")
    return match.group() if match else ""


def _release_tag_metadata(audio: list[Path]) -> tuple[str, str, str, str]:
    from collections import Counter

    tagged = [_audio_tags(path) for path in audio]

    def common(key: str, *fallback_keys: str) -> str:
        for field in (key, *fallback_keys):
            names = [tags[field].strip() for tags in tagged if tags.get(field, "").strip()]
            if not names:
                continue
            counts = Counter(name.casefold() for name in names)
            winner, count = counts.most_common(1)[0]
            if count == len(audio):
                return next(name for name in names if name.casefold() == winner)
        return ""

    artist = common("albumartist", "artist")
    album = common("album")
    date = common("date")
    return artist, album, _tag_year(date), date


def discover_release(workdir: Path) -> tuple[list[Path], Path | None, str | None]:
    audio = find_audio(workdir)
    if not audio:
        return [], None, None

    release_dir = max((p.parent for p in audio), key=lambda d: len(d.parts))
    counts: dict[Path, int] = {}
    for path in audio:
        counts[path.parent] = counts.get(path.parent, 0) + 1
    busiest = max(counts, key=lambda d: (counts[d], len(d.parts)))
    if counts[busiest] > 1:
        release_dir = busiest

    if _DISC_DIR.fullmatch(release_dir.name):
        parents = {p.parent.parent for p in audio if _DISC_DIR.fullmatch(p.parent.name)}
        if len(parents) == 1 and all(
            _DISC_DIR.fullmatch(p.parent.name) or p.parent in parents for p in audio
        ):
            release_dir = parents.pop()

    cover_path = None
    for name in ("cover.jpg", "cover.jpeg", "folder.jpg", "Cover.jpg", "cover.png"):
        probe = release_dir / name
        if probe.exists():
            cover_path = str(probe)
            break
    if cover_path is None:
        for candidate in sorted(workdir.rglob("cover.*")):
            if candidate.is_file():
                cover_path = str(candidate)
                break
        else:
            for candidate in sorted(workdir.rglob("folder.*")):
                if candidate.is_file() and candidate.suffix.lower() in (".jpg", ".jpeg", ".png"):
                    cover_path = str(candidate)
                    break

    return audio, release_dir, cover_path


def read_cover(cover_path: str | None, max_bytes: int = 5 * 1024 * 1024) -> bytes | None:
    if not cover_path:
        return None
    try:
        path = Path(cover_path)
        if not path.exists():
            return None
        if path.suffix.lower() in (".jpg", ".jpeg"):
            return path.read_bytes()
        return _reencode_cover(path, max_bytes)
    except Exception as e:
        print(f"[engine] could not read cover {cover_path}: {e}")
        return None


def _reencode_cover(path: Path, max_bytes: int) -> bytes | None:
    try:
        from io import BytesIO
        from PIL import Image

        img = Image.open(path).convert("RGB")
        img.thumbnail((1400, 1400))
        for quality in (90, 82, 72, 60):
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            if buf.tell() <= max_bytes:
                return buf.getvalue()
        return None
    except Exception as e:
        print(f"[engine] cover re-encode failed: {e}")
        return None


_YEAR_IN_NAME = re.compile(r"[\[(](\d{4})[\])]")
_BRACKET_TAG = re.compile(r"\s*\[(?:FLAC|MP3|ALAC|AAC|OGG|OPUS|WAV|\d+ ?bit)[^\]]*\]", re.I)
_TRAILING_YEAR = re.compile(r"\s*[\(\[·-]\s*\d{4}\s*[\)\]]?\s*$")
_TRACK_NUM = re.compile(
    r"^\s*(?:(?:disc\s+)?(\d{1,3})\s*-\s*)?(\d{1,3})\s*[._-]\s*", re.I
)


def _track_prefix(stem: str) -> tuple[int | None, str | None, str]:
    match = _TRACK_NUM.match(stem)
    if not match:
        return None, None, stem
    disc = int(match.group(1)) if match.group(1) else None
    return disc, match.group(2), stem[match.end() :]


def _split_track_rest(number: str | None, rest: str, stem: str) -> tuple[str | None, str, str]:
    parts = [p.strip() for p in rest.split(" - ") if p.strip()]
    if len(parts) >= 2:
        return number, parts[0], " - ".join(parts[1:])
    if parts:
        return number, "", parts[0]
    return number, "", stem


def _split_track_stem(stem: str) -> tuple[str | None, str, str]:
    _, number, rest = _track_prefix(stem)
    return _split_track_rest(number, rest, stem)


def _artist_from_filenames(audio: list[Path]) -> str:
    from collections import Counter

    names = [artist for _, artist, _ in (_split_track_stem(p.stem) for p in audio) if artist]
    if not names:
        return ""
    return Counter(names).most_common(1)[0][0]


def infer_release_from_paths(
    audio: list[Path],
    fallback_artist: str = "",
    workdir: Path | None = None,
) -> tuple[str, str, str]:
    if not audio:
        return fallback_artist or "Unknown Artist", "Unknown Album", ""

    release_dir = audio[0].parent
    if _DISC_DIR.fullmatch(release_dir.name):
        release_dir = release_dir.parent
    if workdir is not None:
        workdir = Path(workdir)
        artist_dir_real = workdir in release_dir.parent.parents
        album_dir_real = workdir in release_dir.parents
    else:
        artist_dir_real = album_dir_real = False

    album = (release_dir.name or "") if album_dir_real else ""
    artist = release_dir.parent.name if artist_dir_real else ""
    if not artist:
        artist = _artist_from_filenames(audio)

    year_match = _YEAR_IN_NAME.search(album)
    year = year_match.group(1) if year_match else ""

    album = _BRACKET_TAG.sub("", album)
    album = _TRAILING_YEAR.sub("", album).strip(" -·")
    artist = _BRACKET_TAG.sub("", artist).strip(" -·")

    artist = artist or fallback_artist
    if artist and album.lower().startswith(artist.lower() + " -"):
        album = album[len(artist) + 1 :].strip(" -·")

    tag_artist, tag_album, tag_year, _ = _release_tag_metadata(audio)
    return (
        tag_artist or artist or "Unknown Artist",
        tag_album or album or "Unknown Album",
        tag_year or year,
    )


def _track_number_from_name(name: str) -> int | None:
    match = _TRACK_NUM.match(name)
    if match:
        return int(match.group(2))
    leading = re.match(r"^\s*(\d{1,3})\b", name)
    return int(leading.group(1)) if leading else None


def tracks_from_paths(audio: list[Path], release_dir: Path, artist_fallback: str = "") -> list:
    parsed = []
    for index, path in enumerate(audio, start=1):
        tags = _audio_tags(path)
        disc, number, rest = _track_prefix(path.stem)
        tag_disc = _tag_number(tags.get("discnumber", ""))
        if tag_disc.isdigit():
            disc = int(tag_disc)
        if disc is None and _DISC_DIR.fullmatch(path.parent.name):
            disc = int(re.search(r"\d+", path.parent.name).group())
        number, artist_name, title = _split_track_rest(number, rest, path.stem)
        tag_number = _tag_number(tags.get("tracknumber", ""))
        number = tag_number or number
        artist_name = tags.get("artist") or tags.get("albumartist") or artist_name
        title = tags.get("title") or title
        parsed.append([index, disc, number, artist_name, title, path])

    multi_disc = len({disc for _, disc, *_ in parsed if disc is not None}) > 1
    entries = []
    for index, disc, number, artist_name, title, path in parsed:
        number = str(index) if multi_disc else (
            number or _track_number_from_name(path.name) or index
        )
        artist_name = artist_name or artist_fallback or "Unknown Artist"

        entries.append(
            {
                "song_path": str(path),
                "download_dir": str(release_dir),
                "song_name": title,
                "artist_name": artist_name,
                "file_extension": path.suffix.lower(),
                "TRACK_NUMBER": str(number),
                "SNG_ID": "",
            }
        )
    return entries


_DIRECT_ENGINES = pcfg.DIRECT_ENGINES


def _source_label(platform: str, mode: str, provider: str) -> str:
    label = pcfg.PLATFORM_LABELS.get(platform, platform)
    if provider == "deezer-arl-fallback":
        return f"Deezer ARL (Fallback from {label})"
    if platform == pcfg.DEEZER:
        return "Deezer (Direct ARL)"
    return f"{label} (Direct · {provider or _DIRECT_ENGINES.get(platform, mode)})"


def _probe_audio(path: str) -> dict:
    import json
    import subprocess

    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,bits_per_raw_sample,bits_per_sample,bit_rate",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
        streams = json.loads(completed.stdout or "{}").get("streams", [])
        return streams[0] if streams else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def _quality_label(tracks: list) -> str:
    extensions = sorted({t.get("file_extension", "").lower().lstrip(".") for t in tracks})
    extensions = [ext for ext in extensions if ext]
    codec_label = " / ".join(ext.upper() for ext in extensions) or "Audio"
    if not tracks:
        return codec_label

    probed = [_probe_audio(t.get("song_path", "")) for t in tracks]
    rates = {int(p["sample_rate"]) for p in probed if str(p.get("sample_rate", "")).isdigit()}
    depths = {
        int(p.get("bits_per_raw_sample") or p.get("bits_per_sample") or 0)
        for p in probed
    }
    depths.discard(0)
    bitrates = {int(p["bit_rate"]) for p in probed if str(p.get("bit_rate", "")).isdigit()}
    details = []
    if depths and extensions == ["flac"]:
        details.append("/".join(str(depth) for depth in sorted(depths)) + "-bit")
    if rates and extensions in (["flac"], ["wav"], ["aiff"]):
        rate = min(rates) if len(rates) == 1 else max(rates)
        details.append(f"{rate / 1000:g} kHz")
    elif bitrates and extensions in (["mp3"], ["m4a"], ["aac"], ["ogg"], ["opus"]):
        details.append(f"{round(max(bitrates) / 1000)} kbps")
    return f"{codec_label} {'/'.join(details)}" if details else codec_label


def _result_from_workdir(
    platform: str, mode: str, provider: str, url: str, kind: str, workdir: Path
) -> EngineResult:
    audio, release_dir, cover_path = discover_release(workdir)
    if not audio or release_dir is None:
        raise EngineError(f"{provider} produced no audio files for {url}")

    artist, album, year = infer_release_from_paths(audio, workdir=workdir)
    tracks = tracks_from_paths(audio, release_dir, artist_fallback=artist)
    _, _, _, release_date = _release_tag_metadata(audio)
    canonical_url = url
    if platform == pcfg.QOBUZ:
        source = classify(url)
        if source:
            canonical_url = source.url
    metadata = ReleaseMetadata(
        artist=artist,
        title=tracks[0]["song_name"] if kind == KIND_TRACK and len(tracks) == 1 else album,
        album_title=album,
        year=year,
        release_date=release_date or year,
        kind=kind,
        album_link="" if kind == KIND_TRACK else canonical_url,
        track_link=canonical_url if kind == KIND_TRACK else "",
        cover_data=read_cover(cover_path),
        artists_list=[artist],
    )
    metadata.source_label = _source_label(platform, mode, provider)
    metadata.quality_label = _quality_label(tracks)
    return EngineResult(
        metadata=metadata,
        tracks=tracks,
        mode=mode,
        platform=platform,
        provider=provider,
        workdir=str(workdir),
    )


def discover_releases(workdir: Path) -> list[tuple[Path, list[Path]]]:
    audio = find_audio(workdir)
    if not audio:
        return []

    groups: dict[Path, list[Path]] = {}
    for path in audio:
        groups.setdefault(path.parent, []).append(path)

    return [
        (release_dir, sorted(paths, key=_natural_key))
        for release_dir, paths in sorted(groups.items(), key=lambda kv: len(kv[0].parts))
    ]


def _matching_deezer_url(candidate: str, kind: str) -> str:
    link = classify(candidate or "")
    if (
        link
        and link.platform == pcfg.DEEZER
        and ((kind == KIND_TRACK and link.is_single_track) or link.kind == kind)
    ):
        return link.url
    return ""


async def _search_deezer_url(url: str, kind: str) -> str:
    source = classify(url)
    if source is None:
        return ""
    original_url = source.original_url or source.url
    info = await metadata_bridge.release_info(
        source.platform,
        KIND_TRACK if kind == KIND_TRACK else source.kind,
        source.object_id,
        track_id=source.track_id,
        storefront=source.storefront,
        url=source.original_url or source.url,
    )
    for deezer_url in (
        info.siblings.get("deezer", "") if info else "",
        (info.track_link if kind == KIND_TRACK else info.album_link) if info else "",
    ):
        matched = _matching_deezer_url(deezer_url, kind)
        if matched:
            return matched

    if info and info.isrcs:
        matched = await metadata_bridge.deezer_isrc_match(info.isrcs[0])
        if matched and matched.get("id"):
            return f"https://www.deezer.com/track/{matched['id']}"

    query = metadata_bridge._search_metadata_hint(info, kind, original_url)
    if not query:
        return ""
    payload = await metadata_bridge._fetch_json(
        f"https://api.deezer.com/search/{kind}", params={"q": query, "limit": 5}
    )
    for item in (payload or {}).get("data", []):
        item_id = item.get("id")
        if item_id:
            return f"https://www.deezer.com/{kind}/{item_id}"
    return ""


async def _deezer_match(url: str, kind: str) -> tuple[str, object | None]:
    data = await metadata_bridge.odesli_links(url)
    if data:
        for deezer_url in (
            data.get("matched_urls", {}).get("deezer", ""),
            data.get("platforms", {}).get("deezer", ""),
        ):
            matched = _matching_deezer_url(deezer_url, kind)
            if matched:
                return matched, None
    return await _search_deezer_url(url, kind), None


def _deezer_source_url(url: str) -> str:
    source = classify(url)
    return (source.original_url if source else "") or url


async def download_via_deezer_fallback(
    platform: str, url: str, kind: str, workdir: Path, progress_cb=None
) -> EngineResult:
    """Resolve a foreign-platform release and download it with the Deezer ARL."""
    if kind not in (KIND_TRACK, KIND_ALBUM):
        raise EngineError("Deezer fallback supports track and album links only")
    if not pcfg.fallback_enabled():
        raise EngineError("Cross-platform fallback is disabled (FALLBACK_ENABLED=0)")
    if not pcfg.has_deezer_credentials():
        raise EngineError("Deezer fallback requires a configured DEEZER_TOKEN")
    if progress_cb:
        await progress_cb("⬇️ Downloading from Deezer via Direct ARL...")

    from dl_utils import deezer_download as dz
    from dl_utils.deezer_utils import clean_filename

    deezer_url, _ = await _deezer_match(_deezer_source_url(url), kind)
    matched = _matching_deezer_url(deezer_url, kind)
    if not matched:
        raise EngineError(f"could not find a Deezer {kind} matching {url}")
    object_id = matched.rstrip("/").rsplit("/", 1)[-1]

    try:
        if dz.session is None:
            await asyncio.to_thread(
                dz.init_deezer_session,
                get_env_or_token_file("DEEZER_PROXY", "").strip(),
                "flac" if pcfg.flac_enabled() else "mp3",
            )
        infos = await asyncio.to_thread(
            dz.get_song_infos_from_deezer_website, kind, object_id
        )
        songs = [infos] if isinstance(infos, dict) else list(infos or [])
        if not songs:
            raise EngineError(f"Deezer returned no tracks for {matched}")

        workdir.mkdir(parents=True, exist_ok=True)
        for index, song in enumerate(songs, start=1):
            extension, audio_format = dz.get_file_format(song)
            song["format_fallback"] = bool(pcfg.flac_enabled() and extension.lower() != ".flac")
            artist = clean_filename(dz.get_artists(song) or "Unknown Artist")
            title = clean_filename(song.get("SNG_TITLE") or f"Track {index}")
            try:
                number = int(song.get("TRACK_NUMBER") or index)
            except (TypeError, ValueError):
                number = index
            filename = f"{number:02d} - {artist} - {title}{extension}"
            output = workdir / filename
            if progress_cb:
                await progress_cb(
                    f"⬇️ Downloading track {index}/{len(songs)}: {artist} - {title}..."
                )
                if song["format_fallback"]:
                    await progress_cb(
                        f"⚠️ Fallback: Track {index:02d} - {artist} - {title} was not available "
                        "as lossless quality on Deezer (using MP3)"
                    )
            try:
                await asyncio.to_thread(dz.download_song, song, audio_format, str(output))
                if not output.is_file() or output.stat().st_size == 0:
                    raise EngineError(f"Deezer produced an empty file for track {song.get('SNG_ID', index)}")
                song["song_path"] = str(output)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                song["download_failed"] = True
                if progress_cb:
                    await progress_cb(
                        f"❌ Failure: Track {index:02d} - {artist} - {title} was not available"
                    )
                print(f"[engine] Deezer track {song.get('SNG_ID', index)} failed: {error}")
    except EngineError:
        raise
    except Exception as e:
        raise EngineError(f"Deezer ARL fallback failed: {e}") from e

    result = _result_from_workdir(
        platform, "direct", "deezer-arl-fallback", url, kind, workdir
    )
    result.metadata.tracks_api_data = songs
    downloaded_songs = [song for song in songs if not song.get("download_failed")]
    for index, track in enumerate(result.tracks):
        source = downloaded_songs[index] if index < len(downloaded_songs) else None
        if source:
            track["SNG_ID"] = source.get("SNG_ID")
            track["format_fallback"] = bool(source.get("format_fallback"))
    info = await (
        metadata_bridge.deezer_track(object_id)
        if kind == KIND_TRACK
        else metadata_bridge.deezer_album(object_id)
    )
    if info:
        if info.cover_data is None and info.cover_url:
            info.cover_data = await metadata_bridge.fetch_bytes(info.cover_url)
        result.metadata.artist = info.artist or result.metadata.artist
        result.metadata.title = info.title or info.album_title or result.metadata.title
        result.metadata.album_title = info.album_title or result.metadata.album_title
        result.metadata.year = info.year or result.metadata.year
        result.metadata.release_date = info.release_date or result.metadata.release_date
        result.metadata.album_link = info.album_link or result.metadata.album_link
        result.metadata.track_link = info.track_link or result.metadata.track_link
        result.metadata.cover_data = info.cover_data or result.metadata.cover_data
        result.metadata.artists_list = [result.metadata.artist]
    return result


def _streamrip_config(platform: str, workdir: Path) -> Path:
    try:
        from streamrip.config import set_user_defaults
    except ImportError as e:
        raise EngineError("streamrip is not installed (pip install streamrip)") from e

    try:
        from tomlkit import parse as toml_parse
    except ImportError as e:
        raise EngineError("streamrip requires tomlkit") from e

    config_path = workdir / "streamrip_config.toml"
    set_user_defaults(str(config_path))
    # Tighten before any credential is written, not after: the file is created
    # from the process umask (0644 by default) and workdir sits under TMP_DIR,
    # so a post-write chmod would leave a window where the Qobuz auth token and
    # the Tidal refresh token are world-readable. The rewrite below truncates
    # in place and inherits this mode.
    os.chmod(config_path, 0o600)

    with open(config_path, "r", encoding="utf-8") as f:
        doc = toml_parse(f.read())

    flac = pcfg.flac_enabled()

    downloads = doc["downloads"]
    downloads["folder"] = str(workdir)
    downloads["source_subdirectories"] = False
    downloads["disc_subdirectories"] = False
    downloads["concurrency"] = True
    downloads["max_connections"] = pcfg.streamrip_max_connections()
    downloads["requests_per_minute"] = 30
    downloads["verify_ssl"] = True

    doc["database"]["downloads_enabled"] = False
    doc["database"]["failed_downloads_enabled"] = False
    doc["cli"]["progress_bars"] = False
    doc["cli"]["text_output"] = True
    doc["misc"]["check_for_updates"] = False
    doc["artwork"]["save_artwork"] = True
    doc["artwork"]["embed"] = True
    doc["artwork"]["embed_size"] = "large"
    doc["filepaths"]["add_singles_to_folder"] = False
    doc["filepaths"]["restrict_characters"] = False
    doc["filepaths"]["folder_format"] = "{albumartist} - {title} ({year})"
    doc["filepaths"]["track_format"] = "{tracknumber:02d}. {artist} - {title}"

    if platform == pcfg.QOBUZ:
        _configure_qobuz(doc, flac)
    elif platform == pcfg.TIDAL:
        _configure_tidal(doc, flac)
    else:
        raise EngineError(f"streamrip has no engine for {platform!r}")

    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(doc.as_string())
    return config_path


def _configure_qobuz(doc, flac: bool) -> None:
    section = doc["qobuz"]
    section["quality"] = 3 if flac else 1
    section["download_booklets"] = False

    fields = pcfg.qobuz_fields()
    userid = str(fields.get("email_or_userid") or "")
    token = str(fields.get("password_or_token") or "")
    email = userid if "@" in userid else ""
    password = token
    app_id = str(fields.get("app_id") or "")
    secrets = fields.get("secrets") or []

    use_auth_token = fields.get("use_auth_token", True)
    if isinstance(use_auth_token, str):
        use_auth_token = use_auth_token.strip().lower() in ("1", "true", "yes", "on")

    if userid and token and use_auth_token:
        section["use_auth_token"] = True
        section["email_or_userid"] = userid
        section["password_or_token"] = token
    elif (email and password) or (userid and token and not use_auth_token):
        login_user = email or userid
        login_pass = password or token
        section["use_auth_token"] = False
        section["email_or_userid"] = login_user
        import hashlib

        section["password_or_token"] = (
            login_pass
            if re.fullmatch(r"[0-9a-f]{32}", login_pass)
            else hashlib.md5(login_pass.encode()).hexdigest()
        )
    else:
        raise EngineError(
            "QOBUZ: set QOBUZ_USER_ID + QOBUZ_USER_AUTH_TOKEN (or "
            "QOBUZ_EMAIL_OR_USER_ID + QOBUZ_PASSWORD_OR_TOKEN)"
        )

    if app_id:
        section["app_id"] = app_id
    if secrets:
        section["secrets"] = [str(s).strip() for s in secrets if str(s).strip()]


def _configure_tidal(doc, flac: bool) -> None:
    section = doc["tidal"]
    section["quality"] = 3 if flac else 1
    section["download_videos"] = False

    if not pcfg.has_tidal_credentials():
        raise EngineError(
            "TIDAL: set TIDAL_USER_ID + TIDAL_ACCESS_TOKEN + TIDAL_REFRESH_TOKEN "
            "or TIDAL_OAUTH_TOKEN."
        )
    fields = pcfg.tidal_oauth_fields()
    section["token_type"] = "Bearer"
    values = {
        "user_id": str(fields["user_id"]),
        "access_token": str(fields["access_token"]),
        "refresh_token": str(fields["refresh_token"]),
        "country_code": str(fields.get("country_code") or "US"),
        "token_type": "Bearer",
    }
    real_expiry = str(fields.get("token_expiry") or "")
    claims = pcfg.decode_tidal_jwt_claims(str(fields["access_token"]))
    try:
        token_exp = float(claims.get("token_expiry") or real_expiry or 0)
    except (TypeError, ValueError):
        token_exp = 0
    values["token_expiry"] = str(int(token_exp)) if token_exp > 0 else real_expiry
    for key, value in values.items():
        section[key] = value


def _sync_refreshed_tidal_tokens(config_path: Path, original: dict) -> None:
    """Persist a token refreshed by Streamrip without failing a good download."""
    if not config_path.is_file():
        return
    try:
        from tomlkit import parse
        from dl_utils import credential_migrator as cm

        doc = parse(config_path.read_text(encoding="utf-8"))
        refreshed = dict(doc.get("tidal", {}))
        access = str(refreshed.get("access_token") or "")
        if not access:
            return
        keys = ("access_token", "refresh_token", "token_expiry", "country_code")
        if all(str(refreshed.get(key) or "") == str(original.get(key) or "") for key in keys):
            return
        fields = {**original, **refreshed}
        if pcfg._credential_file_ready("TIDAL_CONFIG_PATH"):
            path = pcfg._credential_file_path("TIDAL_CONFIG_PATH")
            cm.write_tidal_credentials(fields, path)
        else:
            updates = {
                "TIDAL_ACCESS_TOKEN": access,
                "TIDAL_REFRESH_TOKEN": str(refreshed.get("refresh_token") or original.get("refresh_token") or ""),
                "TIDAL_TOKEN_EXPIRY": str(refreshed.get("token_expiry") or ""),
            }
            utils.update_token_env_keys(updates)
    except Exception as error:
        print(f"[engine] could not persist refreshed Tidal token: {error}")


def _streamrip_error_detail(tail: str) -> str:
    lines = [
        re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).strip(" │\t")
        for line in tail.splitlines()
        if line.strip(" │\t")
    ]
    return "\n".join(lines[-3:])[-1000:] or "no error output"


def _tidal_failure(rc: int, tail: str) -> EngineError:
    claims = pcfg.decode_tidal_jwt_claims(
        str(pcfg.tidal_oauth_fields().get("access_token") or "")
    )
    detail = _streamrip_error_detail(tail)
    if str(claims.get("client_id") or "") == "3235":
        return EngineError(
            "Tidal token was issued for the Web Player (cid=3235), which Tidal blocks "
            "from direct stream manifest downloads. Run /authenticatetidal in Telegram "
            f"to link a TV device token via link.tidal.com.\n{detail}"
        )
    lowered = tail.casefold()
    prefix = "streamrip"
    if any(marker in lowered for marker in ("401", "403", "unauthorized", "forbidden")):
        prefix += (
            ": Tidal API returned HTTP 401/403 Unauthorized. Run /authenticatetidal "
            "in Telegram to refresh your Tidal TV device login"
        )
    else:
        prefix += f" exit {rc}"
    return EngineError(f"{prefix}:\n{detail}")



def _streamrip_command(config_path: Path, url: str) -> list[str]:
    arguments = [
        "--config-path", str(config_path),
        "--max-connections", str(pcfg.streamrip_max_connections()),
        "--no-progress", "url", url,
    ]
    if importlib.util.find_spec("streamrip") is not None:
        return [sys.executable, "-m", "dl_utils.streamrip_runner", *arguments]
    rip_bin = _which("rip")
    if rip_bin:
        try:
            shebang = Path(rip_bin).read_text(encoding="utf-8").splitlines()[0]
            interpreter = shebang[2:].strip() if shebang.startswith("#!") else ""
            if interpreter and Path(interpreter).is_file():
                return [interpreter, "-m", "dl_utils.streamrip_runner", *arguments]
        except (OSError, IndexError):
            pass
    return [sys.executable, "-m", "dl_utils.streamrip_runner", *arguments]


async def download_via_streamrip(platform: str, url: str, kind: str, workdir: Path, progress_cb=None) -> EngineResult:
    config_path = _streamrip_config(platform, workdir)
    cmd = _streamrip_command(config_path, url)

    original_tidal_fields = pcfg.tidal_oauth_fields() if platform == pcfg.TIDAL else {}
    rc, tail = await _invoke_subprocess(_run_subprocess, cmd, workdir, progress_cb)
    if platform == pcfg.TIDAL:
        _sync_refreshed_tidal_tokens(config_path, original_tidal_fields)
    audio, release_dir, _ = discover_release(workdir)

    if rc != 0 or not audio:
        print(f"[streamrip] {platform} rc={rc} output:\n{tail}")
    if not audio:
        if platform == pcfg.TIDAL:
            raise _tidal_failure(rc, tail)
        raise EngineError(
            f"streamrip exit {rc} produced no audio for {url}:\n"
            f"{_streamrip_error_detail(tail)}"
        )
    if rc != 0:
        print(f"[engine] streamrip partial success (rc={rc}): {tail[-300:]}")
    if platform == pcfg.TIDAL and pcfg.flac_enabled():
        for path in audio:
            try:
                from mutagen.flac import FLAC

                if path.suffix.lower() != ".flac" or path.read_bytes()[:4] != b"fLaC":
                    raise EngineError(f"Tidal returned a non-FLAC file: {path.name}")
                if FLAC(path).info.bits_per_sample < 16:
                    raise EngineError(f"Tidal returned invalid lossless audio: {path.name}")
            except EngineError:
                raise
            except Exception as error:
                raise EngineError(f"Tidal FLAC validation failed for {path.name}: {error}") from error

    return _result_from_workdir(
        platform, "direct", "streamrip", url, kind, workdir
    )


async def _spotify_release_metadata(url: str, kind: str) -> dict:
    from dl_utils import dotify_shim

    object_id = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    return await asyncio.to_thread(
        dotify_shim.album_metadata if kind == KIND_ALBUM else dotify_shim.track_metadata,
        object_id,
    )


def _spotify_expected(metadata: dict, kind: str) -> list[dict]:
    from dl_utils import dotify_shim

    if kind == KIND_TRACK:
        metadata = {"tracks": {"items": [metadata]}}
    return dotify_shim.expected_tracks(metadata)


def _dotify_audio_quality() -> str:
    quality = get_env_or_token_file("SPOTIFY_AUDIO_QUALITY", "").strip()
    return quality or "aac-high,aac-medium"


def _dotify_aac_fallback(quality: str) -> str | None:
    tiers = [tier.strip() for tier in quality.split(",") if tier.strip()]
    if not any(tier in {"flac-mp4-24", "flac-mp4"} for tier in tiers):
        return None
    fallback = [tier for tier in tiers if tier in {"aac-high", "aac-medium"}]
    return ",".join(fallback) if fallback else None


def _dotify_widevine_denied(output: str) -> bool:
    output = output.lower()
    return "widevine" in output and any(
        marker in output for marker in ("403", "access denied", "api_request_failed")
    )


def _dotify_rate_limited(output: str) -> bool:
    output = output.lower()
    return any(
        marker in output
        for marker in ("status code 429", "rate-limited", "too many requests")
    )


def _dotify_command(
    binary: str,
    cookies: Path,
    wvd: Path,
    workdir: Path,
    url: str,
    quality: str | None = None,
) -> list:
    quality = quality or _dotify_audio_quality()
    return [
        binary, "download", "--no-config-file", "--skip-preflight", "--no-tui",
        "--no-resume", "--no-exceptions", "--session-type", "web",
        "--cookies-path", str(cookies), "--wvd-path", str(wvd),
        "--audio-quality", quality, "--allow-quality-fallback",
        "--audio-remux-mode", "ffmpeg", "--output", str(workdir),
        "--temp", str(workdir / "dotify-temp"),
        "--album-folder-template", "{album_artist} - {album}",
        "--compilation-folder-template", "{album_artist} - {album}",
        "--single-disc-file-template", "{track:02d} - {artist} - {title}",
        "--multi-disc-file-template", "{disc}-{track:02d} - {artist} - {title}",
        "--no-synced-lyrics-file", "--save-cover-file", "--wait-interval", "0",
        "--widevine-retries", "2", "--widevine-max-wait", "0", url,
    ]


def _artist_from_audio_tags(tracks: list) -> str:
    import mutagen

    for track in tracks:
        try:
            audio = mutagen.File(str(track.get("song_path") or ""))
            tags = getattr(audio, "tags", None) or {}
            for key in ("©ART", "aART", "artist", "ARTIST"):
                value = tags.get(key)
                if isinstance(value, (list, tuple)):
                    value = value[0] if value else ""
                artist = str(value or "").strip()
                if artist and artist.casefold() != "unknown artist":
                    return artist
        except Exception:
            continue
    return ""


def _clean_dotify_attempt(workdir: Path, before: set[Path]) -> None:
    for path in sorted(
        (item for item in workdir.rglob("*") if item.is_file() and item not in before),
        reverse=True,
    ):
        path.unlink(missing_ok=True)
    for directory in sorted(
        (item for item in workdir.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass


async def download_via_dotify(url: str, kind: str, workdir: Path, progress_cb=None) -> EngineResult:
    from dl_utils import dotify_shim

    dotify_bin = _which("dotify")
    if not dotify_bin:
        raise EngineError("dotify-cli is not installed")
    if not pcfg.has_spotify_credentials():
        raise EngineError(pcfg.spotify_credentials_error())

    wvd = pcfg._credential_file_path("SPOTIFY_WVD_PATH") or Path("")
    try:
        cookies = dotify_shim.stage_cookies(workdir)
    except ValueError as error:
        raise EngineError(str(error)) from error

    release_metadata = await _spotify_release_metadata(url, kind)
    expected = _spotify_expected(release_metadata, kind)
    playable = [track for track in expected if track.get("is_playable", True)]
    for track in expected:
        if not track.get("is_playable", True):
            print(
                f"[engine] Spotify track {track['TRACK_NUMBER']} unavailable: "
                f"{track['artist_name']} - {track['SNG_TITLE']}"
            )
    if not playable:
        raise EngineError("SPOTIFY: no playable tracks are available for this release")

    failures = []
    quality = _dotify_audio_quality()
    for index, track in enumerate(playable):
        if index > 0:
            wait_seconds = utils.get_float_env("SPOTIFY_BULK_WAIT_SECONDS", 4.0)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
        track_url = f"https://open.spotify.com/track/{track['SNG_ID']}"
        before = set(find_audio(workdir))
        cmd = _dotify_command(
            dotify_bin, cookies, wvd, workdir, track_url, quality=quality
        )
        attempt_files = set()
        for attempt in range(3):
            attempt_before = {path for path in workdir.rglob("*") if path.is_file()}
            rc, tail = await _invoke_subprocess(_run_subprocess, cmd, workdir, progress_cb)
            current_audio = set(find_audio(workdir))
            attempt_files = current_audio - attempt_before
            if rc == 0 and attempt_files:
                break
            if attempt == 2 or not _dotify_transient_failure(tail):
                break
            _clean_dotify_attempt(workdir, attempt_before)
            delay = 1.5 * (2 ** attempt)
            print(
                f"[engine] transient dotify-cli network failure; "
                f"retrying track {track['TRACK_NUMBER']} in {delay:g}s"
            )
            await asyncio.sleep(delay)
        produced_audio = rc == 0 and bool(attempt_files)
        if not produced_audio:
            _clean_dotify_attempt(workdir, attempt_before)
        aac_quality = _dotify_aac_fallback(quality)
        if (
            (rc != 0 or not produced_audio)
            and aac_quality
            and _dotify_widevine_denied(tail)
        ):
            quality = aac_quality
            print(
                f"[engine] Spotify Widevine denied lossless audio for track "
                f"{track['TRACK_NUMBER']}; retrying with {quality}"
            )
            await asyncio.sleep(3.0)
            cmd = _dotify_command(
                dotify_bin, cookies, wvd, workdir, track_url, quality=quality
            )
            rc, tail = await _invoke_subprocess(_run_subprocess, cmd, workdir, progress_cb)
            produced_audio = set(find_audio(workdir)) != before
        if (rc != 0 or not produced_audio) and _dotify_rate_limited(tail):
            await _report(
                progress_cb,
                "⚠️ Spotify rate-limited the request. Waiting 62s before retrying...",
            )
            await asyncio.sleep(SPOTIFY_RATE_LIMIT_COOLDOWN_SECONDS)
            cmd = _dotify_command(
                dotify_bin, cookies, wvd, workdir, track_url, quality=quality
            )
            rc, tail = await _invoke_subprocess(_run_subprocess, cmd, workdir, progress_cb)
            produced_audio = set(find_audio(workdir)) != before
        if rc != 0 or not produced_audio:
            detail = _strip_ansi(tail)[-300:] if tail else "no audio was produced"
            failures.append(f"Track {track['TRACK_NUMBER']}: {detail}")
            print(f"[engine] Spotify track failed; continuing album: {failures[-1]}")

    audio, _, _ = discover_release(workdir)
    if not audio:
        detail = failures[-1] if failures else "no audio was produced"
        raise EngineError(f"SPOTIFY: dotify-cli could not download any tracks; {detail}")

    result = _result_from_workdir(
        pcfg.SPOTIFY, "direct", "dotify-cli", url, kind, workdir
    )
    result.metadata.tracks_api_data = expected
    album = release_metadata if kind == KIND_ALBUM else release_metadata.get("album") or {}
    result.metadata.artist = (album.get("artists") or [{}])[0].get(
        "name", result.metadata.artist
    )
    result.metadata.album_title = album.get("name") or result.metadata.album_title
    result.metadata.title = (
        release_metadata.get("name") or result.metadata.tracks[0]["song_name"]
        if kind == KIND_TRACK
        else result.metadata.album_title
    )
    result.metadata.album_link = (
        f"https://open.spotify.com/album/{album['id']}" if album.get("id") else ""
    ) if kind == KIND_TRACK else result.metadata.album_link
    result.metadata.release_date = album.get("release_date") or result.metadata.release_date
    result.metadata.year = result.metadata.release_date[:4]
    if str(result.metadata.artist or "").strip().casefold() in ("", "unknown artist"):
        artist = next(
            (
                str(track.get("artist_name") or "").strip()
                for track in result.tracks
                if str(track.get("artist_name") or "").strip()
                and str(track.get("artist_name") or "").strip().casefold() != "unknown artist"
            ),
            "",
        )
        if not artist:
            artist = next(
                (
                    str(track.get("artist_name") or "").strip()
                    for track in expected
                    if str(track.get("artist_name") or "").strip()
                    and str(track.get("artist_name") or "").strip().casefold() != "unknown artist"
                ),
                "",
            )
        if not artist:
            artist = _artist_from_audio_tags(result.tracks)
        result.metadata.artist = artist or "Unknown Artist"
    result.metadata.artists_list = [result.metadata.artist]
    remaining = list(expected)
    multi_disc = len({item["DISC_NUMBER"] for item in expected}) > 1
    for index, track in enumerate(result.tracks):
        disc, number, _ = _track_prefix(Path(track["song_path"]).name)
        source = next(
            (
                item
                for item in remaining
                if item["TRACK_NUMBER"] == str(int(number or 0))
                and (disc is None or item["DISC_NUMBER"] == str(disc))
            ),
            None,
        )
        if source is None and kind == KIND_TRACK and len(expected) == 1:
            source = expected[0]
        if source:
            remaining.remove(source)
            track["SNG_ID"] = source["SNG_ID"]
            track["id"] = source["SNG_ID"]
        if multi_disc:
            track["TRACK_NUMBER"] = str(index + 1)
    return result


# Backward-compatible name; this alias never invokes the legacy audio protocol.
download_via_zotify = download_via_dotify


async def _apple_cookies_file(workdir: Path) -> str:
    # A ready-to-go path may be pre-filled even when only the media token is
    # available. Missing/unchanged example files must not mask that fallback.
    if pcfg._credential_file_ready("APPLE_COOKIES_PATH"):
        return str(pcfg._credential_file_path("APPLE_COOKIES_PATH"))

    token = get_env_or_token_file("APPLE_MEDIA_USER_TOKEN", "").strip()
    if not token:
        raise EngineError(
            "APPLE: set APPLE_COOKIES_PATH (Netscape cookies.txt) or APPLE_MEDIA_USER_TOKEN"
        )

    path = workdir / "cookies.txt"
    body = "\n".join(
        [
            "# Netscape HTTP Cookie File",
            "# generated from APPLE_MEDIA_USER_TOKEN",
            "",
            ".apple.com\tTRUE\t/\tTRUE\t0\tmedia-user-token\t" + token,
        ]
    )
    # Create with the final mode instead of writing then chmod'ing: write_text()
    # lands at the umask default (0644), leaving the media-user-token readable by
    # anything in the container for the window before chmod ran. The mode argument
    # to os.open is masked by umask, and 0o600 has no group/other bits to lose.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body + "\n")
    return str(path)


GAMDL_SONG_CODECS = frozenset(
    {
        "aac-web",
        "aac-he-web",
        "aac",
        "aac-he",
        "aac-binaural",
        "aac-downmix",
        "aac-he-binaural",
        "aac-he-downmix",
        "atmos",
        "ac3",
        "alac",
        "ask",
    }
)


def _gamdl_codec_priority() -> str | None:
    configured = get_env_or_token_file("APPLE_CODEC_PRIORITY", "").strip()
    if not configured:
        return "alac,aac-web" if pcfg.flac_enabled() else "aac-web"
    codecs = [part.strip().lower() for part in configured.split(",")]
    if not all(codecs) or any(codec not in GAMDL_SONG_CODECS for codec in codecs):
        raise EngineError(
            "APPLE_CODEC_PRIORITY must be a comma-separated list of gamdl 3.9.1 "
            f"SongCodec values: {', '.join(sorted(GAMDL_SONG_CODECS))}"
        )
    return ",".join(codecs)


def _gamdl_command(gamdl_bin: str, cookies_path: str, url: str, workdir: Path) -> list[str]:
    cmd = [
        gamdl_bin,
        "--cookies-path",
        cookies_path,
        "--output-path",
        str(workdir),
        "--temp-path",
        str(workdir),
        "--no-config-file",
        "--save-cover",
        "--cover-format",
        "jpg",
        "--no-synced-lyrics",
        "--no-album-folder-template",
        "{artist} - {album}",
        "--single-disc-file-template",
        "{track:02d} - {artist} - {title}",
        "--multi-disc-file-template",
        "{disc}-{track:02d} - {artist} - {title}",
        "--log-level",
        "WARNING",
        url,
    ]
    codec_priority = _gamdl_codec_priority()
    if codec_priority:
        cmd[1:1] = ["--song-codec-priority", codec_priority]
    return cmd


async def download_via_gamdl(url: str, kind: str, workdir: Path, progress_cb=None) -> EngineResult:
    gamdl_bin = _which("gamdl")
    if not gamdl_bin:
        raise EngineError("gamdl is not installed (pip install gamdl==3.9.1)")

    cookies_path = await _apple_cookies_file(workdir)
    cmd = _gamdl_command(gamdl_bin, cookies_path, url, workdir)
    rc, tail = await _invoke_subprocess(_run_subprocess, cmd, workdir, progress_cb)
    audio, _, _ = discover_release(workdir)
    if rc != 0 and not audio:
        raise EngineError(f"gamdl exit {rc}: {tail[-500:]}")
    return _result_from_workdir(pcfg.APPLE, "direct", "gamdl", url, kind, workdir)


async def download_via_amazon_cli(url: str, kind: str, workdir: Path, progress_cb=None) -> EngineResult:
    cli = get_env_or_token_file("AMAZON_CLI", "amazon-music-downloader").strip()
    binary = _which(cli)
    token = get_env_or_token_file("AMAZON_DEVICE_TOKEN", "").strip()
    wvd_path = pcfg._credential_file_path("AMAZON_WVD_PATH")
    wvd = str(wvd_path) if wvd_path else ""

    if not binary:
        raise EngineError(f"AMAZON: CLI {cli!r} not found on PATH")
    if not token:
        raise EngineError("AMAZON: AMAZON_DEVICE_TOKEN is not set")
    if not wvd or not Path(wvd).exists():
        raise EngineError(f"AMAZON: WVD file not found at {wvd or '(unset)'}")

    cmd = [
        binary,
        "--device-token",
        token,
        "--wvd-path",
        wvd,
        "--output-dir",
        str(workdir),
        url,
    ]
    rc, tail = await _invoke_subprocess(_run_subprocess, cmd, workdir, progress_cb)
    audio, _, _ = discover_release(workdir)
    if rc != 0 and not audio:
        raise EngineError(f"amazon cli exit {rc}: {tail[-500:]}")
    return _result_from_workdir(pcfg.AMAZON, "direct", cli, url, kind, workdir)


# FIXED: Bind `platform` via functools.partial so every entry in _TOKEN_ENGINES
# has the exact same `(url, kind, workdir)` 3-argument call signature!
_TOKEN_ENGINES = {
    pcfg.QOBUZ: functools.partial(download_via_streamrip, pcfg.QOBUZ),
    pcfg.TIDAL: functools.partial(download_via_streamrip, pcfg.TIDAL),
    pcfg.SPOTIFY: download_via_dotify,
    pcfg.APPLE: download_via_gamdl,
    pcfg.AMAZON: download_via_amazon_cli,
}


def _short_reason(error: Exception, limit: int = 160) -> str:
    text = str(error).strip() or error.__class__.__name__
    text = " ".join(text.splitlines())
    return text if len(text) <= limit else text[:limit] + "…"


async def _report(progress_cb, text: str) -> None:
    print(f"[progress] {text}")
    progress_cb = progress_cb or _progress_callback.get()
    if progress_cb:
        try:
            await progress_cb(text)
        except TypeError:
            await progress_cb(text, force=True)


async def _call_with_progress(callback, function, *args, **kwargs):
    token = _progress_callback.set(callback)
    try:
        return await function(*args, **kwargs)
    finally:
        _progress_callback.reset(token)


async def _invoke_subprocess(runner, cmd, cwd, progress_cb=None):
    if "progress_cb" in inspect.signature(runner).parameters:
        return await runner(cmd, cwd=cwd, progress_cb=progress_cb)
    # Third-party integrations and older tests can still provide a two-argument
    # runner; the real implementation receives the request-local callback.
    return await runner(cmd, cwd)


async def download_release(
    platform: str,
    url: str,
    kind: str,
    workdir: Path,
    progress_cb=None,
) -> EngineResult:
    label = pcfg.PLATFORM_LABELS.get(platform, platform)
    if not pcfg.has_credentials(platform):
        if platform == pcfg.SPOTIFY or not pcfg.fallback_enabled():
            raise EngineError(pcfg.missing_credentials_message(platform))
        await _report(
            progress_cb,
            f"⚠️ {label} credentials not configured. Falling back to Deezer ARL...",
        )
        return await download_via_deezer_fallback(
            platform, url, kind, workdir, progress_cb=progress_cb
        )

    engine = _TOKEN_ENGINES.get(platform)
    if engine is None:
        raise EngineError(f"{label} direct engine is not configured in this dispatcher")

    engine_name = _DIRECT_ENGINES.get(platform, "direct")
    await _report(progress_cb, f"⬇️ Downloading from {label} via {engine_name}...")
    try:
        return await _call_with_progress(progress_cb, engine, url, kind, workdir)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if platform == pcfg.SPOTIFY or not pcfg.fallback_enabled():
            raise
        print(f"[engine] {engine_name} failed for {platform}: {error}")
        await _report(
            progress_cb,
            f"⚠️ {label} direct download failed ({_short_reason(error)}). "
            "Falling back to Deezer ARL...",
        )
        _clear_partial(workdir)
        return await download_via_deezer_fallback(
            platform, url, kind, workdir, progress_cb=progress_cb
        )


def _clear_partial(workdir: Path) -> None:
    try:
        for item in workdir.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            elif (
                item.suffix.lower() in AUDIO_EXTENSIONS
                or item.name in ("cover.jpg", "spotify_credentials.json", "spotify_cookies.txt")
            ):
                item.unlink(missing_ok=True)
    except OSError as e:
        print(f"[engine] could not clear {workdir}: {e}")


async def download_artist_discography(
    platform: str,
    artist_url: str,
    artist_base_dir: Path,
    releases: list,
    progress_cb=None,
) -> list:
    downloaded = []
    delay = pcfg.artist_api_delay()

    for index, release in enumerate(releases, start=1):
        release_url = release.get("url")
        if not release_url:
            print(f"[engine] {platform}: release {release.get('id')} has no url, skipping")
            continue

        print(
            f"[engine] {platform} discography {index}/{len(releases)}: "
            f"{release.get('title')} ({release.get('id')})"
        )
        prefix = (
            f"⬇️ {release.get('artist') or platform} Discography "
            f"(Release {index}/{len(releases)} — {release.get('record_type', 'Album')}): "
            f"{release.get('artist') or ''} - {release.get('title') or release.get('id')} "
            f"[{release.get('year', '')}]"
        )
        await _report(progress_cb, prefix + "...")
        release_dir = artist_base_dir / f"rel{index}"
        try:
            if index > 1 and delay:
                await asyncio.sleep(delay)
            release_dir.mkdir(parents=True, exist_ok=True)
            async def release_progress(text, prefix=prefix):
                await _report(progress_cb, prefix + " " + text)

            kwargs = {"progress_cb": release_progress} if progress_cb else {}
            result = await _call_with_progress(
                release_progress if progress_cb else None,
                download_release,
                platform,
                release_url,
                KIND_ALBUM,
                release_dir,
                **kwargs,
            )
            if not result.tracks:
                print(f"[engine] {platform}: no tracks for {release_url}")
                cleanup_dir(release_dir)
                continue
            downloaded.append((release, result.metadata, result.tracks))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[engine] {platform}: release {release.get('title')} failed: {e}")
            traceback.print_exc()
            cleanup_dir(release_dir)

    if not downloaded:
        raise EngineError(
            f"no releases could be downloaded for {artist_url} after retries"
        )
    print(f"[engine] {platform}: {len(downloaded)}/{len(releases)} releases downloaded")
    return downloaded