"""Offline checks for the concurrency scheduler in utils.py.

These cover the invariants every download handler depends on: one slot per user,
hard caps on active and queued work, FIFO promotion on release, and no capacity
leak when a slot is released twice or was never granted.

Run with `python3 -m unittest discover tests`. No network, no Telegram.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")

import utils  # noqa: E402
from utils import (  # noqa: E402
    SLOT_QUEUED,
    SLOT_REJECTED,
    SLOT_START,
    SLOT_USER_BUSY,
    is_downloading,
    release_download,
    request_download,
    scheduler_snapshot,
)


class _FakeMessage:
    def __init__(self):
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _FakeEvent:
    """Minimal stand-in for the aiogram Message that announce_if_queued touches."""

    def __init__(self):
        self.answers = []
        self.messages = []

    async def answer(self, text):
        self.answers.append(text)
        message = _FakeMessage()
        self.messages.append(message)
        return message

    @property
    def deleted_count(self):
        return sum(1 for m in self.messages if m.deleted)


class _FlakyDeleteEvent(_FakeEvent):
    """Like _FakeEvent, but delete() raises, as a Telegram network error would."""

    async def answer(self, text):
        self.answers.append(text)

        class Undeletable:
            async def delete(self):
                raise RuntimeError("message was already gone")

        return Undeletable()


class SchedulerTestCase(unittest.TestCase):
    """Reset scheduler state and pin the caps, so tests are order-independent."""

    def setUp(self):
        utils._active_slots.clear()
        utils._queued_slots.clear()
        self._saved = (utils.MAX_CONCURRENT_DOWNLOADS, utils.MAX_QUEUED_DOWNLOADS)
        utils.MAX_CONCURRENT_DOWNLOADS = 2
        utils.MAX_QUEUED_DOWNLOADS = 1

    def tearDown(self):
        utils.MAX_CONCURRENT_DOWNLOADS, utils.MAX_QUEUED_DOWNLOADS = self._saved
        utils._active_slots.clear()
        utils._queued_slots.clear()

    def run_async(self, coro):
        return asyncio.run(coro)

    def active_slot(self, user_id):
        slot = utils._active_slots.get(user_id)
        self.assertIsNotNone(slot, f"user {user_id} holds no active slot")
        return slot


class TestGrantAndCaps(SchedulerTestCase):
    def test_first_request_starts_immediately(self):
        slot = request_download(1)
        self.assertTrue(slot.granted)
        self.assertEqual(slot.status, SLOT_START)

    def test_capacity_is_respected(self):
        a, b = request_download(1), request_download(2)
        self.assertTrue(a.granted and b.granted)
        # The third user fits in the single queue slot.
        c = request_download(3)
        self.assertEqual(c.status, SLOT_QUEUED)
        self.assertTrue(c.granted)
        # The fourth has nowhere to go.
        self.assertEqual(request_download(4).status, SLOT_REJECTED)

    def test_queue_full_rejects(self):
        utils.MAX_QUEUED_DOWNLOADS = 0
        request_download(1)
        request_download(2)
        self.assertEqual(request_download(3).status, SLOT_REJECTED)

    def test_rejected_slot_grants_no_capacity(self):
        utils.MAX_CONCURRENT_DOWNLOADS = 1
        utils.MAX_QUEUED_DOWNLOADS = 0
        self.assertTrue(request_download(1).granted)
        slot = request_download(9)
        self.assertFalse(slot.granted)
        snap = scheduler_snapshot()
        # Only user 1's claim exists; the rejection added nothing to track.
        self.assertEqual(snap["active_count"], 1)
        self.assertEqual(snap["queued_count"], 0)
        self.assertEqual(snap["active_users"], [1])

    def test_rejected_slot_is_not_waiting(self):
        utils.MAX_CONCURRENT_DOWNLOADS = 1
        utils.MAX_QUEUED_DOWNLOADS = 0
        slot = request_download(9)
        # A rejected slot is already decided, so nothing may block on it.
        self.run_async(asyncio.wait_for(slot.wait_for_turn(), timeout=1))


class TestOneSlotPerUser(SchedulerTestCase):
    def test_second_request_from_same_user_is_busy(self):
        request_download(7)
        self.assertEqual(request_download(7).status, SLOT_USER_BUSY)

    def test_queued_user_also_counts_as_busy(self):
        request_download(1)
        request_download(2)
        request_download(3)  # queued
        self.assertEqual(request_download(3).status, SLOT_USER_BUSY)

    def test_is_downloading_covers_active_and_queued(self):
        self.assertFalse(is_downloading(5))
        request_download(5)
        self.assertTrue(is_downloading(5))
        release_download(self.active_slot(5))
        self.assertFalse(is_downloading(5))

    def test_busy_response_does_not_consume_capacity(self):
        slot = request_download(1)
        before = scheduler_snapshot()
        request_download(1)
        request_download(1)
        self.assertEqual(scheduler_snapshot(), before)
        release_download(slot)

    def test_user_busy_does_not_queue_the_same_user_twice(self):
        request_download(4)
        for _ in range(5):
            request_download(4)
        self.assertEqual(scheduler_snapshot()["queued_count"], 0)


class TestReleaseAndPromotion(SchedulerTestCase):
    def test_release_frees_the_slot(self):
        slot = request_download(1)
        release_download(slot)
        self.assertFalse(is_downloading(1))
        self.assertEqual(scheduler_snapshot()["active_count"], 0)

    def test_release_promotes_the_queued_request(self):
        a, b = request_download(1), request_download(2)
        queued = request_download(3)
        self.assertEqual(queued.status, SLOT_QUEUED)
        release_download(a)
        self.assertEqual(queued.status, SLOT_START)
        snap = scheduler_snapshot()
        self.assertEqual(snap["active_count"], 2)
        self.assertEqual(snap["queued_count"], 0)
        self.assertIn(3, snap["active_users"])
        release_download(b)
        release_download(queued)
        self.assertEqual(scheduler_snapshot()["active_count"], 0)

    def test_promotion_is_fifo(self):
        utils.MAX_CONCURRENT_DOWNLOADS = 1
        utils.MAX_QUEUED_DOWNLOADS = 3
        first = request_download(1)
        q2, q3, q4 = request_download(2), request_download(3), request_download(4)
        self.assertEqual([q.status for q in (q2, q3, q4)], [SLOT_QUEUED] * 3)
        release_download(first)
        self.assertEqual(q2.status, SLOT_START)
        self.assertEqual([q.status for q in (q3, q4)], [SLOT_QUEUED, SLOT_QUEUED])
        release_download(q2)
        self.assertEqual(q3.status, SLOT_START)

    def test_double_release_is_a_no_op(self):
        slot = request_download(1)
        release_download(slot)
        release_download(slot)
        self.assertEqual(scheduler_snapshot()["active_count"], 0)
        # Capacity must still be intact for a fresh request.
        self.assertTrue(request_download(2).granted)

    def test_releasing_an_ungranted_slot_changes_nothing(self):
        utils.MAX_CONCURRENT_DOWNLOADS = 1
        utils.MAX_QUEUED_DOWNLOADS = 0
        request_download(1)
        rejected = request_download(2)
        before = scheduler_snapshot()
        release_download(rejected)
        self.assertEqual(scheduler_snapshot(), before)

    def test_release_none_is_safe(self):
        release_download(None)
        self.assertEqual(scheduler_snapshot()["active_count"], 0)

    def test_stale_handle_does_not_free_a_new_claim(self):
        # User 1 releases, then immediately takes a new slot. Dropping the old
        # handle afterwards must not evict the live one.
        old = request_download(1)
        release_download(old)
        new = request_download(1)
        release_download(old)
        self.assertTrue(new.granted)
        self.assertTrue(is_downloading(1))
        self.assertEqual(scheduler_snapshot()["active_count"], 1)

    def test_no_capacity_leaks_across_many_cycles(self):
        for user in range(50):
            slot = request_download(user)
            self.assertTrue(slot.granted, user)
            release_download(slot)
        snap = scheduler_snapshot()
        self.assertEqual(snap["active_count"], 0)
        self.assertEqual(snap["queued_count"], 0)


class TestSnapshot(SchedulerTestCase):
    def test_snapshot_is_a_copy(self):
        request_download(1)
        snap = scheduler_snapshot()
        snap["active_users"].append(999)
        snap["queued_users"].append(999)
        fresh = scheduler_snapshot()
        self.assertNotIn(999, fresh["active_users"])
        self.assertNotIn(999, fresh["queued_users"])

    def test_snapshot_reports_configured_caps(self):
        snap = scheduler_snapshot()
        self.assertEqual(snap["max_concurrent"], 2)
        self.assertEqual(snap["max_queued"], 1)

    def test_snapshot_lists_queued_users_in_order(self):
        utils.MAX_CONCURRENT_DOWNLOADS = 1
        utils.MAX_QUEUED_DOWNLOADS = 3
        request_download(1)
        request_download(3)
        request_download(2)
        snap = scheduler_snapshot()
        self.assertEqual(snap["queued_users"], [2, 3])
        self.assertEqual(snap["active_users"], [1])


class TestWaitForTurn(SchedulerTestCase):
    def test_queued_slot_waits_until_promoted(self):
        utils.MAX_CONCURRENT_DOWNLOADS = 1

        async def scenario():
            a = request_download(1)
            queued = request_download(2)
            waiter = asyncio.create_task(queued.wait_for_turn())
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            release_download(a)
            await asyncio.wait_for(waiter, timeout=1)
            return queued.status

        self.assertEqual(self.run_async(scenario()), SLOT_START)

    def test_granted_slot_does_not_block(self):
        async def scenario():
            slot = request_download(1)
            await asyncio.wait_for(slot.wait_for_turn(), timeout=1)

        self.run_async(scenario())


class TestAnnounceQueued(SchedulerTestCase):
    def test_announce_is_silent_for_an_active_slot(self):
        async def scenario():
            slot = request_download(1)
            fake = _FakeEvent()
            await utils.announce_if_queued(fake, slot)
            return fake

        self.assertEqual(self.run_async(scenario()).answers, [])

    def test_announce_notifies_waits_then_clears_the_notice(self):
        utils.MAX_CONCURRENT_DOWNLOADS = 1

        async def scenario():
            a = request_download(1)
            queued = request_download(2)
            fake = _FakeEvent()
            announcer = asyncio.create_task(utils.announce_if_queued(fake, queued))
            await asyncio.sleep(0)
            self.assertEqual(len(fake.answers), 1)
            self.assertFalse(announcer.done())
            release_download(a)
            await asyncio.wait_for(announcer, timeout=1)
            self.assertEqual(queued.status, SLOT_START)
            return fake

        finished = self.run_async(scenario())
        self.assertEqual(finished.deleted_count, 1)

    def test_announce_survives_a_message_that_cannot_be_deleted(self):
        utils.MAX_CONCURRENT_DOWNLOADS = 1

        async def scenario():
            a = request_download(1)
            queued = request_download(2)
            fake = _FlakyDeleteEvent()
            release_download(a)
            await asyncio.wait_for(utils.announce_if_queued(fake, queued), timeout=1)
            return queued.status

        self.assertEqual(self.run_async(scenario()), SLOT_START)


class TestCapParsing(unittest.TestCase):
    def test_non_positive_and_junk_caps_fall_back(self):
        for raw in ("0", "-3", "junk", None, ""):
            self.assertEqual(utils._positive_int(raw, 2), 2, raw)

    def test_valid_cap_is_used(self):
        self.assertEqual(utils._positive_int("5", 2), 5)
        self.assertEqual(utils._positive_int(7, 2), 7)


if __name__ == "__main__":
    unittest.main()
