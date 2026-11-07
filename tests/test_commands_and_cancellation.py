import asyncio
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")

import utils  # noqa: E402
from tests.test_production_runtime_fixes import isolated_main  # noqa: E402


class SchedulerIsolationCase(unittest.TestCase):
    def setUp(self):
        self.saved_caps = utils.MAX_CONCURRENT_DOWNLOADS, utils.MAX_QUEUED_DOWNLOADS
        utils.MAX_CONCURRENT_DOWNLOADS = 4
        utils.MAX_QUEUED_DOWNLOADS = 3
        utils._active_slots.clear()
        utils._queued_slots.clear()

    def tearDown(self):
        utils.MAX_CONCURRENT_DOWNLOADS, utils.MAX_QUEUED_DOWNLOADS = self.saved_caps
        utils._active_slots.clear()
        utils._queued_slots.clear()

    def test_cancel_is_scoped_to_user_and_current_chat(self):
        group = utils.request_download(10, -100)
        private = utils.request_download(10, 10)
        other_user = utils.request_download(20, -100)
        other_group = utils.request_download(10, -200)

        self.assertEqual(utils.cancel_download(10, -100), (1, 0))

        self.assertTrue(group.cancelled)
        self.assertFalse(private.cancelled)
        self.assertFalse(other_user.cancelled)
        self.assertFalse(other_group.cancelled)
        self.assertEqual(
            {slot.key for slot in utils._active_slots.values()},
            {(10, 10), (20, -100), (10, -200)},
        )

    def test_cancel_queued_slot_and_promote_next_live_request(self):
        utils.MAX_CONCURRENT_DOWNLOADS = 1
        active = utils.request_download(1, 1)
        cancelled = utils.request_download(2, 2)
        promoted = utils.request_download(3, 3)

        self.assertEqual(utils.cancel_download(2, 2), (0, 1))
        self.assertTrue(cancelled.cancelled)
        self.assertEqual(utils.scheduler_snapshot()["queued_count"], 1)
        utils.release_download(active)
        self.assertEqual(promoted.status, utils.SLOT_START)
        self.assertEqual(utils.scheduler_snapshot()["queued_count"], 0)
        self.assertIs(utils._active_slots[(3, 3)], promoted)

    def test_cancel_kills_process_runs_hooks_and_removes_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "request"
            workdir.mkdir()
            (workdir / "partial.ogg").write_bytes(b"partial")
            slot = utils.request_download(1, -1)
            slot.process = SimpleNamespace(pid=123)
            slot.register_workdir(workdir)
            called = []
            slot.register_cancel_hook(lambda: called.append("stop"))
            with mock.patch("dl_utils.provider_engines._kill_process_tree") as kill:
                self.assertEqual(utils.cancel_download(1, -1), (1, 0))
            kill.assert_called_once_with(slot.process)
            self.assertEqual(called, ["stop"])
            self.assertFalse(workdir.exists())

    def test_global_cancel_includes_all_users_and_reports_counts(self):
        utils.MAX_CONCURRENT_DOWNLOADS = 2
        utils.request_download(1, 10)
        utils.request_download(2, -20)
        utils.request_download(3, 30)
        self.assertEqual(utils.cancel_all_downloads(), (2, 1))
        self.assertEqual(utils.scheduler_snapshot()["active_count"], 0)
        self.assertEqual(utils.scheduler_snapshot()["queued_count"], 0)

    def test_grant_and_release_remain_synchronous(self):
        self.assertFalse(inspect.iscoroutinefunction(utils.request_download))
        self.assertFalse(inspect.iscoroutinefunction(utils.release_download))
        for function in (utils.request_download, utils.release_download):
            self.assertNotIn("await ", inspect.getsource(function))


class _Message:
    def __init__(self, user_id, chat_id):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=chat_id)
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


class TestCancelCommand(SchedulerIsolationCase):
    def test_non_admin_cancel_all_only_cancels_current_chat(self):
        async def scenario():
            with isolated_main() as (main, _):
                own_group = utils.request_download(7, -100)
                own_dm = utils.request_download(7, 7)
                other = utils.request_download(8, -100)
                message = _Message(7, -100)
                await main.cancel_command(message, SimpleNamespace(args="all"))
                self.assertTrue(own_group.cancelled)
                self.assertFalse(own_dm.cancelled)
                self.assertFalse(other.cancelled)
                self.assertEqual(message.replies, ["🛑 Your download in this chat has been cancelled."])

        asyncio.run(scenario())

    def test_admin_cancel_all_cancels_every_slot(self):
        async def scenario():
            with isolated_main() as (main, _):
                utils.request_download(main.ADMIN_ID, -100)
                utils.request_download(7, 7)
                message = _Message(main.ADMIN_ID, -100)
                await main.cancel_command(message, SimpleNamespace(args="ALL"))
                self.assertEqual(utils.scheduler_snapshot()["active_count"], 0)
                self.assertIn("2 active and 0 queued", message.replies[0])

        asyncio.run(scenario())

    def test_empty_chat_has_exact_informational_response(self):
        async def scenario():
            with isolated_main() as (main, _):
                message = _Message(7, -100)
                await main.cancel_command(message, SimpleNamespace(args=None))
                self.assertEqual(
                    message.replies,
                    ["ℹ️ You have no active or queued download in this chat."],
                )

        asyncio.run(scenario())

    def test_admin_status_filter_accepts_admin(self):
        async def scenario():
            from aiogram import types

            with isolated_main() as (main, _):
                update = types.Update(
                    update_id=1,
                    message=types.Message(
                        message_id=1,
                        date=0,
                        chat=types.Chat(id=main.ADMIN_ID, type="private"),
                        from_user=types.User(id=main.ADMIN_ID, is_bot=False, first_name="Admin"),
                        text="/status",
                        entities=[types.MessageEntity(type="bot_command", offset=0, length=7)],
                    ),
                )
                message = update.message
                registered = next(
                    handler for handler in main.dp.message.handlers
                    if handler.callback is main.admin_status
                )
                checked = await registered.check(
                    message,
                    bot=main.bot,
                    event_from_user=message.from_user,
                    event_chat=message.chat,
                    event_update=update,
                )
                self.assertTrue(checked[0])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
