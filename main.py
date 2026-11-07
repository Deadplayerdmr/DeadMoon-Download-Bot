import asyncio
import html
import json
import logging
import os
import re
import shutil
import sys
import time
import locale
from pathlib import Path
from typing import Dict, Any, Awaitable, Callable, Set, List, Tuple

from aiogram import BaseMiddleware, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    ChatMemberUpdated,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

# Import your existing bot setup
from bot import bot, dp
from dl_utils import platform_config as pcfg
from dl_utils.deezer_download import TYPE_ALBUM, TYPE_TRACK
from handlers.deezer import (
    DEEZER_URL,
    deezer_router,
    handle_album_link,
    handle_track_link,
    refresh_deezer_session,
)
from handlers.streaming import status_report, streaming_router, supported_platforms
from handlers.yt_dlp import soundcloud_router, youtube_router
from utils import (
    TMP_DIR,
    TOKEN_ENV_PATH,
    cancel_all_downloads,
    cancel_download,
    get_env_or_token_file,
    scheduler_snapshot,
)

# Upstream: Deep-link payload regex for inline download button
DEEP_LINK_PAYLOAD_REGEX = re.compile(
    rf"^({TYPE_TRACK}|{TYPE_ALBUM})_(\d+)$"
)

# ----------------------------
# CONFIGURATION
# ----------------------------
# Load ADMIN_ID from environment variable (set in token.env)
ADMIN_ID = int(get_env_or_token_file("ADMIN_ID", "0") or 0)
if ADMIN_ID == 0:
    logging.error("ERROR: ADMIN_ID not set in token.env! Please set your Telegram User ID.")
    sys.exit(1)

# Upstream: Python version check
if sys.version_info < (3, 13):
    print(
        "Python 3.13 is required, but you are using Python {}.{}".format(
            sys.version_info.major, sys.version_info.minor
        )
    )
    sys.exit(1)

# Store whitelist in /data directory (mounted volume for persistence)
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
WHITELIST_FILE = DATA_DIR / "whitelist.json"

# ----------------------------
# LOGGING & SETUP
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Set locale and create necessary temp directories.
# One subtree per platform under TMP_DIR, so a request's staging tree can be
# removed wholesale in the handler's finally: block without touching another
# user's in-flight download.
try:
    locale.setlocale(locale.LC_TIME, "")
except locale.Error:
    pass  # Ignore locale errors in Docker

os.makedirs(TMP_DIR, exist_ok=True)
for _platform in ("deezer", "qobuz", "tidal", "amazon", "spotify", "apple", "yt", "sc"):
    os.makedirs(os.path.join(TMP_DIR, _platform), exist_ok=True)

# ----------------------------
# WHITELIST MANAGER
# ----------------------------
def load_whitelist() -> Set[int]:
    """Loads the whitelist from a JSON file."""
    if not WHITELIST_FILE.exists():
        logging.warning(f"Whitelist file not found. Creating new file with ADMIN_ID: {ADMIN_ID}")
        save_whitelist({ADMIN_ID})
        return {ADMIN_ID}
    try:
        with open(WHITELIST_FILE, "r") as f:
            data = json.load(f)
            return set(map(int, data)) | {ADMIN_ID}
    except Exception as e:
        logging.error(f"Error loading whitelist: {e}")
        return {ADMIN_ID}

def save_whitelist(whitelist: Set[int]):
    """Saves the current whitelist to a JSON file."""
    try:
        with open(WHITELIST_FILE, "w") as f:
            json.dump(list(whitelist), f, indent=4)
        logging.info(f"Whitelist saved with {len(whitelist)} IDs")
    except Exception as e:
        logging.error(f"Error saving whitelist: {e}")

# Load into memory
ALLOWED_IDS = load_whitelist()
logging.info(f"Loaded whitelist with {len(ALLOWED_IDS)} IDs")

# ----------------------------
# COMMAND SETUP
# ----------------------------
USER_COMMANDS = [
    BotCommand(command="start", description="Start the bot"),
    BotCommand(command="help", description="Show supported links and usage"),
    BotCommand(command="cancel", description="Cancel your download in this chat"),
]


async def set_user_commands(bot_instance):
    await bot_instance.set_my_commands(
        commands=USER_COMMANDS,
        scope=BotCommandScopeDefault(),
    )


async def set_admin_commands(bot_instance):
    """Set the administrator-only command list, including user commands."""
    admin_commands = [
        command for command in USER_COMMANDS if command.command != "cancel"
    ] + [
        BotCommand(command="cancel", description="Cancel your active/queued download (admin: /cancel all)"),
        BotCommand(command="whitelist", description="Show the current list of authorized IDs"),
        BotCommand(command="allow", description="Whitelist IDs: /allow <ID1> <ID2> or /allow <ID1>,<ID2>"),
        BotCommand(command="block", description="Block IDs: /block <ID1> <ID2> or /block <ID1>,<ID2>"),
        BotCommand(command="blockall", description="Remove ALL whitelisted IDs (except admin)"),
        BotCommand(command="newarl", description="Replace Deezer ARL token: /newarl <arl>"),
        BotCommand(command="arl", description="Show current Deezer ARL token"),
        BotCommand(command="status", description="Enabled platforms, engine modes, queue depth"),
    ]
    
    scope = BotCommandScopeChat(chat_id=ADMIN_ID)
    
    try:
        await bot_instance.set_my_commands(
            commands=admin_commands,
            scope=scope
        )
        logging.info(f"Successfully set custom commands for Admin ID: {ADMIN_ID}")
    except Exception as e:
        logging.error(f"Failed to set admin commands: {e}")

# ----------------------------
# MIDDLEWARE (Access Control)
# ----------------------------
class AccessControlMiddleware(BaseMiddleware):
    """Checks every incoming update (message, query, callback) for authorization."""
    
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        chat = data.get("event_chat")

        user_id = user.id if user else 0
        chat_id = chat.id if chat else 0
        chat_type = chat.type if chat else "private"

        # 1. Always allow the ADMIN
        if user_id == ADMIN_ID:
            return await handler(event, data)

        # 2. Check Permissions
        is_allowed = False
        
        if chat_type == "private":
            if user_id in ALLOWED_IDS:
                is_allowed = True
        elif chat_id in ALLOWED_IDS:
            is_allowed = True
        
        # 3. Decision Time
        if is_allowed:
            return await handler(event, data)
        else:
            if isinstance(event, Message) and chat_type == "private":
                await event.answer("You are not authorized to use this bot. Contact the admin for access.")
            
            return

# Register Middleware globally
dp.message.outer_middleware(AccessControlMiddleware())
dp.callback_query.outer_middleware(AccessControlMiddleware())
dp.inline_query.outer_middleware(AccessControlMiddleware())


# MERGED: Upstream deep-link support + user's access control
@dp.message(Command(commands=["start", "help"]))
async def help_start(event: types.Message, command: CommandObject):
    if command.command == "start" and command.args:
        payload_match = DEEP_LINK_PAYLOAD_REGEX.match(command.args.strip())
        if payload_match:
            kind, item_id = payload_match.group(1), payload_match.group(2)
            real_link = f"{DEEZER_URL}/{kind}/{item_id}"
            if kind == TYPE_TRACK:
                await handle_track_link(event, real_link=real_link)
            else:
                await handle_album_link(event, real_link=real_link)
            return

    bot_info = await bot.get_me()
    bot_username = html.escape(bot_info.username or "")
    platforms = html.escape(", ".join(supported_platforms()) or "Deezer")
    msg = (
        "Hey, I'm a music acquisition bot.\n\n"
        "<b>Send me a link</b> and I'll send back the audio as a zip archive.\n"
        f"<b>Supported:</b> {platforms}\n\n"
        "Track and album links work across all supported platforms "
        "(including mobile share links). Full artist discography links "
        "are supported for Deezer only.\n\n"
        f"<i>Inline mode:</i> <code>@{bot_username} "
        "(album|track|artist) &lt;search&gt;</code>"
    )
    await event.answer(msg, parse_mode="HTML")


@dp.message(Command("status"), F.from_user.id == ADMIN_ID)
async def admin_status(message: Message):
    """Report operational status only to the configured administrator."""
    await message.reply(status_report(), parse_mode="HTML")


@dp.message(Command("cancel"))
async def cancel_command(message: Message, command: CommandObject):
    """Cancel this user's chat-scoped work, or every slot for `/cancel all`."""
    is_global = (
        message.from_user is not None
        and message.from_user.id == ADMIN_ID
        and (command.args or "").strip().lower() == "all"
    )
    if is_global:
        active, queued = cancel_all_downloads()
        await message.reply(
            f"🛑 Cancelled {active} active and {queued} queued download(s) "
            "across all users and chats."
        )
        return

    active, queued = cancel_download(message.from_user.id, message.chat.id)
    if active or queued:
        await message.reply("🛑 Your download in this chat has been cancelled.")
    else:
        await message.reply("ℹ️ You have no active or queued download in this chat.")


def parse_ids_from_text(text: str) -> List[int]:
    """Parse multiple IDs from text, supporting spaces and commas as separators."""
    # Remove the command part (e.g., /allow or /block)
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return []
    
    ids_text = parts[1]
    
    # Replace commas with spaces and split
    ids_text = ids_text.replace(',', ' ')
    potential_ids = ids_text.split()
    
    valid_ids = []
    for id_str in potential_ids:
        id_str = id_str.strip()
        if not id_str:
            continue
        try:
            valid_ids.append(int(id_str))
        except ValueError:
            continue
    
    return valid_ids


# ----------------------------
# ADMIN COMMAND: /whitelist
# ----------------------------
@dp.message(Command("whitelist"), F.from_user.id == ADMIN_ID)
async def show_whitelist(message: Message):
    """Retrieves chat/user info for all whitelisted IDs and displays them."""
    
    await message.answer("Fetching whitelist details...")
    
    output_lines: List[str] = ["<b>Current Whitelist</b>", ""]
    
    # 1. Separate users and groups/channels
    users: List[int] = sorted([i for i in ALLOWED_IDS if i > 0])
    groups: List[int] = sorted([i for i in ALLOWED_IDS if i < 0])
    
    # 2. Function to fetch chat details concurrently
    async def fetch_chat_info(chat_id: int) -> Tuple[int, str]:
        try:
            chat = await bot.get_chat(chat_id)
            # Use 'title' for groups/channels and 'full_name' for users
            name = chat.title or chat.full_name
            return chat_id, html.escape(name or "Name Unknown")
        except Exception:
            # Handle cases where the bot cannot access the chat/user (e.g., bot was removed)
            return chat_id, "Access Denied / Not Found"

    # 3. Fetch details for all IDs concurrently
    tasks = [fetch_chat_info(chat_id) for chat_id in ALLOWED_IDS]
    results = await asyncio.gather(*tasks)
    
    # 4. Organize results for display
    user_results: List[Tuple[int, str]] = sorted([r for r in results if r[0] > 0], key=lambda x: x[1])
    group_results: List[Tuple[int, str]] = sorted([r for r in results if r[0] < 0], key=lambda x: x[1])

    # Display Users
    if user_results:
        output_lines.append("<b>Authorized Users:</b>")
        for chat_id, name in user_results:
            tag = " (ADMIN)" if chat_id == ADMIN_ID else ""
            output_lines.append(f"  - <code>{chat_id}</code>: {name}{tag}")
        output_lines.append("")

    # Display Groups/Channels
    if group_results:
        output_lines.append("<b>Authorized Groups/Channels:</b>")
        for chat_id, name in group_results:
            output_lines.append(f"  - <code>{chat_id}</code>: {name}")
        output_lines.append("")

    # 5. Send the formatted message
    response_text = "\n".join(output_lines)
    await message.answer(response_text, parse_mode="HTML")


# ----------------------------
# ADMIN COMMANDS: /allow, /block, /blockall
# ----------------------------
@dp.message(Command("allow"), F.from_user.id == ADMIN_ID)
async def allow_chat(message: Message):
    """Whitelist one or multiple IDs. Supports space or comma separation."""
    
    # Parse IDs from command
    target_ids = parse_ids_from_text(message.text)
    
    # If no IDs provided, use current chat
    if not target_ids:
        target_ids = [message.chat.id]
    
    added = []
    already_allowed = []
    
    for target_id in target_ids:
        if target_id not in ALLOWED_IDS:
            ALLOWED_IDS.add(target_id)
            added.append(target_id)
        else:
            already_allowed.append(target_id)
    
    if added:
        save_whitelist(ALLOWED_IDS)
    
    # Build response message
    response_parts = []
    if added:
        ids_str = ", ".join(f"`{id}`" for id in added)
        response_parts.append(f"Added to whitelist: {ids_str}")
    if already_allowed:
        ids_str = ", ".join(f"`{id}`" for id in already_allowed)
        response_parts.append(f"Already allowed: {ids_str}")
    
    await message.reply("\n".join(response_parts), parse_mode="Markdown")


@dp.message(Command("block"), F.from_user.id == ADMIN_ID)
async def block_chat(message: Message):
    """Block one or multiple IDs. Supports space or comma separation."""
    
    # Parse IDs from command
    target_ids = parse_ids_from_text(message.text)
    
    # If no IDs provided, use current chat
    if not target_ids:
        target_ids = [message.chat.id]
    
    blocked = []
    not_in_list = []
    admin_blocked_attempt = False
    
    for target_id in target_ids:
        if target_id == ADMIN_ID:
            admin_blocked_attempt = True
            continue
        
        if target_id in ALLOWED_IDS:
            ALLOWED_IDS.remove(target_id)
            blocked.append(target_id)
            
            # Try to leave if it's a group/channel
            if target_id != message.chat.id and target_id < 0:
                try:
                    await bot.leave_chat(target_id)
                except Exception:
                    pass
        else:
            not_in_list.append(target_id)
    
    if blocked:
        save_whitelist(ALLOWED_IDS)
    
    # Build response message
    response_parts = []
    if blocked:
        ids_str = ", ".join(f"`{id}`" for id in blocked)
        response_parts.append(f"Removed from whitelist: {ids_str}")
    if not_in_list:
        ids_str = ", ".join(f"`{id}`" for id in not_in_list)
        response_parts.append(f"Not in whitelist: {ids_str}")
    if admin_blocked_attempt:
        response_parts.append("Cannot block the Admin ID.")
    
    await message.reply("\n".join(response_parts), parse_mode="Markdown")


@dp.message(Command("blockall"), F.from_user.id == ADMIN_ID)
async def block_all(message: Message):
    """Remove ALL whitelisted IDs except the admin."""
    
    # Count how many will be removed
    ids_to_remove = [id for id in ALLOWED_IDS if id != ADMIN_ID]
    count = len(ids_to_remove)
    
    if count == 0:
        await message.reply("No IDs to remove (only admin is whitelisted).")
        return
    
    # Clear all except admin
    ALLOWED_IDS.clear()
    ALLOWED_IDS.add(ADMIN_ID)
    save_whitelist(ALLOWED_IDS)
    
    # Try to leave groups/channels
    left_groups = 0
    for target_id in ids_to_remove:
        if target_id < 0:  # Group or channel
            try:
                await bot.leave_chat(target_id)
                left_groups += 1
            except Exception:
                pass
    
    response = f"Removed **{count}** IDs from whitelist."
    if left_groups > 0:
        response += f"\nLeft {left_groups} groups/channels."
    
    await message.reply(response, parse_mode="Markdown")


# ----------------------------
# ARL TOKEN MANAGEMENT
# ----------------------------
def get_deezer_token() -> str:
    """Read DEEZER_TOKEN from token.env, falling back to the environment.

    The container gets the token through compose's env_file, which never
    creates a readable token.env, so the environment is the only source
    available in some deployments.
    """
    token = get_env_or_token_file("DEEZER_TOKEN", "")
    if not token and not TOKEN_ENV_PATH.exists():
        logging.warning(f"token.env not found at {TOKEN_ENV_PATH}; using environment")
    return token


def set_deezer_token(new_token: str) -> bool:
    """Update DEEZER_TOKEN in token.env file."""
    try:
        with open(TOKEN_ENV_PATH, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        logging.warning(f"{TOKEN_ENV_PATH} does not exist; creating it")
        lines = []
    except OSError as e:
        logging.error(f"Could not read {TOKEN_ENV_PATH}: {e}")
        return False

    # Update or add DEEZER_TOKEN
    found = False
    for i, line in enumerate(lines):
        if line.startswith("DEEZER_TOKEN="):
            lines[i] = f"DEEZER_TOKEN={new_token}\n"
            found = True
            break

    if not found:
        # Add at the beginning after any comments
        insert_pos = 0
        for i, line in enumerate(lines):
            if line.startswith("#"):
                insert_pos = i + 1
            elif line.strip():
                break
        lines.insert(insert_pos, f"DEEZER_TOKEN={new_token}\n")

    # A last line without a trailing newline would swallow the new key.
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    try:
        # os.open applies the mode only when it creates the file, so a
        # bind-mounted token.env keeps its host permissions while a fresh one
        # (the FileNotFoundError branch above) lands at 0600 instead of the
        # umask default of 0644 with the ARL already written into it.
        fd = os.open(TOKEN_ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.writelines(lines)
    except OSError as e:
        logging.error(f"Could not write {TOKEN_ENV_PATH}: {e}")
        return False

    # Keep this process in sync: init_deezer_session() reads DEEZER_TOKEN from
    # the environment, so writing the file alone would not change anything.
    os.environ["DEEZER_TOKEN"] = new_token
    return True


@dp.message(Command("arl"), F.from_user.id == ADMIN_ID)
async def show_arl(message: Message):
    """Display the current DEEZER_TOKEN (ARL) to the admin."""
    token = get_deezer_token()
    
    if not token:
        await message.reply("⚠️ No DEEZER_TOKEN found in token.env")
        return
    
    # Mask the token for security (show first 10 and last 10 chars)
    if len(token) > 20:
        masked = f"{token[:10]}...{token[-10:]}"
    else:
        masked = token
    
    response = f"**Current ARL Token:**\n`{masked}`"
    await message.reply(response, parse_mode="Markdown")


@dp.message(Command("newarl"), F.from_user.id == ADMIN_ID)
async def update_arl(message: Message):
    """Update the DEEZER_TOKEN and restart the bot."""
    
    # Extract token from command
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("❌ Usage: `/newarl <token>`", parse_mode="Markdown")
        return
    
    new_token = parts[1].strip()
    
    # Validate token (should be a long hex string)
    if len(new_token) < 50:
        await message.reply("⚠️ Token seems too short. ARL tokens are typically 192+ characters.")
        return
    
    # Update the token file
    if set_deezer_token(new_token):
        # Rebuild the session here instead of exiting the process. sys.exit()
        # inside an async handler is unreliable, and a container restart reuses
        # the creation-time environment, so the new token could be lost.
        try:
            await refresh_deezer_session("admin /newarl token update")
        except Exception as e:
            logging.error(f"Session re-init after /newarl failed: {e}")
            await message.reply(
                "⚠️ <b>ARL token saved, but the session could not be reloaded.</b>\n\n"
                f"<code>{html.escape(str(e))}</code>\n"
                "Restart the bot manually to apply it.",
                parse_mode="HTML",
            )
            return

        await message.reply(
            "✅ **ARL token updated successfully!**\n\n"
            "Deezer session reloaded — no restart needed.",
            parse_mode="Markdown",
        )
        logging.info("Admin updated the ARL token; session reloaded in-process.")
    else:
        await message.reply("❌ Failed to update token.env file")


# ----------------------------
# AUTO-LEAVE & AUTO-ADD LOGIC
# ----------------------------
@dp.my_chat_member(F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_membership_update(event: ChatMemberUpdated):
    chat = event.chat
    status = event.new_chat_member.status
    user = event.from_user

    if status in ("member", "administrator"):
        logging.info(f"Bot added to {chat.type} {chat.title} ({chat.id}) by {user.id}")

        if user.id == ADMIN_ID:
            if chat.id not in ALLOWED_IDS:
                ALLOWED_IDS.add(chat.id)
                save_whitelist(ALLOWED_IDS)
                await bot.send_message(chat.id, "Admin detected. Group automatically authorized and added to whitelist.")
                logging.info(f"Auto-whitelisted chat {chat.id}")
            return

        if chat.id not in ALLOWED_IDS:
            logging.info(f"Leaving unauthorized {chat.type}: {chat.id}")
            await bot.send_message(chat.id, "This bot is private. Contact the admin to request access.")
            await asyncio.sleep(2)
            await bot.leave_chat(chat.id)

# ----------------------------
# BACKGROUND TASK
# ----------------------------

# A hosted archive written moments ago can still be mid-read by the MIR
# pipeline, so the age threshold is padded. 60s is larger than any plausible
# write gap and far smaller than the shortest sane AUTO_DELETE_SECONDS.
SWEEP_GRACE_SECONDS = 60


def sweep_orphaned_archives(now: float | None = None) -> List[Path]:
    """Delete aged *.zip files left in COPY_FILES_PATH by a crash or restart.

    schedule_file_deletion() only fires for archives the process still knows
    about, so anything written before an unclean shutdown would live on a
    publicly served directory forever. This is the reclaim path for those.

    Returns the paths it actually unlinked.
    """
    if not pcfg.auto_delete_enabled():
        return []
    host_dir = pcfg.copy_files_path()
    if not host_dir:
        # Direct-Telegram mode never writes archives to disk, so nothing can be
        # orphaned here.
        return []

    root = Path(host_dir)
    if not root.is_dir():
        logging.warning(f"[sweep] COPY_FILES_PATH is not a directory: {root}")
        return []

    if now is None:
        now = time.time()
    cutoff = now - (pcfg.auto_delete_seconds() + SWEEP_GRACE_SECONDS)

    removed: List[Path] = []
    try:
        entries = list(os.scandir(root))
    except OSError as e:
        logging.error(f"[sweep] could not scan {root}: {e}")
        return []

    for entry in entries:
        try:
            if not entry.name.lower().endswith(".zip"):
                continue
            # follow_symlinks=False: a symlink in a served directory pointing
            # outside it is not ours to delete.
            if not entry.is_file(follow_symlinks=False):
                continue
            if entry.stat(follow_symlinks=False).st_mtime > cutoff:
                continue
            os.unlink(entry.path)
            removed.append(Path(entry.path))
        except FileNotFoundError:
            continue  # schedule_file_deletion() got there first
        except OSError as e:
            logging.error(f"[sweep] could not remove {entry.path}: {e}")

    if removed:
        logging.info(f"[sweep] reclaimed {len(removed)} orphaned archive(s) from {root}")
    return removed


def sweep_orphaned_staging(now: float | None = None) -> List[Path]:
    """Remove staging subtrees under TMP_DIR, but only while the bot is idle.

    A handler's finally: block owns its own workdir, so staged files normally
    never outlive a request; this catches the residue of a crash between slot
    grant and release. It refuses to run while any download is active or queued:
    a live workdir is indistinguishable from an orphaned one out here, and
    deleting the wrong one corrupts an in-flight archive.
    """
    if not pcfg.auto_delete_enabled():
        return []

    snap = scheduler_snapshot()
    if snap["active_count"] or snap["queued_count"]:
        logging.debug("[sweep] downloads in flight, skipping TMP_DIR scan")
        return []

    if now is None:
        now = time.time()
    cutoff = now - (pcfg.auto_delete_seconds() + SWEEP_GRACE_SECONDS)
    root = Path(TMP_DIR)
    if not root.is_dir():
        return []

    removed: List[Path] = []
    # Only leaf request dirs (<platform>/<kind>/<id>) are candidates; the
    # per-platform skeletons main.py creates at startup are recreated on every
    # boot and are not orphans.
    for platform_dir in sorted(root.iterdir()):
        if not platform_dir.is_dir():
            continue
        for kind_dir in sorted(platform_dir.iterdir()):
            if not kind_dir.is_dir():
                continue
            for request_dir in sorted(kind_dir.iterdir()):
                try:
                    if not request_dir.is_dir():
                        continue
                    if request_dir.stat().st_mtime > cutoff:
                        continue
                    shutil.rmtree(request_dir, ignore_errors=True)
                    removed.append(request_dir)
                except FileNotFoundError:
                    continue
                except OSError as e:
                    logging.error(f"[sweep] could not remove {request_dir}: {e}")

    if removed:
        logging.info(f"[sweep] reclaimed {len(removed)} orphaned staging dir(s)")
    return removed


async def run_cleanup_sweeps() -> Dict[str, int]:
    """One pass of both sweeps, off the event loop. Split out of the ticker so
    the work is testable without waiting on the interval."""
    archives = await asyncio.to_thread(sweep_orphaned_archives)
    staging = await asyncio.to_thread(sweep_orphaned_staging)
    return {"archives": len(archives), "staging": len(staging)}


# How often the orphan sweeper runs. Module-level so a test can shrink it and
# exercise the real ticker instead of asserting its source shape.
CLEANUP_INTERVAL_SECONDS = 600


async def background_cleanup():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            # Both sweeps are blocking filesystem walks; asyncio.to_thread keeps
            # a large served directory from stalling Telegram polling.
            await run_cleanup_sweeps()
        except Exception as e:
            logging.error(f"Background cleanup error: {e}")

# ----------------------------
# MAIN
# ----------------------------
async def main() -> None:
    # Include your music routers. streaming_router goes last: its shortlink
    # handler is a catch-all for this router's platforms, and the concrete
    # per-kind handlers inside it are registered before it, so ordering against
    # the other routers only matters for hosts they share.
    dp.include_routers(youtube_router, soundcloud_router, deezer_router, streaming_router)

    logging.info("Bot is starting...")
    logging.info(f"Admin ID: {ADMIN_ID}")
    logging.info(f"Whitelist loaded: {len(ALLOWED_IDS)} IDs")
    
    await set_user_commands(bot)
    await set_admin_commands(bot)

    # Start polling and background tasks
    await asyncio.gather(
        dp.start_polling(bot),
        background_cleanup(),
    )

if __name__ == "__main__":
    asyncio.run(main())
