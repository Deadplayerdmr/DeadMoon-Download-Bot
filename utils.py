import asyncio
import contextvars
import json
import os
import shutil
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Optional

# Anchored on __file__ so these resolve regardless of the process CWD.
PROJECT_ROOT = Path(__file__).resolve().parent
TOKEN_ENV_PATH = PROJECT_ROOT / "token.env"

LANGS_FILE = json.load(open(PROJECT_ROOT / "langs.json"))
LANG = os.environ.get("BOT_LANG")

TMP_DIR = str(PROJECT_ROOT / "tmp")


def read_token_env_value(key: str) -> str:
    """Read a value straight out of token.env, ignoring the environment.

    docker-compose's env_file injects variables at container *creation* time,
    so the environment can hold a stale value after /newarl rewrites the file.
    The file is the source of truth whenever it exists.
    """
    try:
        with open(TOKEN_ENV_PATH, "r") as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def get_env_or_token_file(key: str, default: str = "") -> str:
    """Config value for `key`, preferring token.env over the environment.

    Reads the file on every call, so editing token.env on the host takes effect
    for the next request without recreating the container. An empty value in
    the file counts as "not set" and falls through to the environment, which is
    what makes `SOUNDCLOUD_OAUTH_TOKEN=` in a committed token.env behave.
    """
    value = read_token_env_value(key)
    if value:
        return value
    return os.environ.get(key, default)


def update_token_env_keys(updates: Dict[str, str]) -> bool:
    """Update token.env in place while retaining comments and existing modes."""
    import logging

    path = Path(TOKEN_ENV_PATH)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        logging.warning(f"{path} does not exist; creating it")
        lines = []
    except OSError as error:
        logging.error(f"Could not read {path}: {error}")
        return False

    remaining = dict(updates)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}\n"
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    insert_at = 0
    for index, line in enumerate(lines):
        if line.startswith("#"):
            insert_at = index + 1
        elif line.strip():
            break
    for key, value in remaining.items():
        lines.insert(insert_at, f"{key}={value}\n")
        insert_at += 1

    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
    except OSError as error:
        logging.error(f"Could not write {path}: {error}")
        return False

    for key, value in updates.items():
        os.environ[key] = value
    return True



_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "n"})


def get_bool_env(key: str, default: bool = False) -> bool:
    """Boolean config value for `key`.

    Accepts 1/0, true/false, yes/no and on/off in any casing. An unset or empty
    value returns `default`, and anything the parser does not recognise does
    too, so a typo'd flag never silently enables a feature.
    """
    raw = get_env_or_token_file(key, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


def get_int_env(key: str, default: int) -> int:
    try:
        return int(get_env_or_token_file(key, str(default)).strip())
    except (TypeError, ValueError):
        return default


def get_float_env(key: str, default: float) -> float:
    try:
        return float(get_env_or_token_file(key, str(default)).strip())
    except (TypeError, ValueError):
        return default


if LANG is not None:
    print("Lang : " + LANG)
else:
    print("Lang : en")
    LANG = "en"


def __(s):
    return LANGS_FILE[s][LANG]


# ----------------------------
# DOWNLOAD CONCURRENCY & QUEUE
# ----------------------------


def _positive_int(raw, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# Bot-wide cap on simultaneous downloads, plus how many extra requests may wait
# for a slot to free up. 2 active + 1 queued means a 4th concurrent user is
# refused outright instead of being parked indefinitely.
MAX_CONCURRENT_DOWNLOADS = _positive_int(
    get_env_or_token_file("MAX_CONCURRENT_DOWNLOADS"), 2
)
MAX_QUEUED_DOWNLOADS = _positive_int(
    get_env_or_token_file("MAX_QUEUED_DOWNLOADS"), 1
)

SLOT_START = "start"  # active slot granted, may download immediately
SLOT_QUEUED = "queued"  # parked until an active slot frees up
SLOT_USER_BUSY = "user_busy"  # this user already has a download running
SLOT_REJECTED = "rejected"  # active slots and the queue are all full

class _SlotRegistry(dict):
    def get(self, key, default=None):
        value = super().get(key, default)
        if value is default and not isinstance(key, tuple):
            value = super().get((key,), default)
        return value


_active_slots: Dict[tuple, "DownloadSlot"] = _SlotRegistry()
_queued_slots: Deque["DownloadSlot"] = deque()
_current_slot: contextvars.ContextVar[Optional["DownloadSlot"]] = (
    contextvars.ContextVar("deadmoon_download_slot", default=None)
)


def _slot_key(user_id, chat_id=None) -> tuple:
    # The legacy one-argument form remains useful to non-Telegram integrations.
    # Telegram handlers always pass a chat ID, so one user can have independent
    # downloads in a DM and in multiple groups.
    return (user_id,) if chat_id is None else (user_id, chat_id)


def is_downloading(user_id, chat_id=None) -> bool:
    """True while this user/chat owns an active or queued download slot."""
    key = _slot_key(user_id, chat_id)
    if key in _active_slots:
        return True
    return any(s.key == key for s in _queued_slots)


class DownloadSlot:
    """A synchronous scheduler claim with asynchronous cancellation hooks."""

    def __init__(self, user_id, status: str, chat_id=None):
        self.user_id = user_id
        self.chat_id = chat_id
        self.key = _slot_key(user_id, chat_id)
        self.status = status
        self.cancelled = False
        self.task: Optional[asyncio.Task] = None
        self.process = None
        self.workdirs = set()
        self._cancel_hooks = []
        self._turn = asyncio.Event()
        if status != SLOT_QUEUED:
            self._turn.set()
        self._released = False
        if status == SLOT_START:
            self._bind_current_task()

    def _bind_current_task(self) -> None:
        try:
            self.task = asyncio.current_task()
            self._context_token = _current_slot.set(self)
        except RuntimeError:
            self.task = None
            self._context_token = None

    @property
    def granted(self) -> bool:
        return self.status in (SLOT_START, SLOT_QUEUED)

    def register_workdir(self, path) -> None:
        if path:
            self.workdirs.add(Path(path))

    def register_cancel_hook(self, hook) -> None:
        """Register a synchronous callback that stops request-specific I/O."""
        if hook not in self._cancel_hooks:
            self._cancel_hooks.append(hook)

    async def wait_for_turn(self) -> None:
        """Block until this queued slot is promoted, or stop on cancellation."""
        await self._turn.wait()
        if self.cancelled:
            raise asyncio.CancelledError
        self._bind_current_task()

    def _grant(self) -> None:
        self.status = SLOT_START
        self._turn.set()


def request_download(user_id, chat_id=None) -> DownloadSlot:
    """Atomically claim capacity; this function deliberately contains no awaits."""
    key = _slot_key(user_id, chat_id)
    if is_downloading(user_id, chat_id):
        return DownloadSlot(user_id, SLOT_USER_BUSY, chat_id)

    if len(_active_slots) < MAX_CONCURRENT_DOWNLOADS:
        slot = DownloadSlot(user_id, SLOT_START, chat_id)
        _active_slots[key] = slot
        return slot

    if len(_queued_slots) < MAX_QUEUED_DOWNLOADS:
        slot = DownloadSlot(user_id, SLOT_QUEUED, chat_id)
        _queued_slots.append(slot)
        return slot

    return DownloadSlot(user_id, SLOT_REJECTED, chat_id)


def release_download(slot: Optional[DownloadSlot]) -> None:
    """Synchronously return capacity and promote the next live queued request."""
    if slot is None or slot._released:
        return
    slot._released = True

    if _active_slots.get(slot.key) is slot:
        del _active_slots[slot.key]
    else:
        try:
            _queued_slots.remove(slot)
        except ValueError:
            return

    while _queued_slots and len(_active_slots) < MAX_CONCURRENT_DOWNLOADS:
        promoted = _queued_slots.popleft()
        if promoted.cancelled or promoted._released:
            continue
        _active_slots[promoted.key] = promoted
        promoted._grant()


def current_download_slot() -> Optional[DownloadSlot]:
    return _current_slot.get()


def _cancel_slots(slots: list[DownloadSlot]) -> tuple[int, int]:
    """Mark and release slots synchronously before invoking any cancel hooks."""
    selected = [slot for slot in slots if slot.granted and not slot._released]
    queued = sum(slot.status == SLOT_QUEUED for slot in selected)
    active = len(selected) - queued
    for slot in selected:
        slot.cancelled = True
        release_download(slot)
        slot._turn.set()  # wake queued handlers so they observe cancellation
    for slot in selected:
        for hook in tuple(slot._cancel_hooks):
            try:
                hook()
            except Exception:
                pass
        if slot.process is not None:
            try:
                from dl_utils.provider_engines import _kill_process_tree

                _kill_process_tree(slot.process)
            except Exception:
                pass
        if slot.task is not None and slot.task is not asyncio.current_task():
            slot.task.cancel()
        for workdir in tuple(slot.workdirs):
            shutil.rmtree(workdir, ignore_errors=True)
    return active, queued


def cancel_download(user_id, chat_id) -> tuple[int, int]:
    """Cancel only the caller's slot in the caller's current chat."""
    key = _slot_key(user_id, chat_id)
    slots = [_active_slots[key]] if key in _active_slots else []
    slots.extend(slot for slot in _queued_slots if slot.key == key)
    return _cancel_slots(slots)


def cancel_all_downloads() -> tuple[int, int]:
    """Cancel every active and queued request, including the administrator's."""
    return _cancel_slots(list(_active_slots.values()) + list(_queued_slots))


def scheduler_snapshot() -> dict:
    """Current scheduler occupancy, for the admin /status command.

    Read-only: it copies user IDs out of the internal structures so nothing
    downstream can mutate scheduler state through the report.
    """
    return {
        "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
        "max_queued": MAX_QUEUED_DOWNLOADS,
        "active_count": len(_active_slots),
        "queued_count": len(_queued_slots),
        "active_users": sorted(key[0] for key in _active_slots),
        "queued_users": sorted(slot.user_id for slot in _queued_slots),
        "active_chats": sorted((slot.user_id, slot.chat_id) for slot in _active_slots.values()),
        "queued_chats": sorted((slot.user_id, slot.chat_id) for slot in _queued_slots),
    }


async def announce_if_queued(event, slot: DownloadSlot) -> None:
    """Tell the user they are queued, then block until their download starts.

    `event` is the aiogram Message being answered; only its answer() coroutine
    and the returned Message's delete() are used, so utils needs no aiogram
    import. Returns immediately for slots that were granted without queueing.
    """
    if slot.status != SLOT_QUEUED:
        return

    queued_msg = await event.answer(__("queued_download"))
    await slot.wait_for_turn()
    try:
        await queued_msg.delete()
    except Exception:
        pass  # the message is gone either way; the download can proceed
