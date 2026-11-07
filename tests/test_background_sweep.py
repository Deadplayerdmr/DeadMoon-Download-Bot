"""Offline checks for the crash-orphan sweeper in main.background_cleanup.

Two contracts are enforced here:

  * an aged *.zip in COPY_FILES_PATH is reclaimed, and anything younger than
    AUTO_DELETE_SECONDS + SWEEP_GRACE_SECONDS is left alone;
  * the TMP_DIR half refuses to run at all while a download is active or queued,
    because a live workdir is indistinguishable from an orphaned one from out
    here and deleting the wrong one corrupts an in-flight archive.

Run with `python3 -m unittest discover tests`. No network, no Telegram.
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")


def _main_importable() -> bool:
    """main imports handlers.deezer, which pulls pycryptodome via deezer_download."""
    return importlib.util.find_spec("Crypto") is not None


def _token_env_path() -> Path:
    import utils

    return utils.TOKEN_ENV_PATH


def _set_token_env_path(value) -> None:
    """Point config resolution at a nonexistent file so the environment wins."""
    import utils

    utils.TOKEN_ENV_PATH = value


TTL = 1800
GRACE = 60


@unittest.skipUnless(_main_importable(), "pycryptodome is not installed")
class SweepCase(unittest.TestCase):
    """Hermetic harness: main.py exits at import without ADMIN_ID and writes
    data/whitelist.json relative to the CWD, so both are bracketed here."""

    @classmethod
    def setUpClass(cls):
        cls._root = tempfile.TemporaryDirectory()
        cls._saved_cwd = os.getcwd()
        cls._saved_token_env = _token_env_path()
        os.chdir(cls._root.name)
        _set_token_env_path(Path(cls._root.name) / "absent-token.env")
        os.environ["ADMIN_ID"] = "4242"
        os.environ["AUTO_DELETE_ENABLED"] = "1"
        os.environ["AUTO_DELETE_SECONDS"] = str(TTL)
        try:
            import main

            cls.main = main
        except SystemExit as e:
            raise unittest.SkipTest(
                f"main exited at import (code {e.code}); env isolation failed"
            ) from e

    @classmethod
    def tearDownClass(cls):
        _set_token_env_path(cls._saved_token_env)
        os.chdir(cls._saved_cwd)
        cls._root.cleanup()
        sys.modules.pop("main", None)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self._root.name))
        self.now = time.time()

        self._saved = {
            "COPY_FILES_PATH": os.environ.get("COPY_FILES_PATH"),
            "AUTO_DELETE_ENABLED": os.environ.get("AUTO_DELETE_ENABLED"),
            "TMP_DIR": self.main.TMP_DIR,
            "snapshot": self.main.scheduler_snapshot,
        }
        self.addCleanup(self._restore)

    def _restore(self):
        for key in ("COPY_FILES_PATH", "AUTO_DELETE_ENABLED"):
            value = self._saved[key]
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.main.TMP_DIR = self._saved["TMP_DIR"]
        self.main.scheduler_snapshot = self._saved["snapshot"]

    def host_dir(self) -> Path:
        root = self.work / "files"
        root.mkdir(parents=True, exist_ok=True)
        os.environ["COPY_FILES_PATH"] = str(root)
        return root

    def idle(self):
        self.main.scheduler_snapshot = lambda: {
            "max_concurrent": 2,
            "max_queued": 1,
            "active_count": 0,
            "queued_count": 0,
            "active_users": [],
            "queued_users": [],
        }

    def busy(self, active=0, queued=0):
        snap = self.idle()
        self.main.scheduler_snapshot = lambda: {
            "max_concurrent": 2,
            "max_queued": 1,
            "active_count": active,
            "queued_count": queued,
            "active_users": [],
            "queued_users": [],
        }
        return snap

    def make_zip(self, directory: Path, name: str, age: float) -> Path:
        path = directory / name
        path.write_bytes(b"PK\x03\x04 fake archive")
        os.utime(path, (self.now - age, self.now - age))
        return path


class _OsProxy:
    """Shadows main.os for one test while forwarding everything else to the real
    module. Assigning to os.unlink directly would mutate the process-global os
    module and leak into unrelated tests if a cleanup ever failed to fire."""

    def __init__(self, unlink):
        object.__setattr__(self, "_unlink", unlink)

    def unlink(self, path):
        return self._unlink(path)

    def __getattr__(self, name):
        return getattr(os, name)


class TestArchiveSweep(SweepCase):
    def test_aged_archive_is_reclaimed(self):
        root = self.host_dir()
        victim = self.make_zip(root, "Artist_-_Album_(deadbeef).zip", TTL + GRACE + 10)
        removed = self.main.sweep_orphaned_archives(now=self.now)
        self.assertEqual(removed, [victim])
        self.assertFalse(victim.exists())

    def test_fresh_archive_survives(self):
        root = self.host_dir()
        keeper = self.make_zip(root, "Artist_-_New_(aaaa1111).zip", 60)
        self.assertEqual(self.main.sweep_orphaned_archives(now=self.now), [])
        self.assertTrue(keeper.exists())

    def test_grace_buffer_is_applied_not_just_ttl(self):
        """Exactly TTL old is inside the grace window, so it must survive."""
        root = self.host_dir()
        edge = self.make_zip(root, "Edge_-_Case_(bbbb2222).zip", TTL + 5)
        self.assertEqual(self.main.sweep_orphaned_archives(now=self.now), [])
        self.assertTrue(edge.exists())

    def test_boundary_age_is_removed(self):
        root = self.host_dir()
        victim = self.make_zip(root, "Old_-_One_(cccc3333).zip", TTL + GRACE + 1)
        self.assertEqual(len(self.main.sweep_orphaned_archives(now=self.now)), 1)
        self.assertFalse(victim.exists())

    def test_non_zip_files_are_never_touched(self):
        root = self.host_dir()
        keep_flac = root / "notmine.flac"
        keep_flac.write_bytes(b"x")
        keep_txt = root / "notes.txt"
        keep_txt.write_text("keep me")
        for p in (keep_flac, keep_txt):
            os.utime(p, (self.now - 10_000, self.now - 10_000))
        self.assertEqual(self.main.sweep_orphaned_archives(now=self.now), [])
        self.assertTrue(keep_flac.exists())
        self.assertTrue(keep_txt.exists())

    def test_uppercase_extension_is_matched(self):
        root = self.host_dir()
        victim = self.make_zip(root, "Shouty_-_Name_(dddd4444).ZIP", TTL + GRACE + 10)
        self.assertEqual(len(self.main.sweep_orphaned_archives(now=self.now)), 1)
        self.assertFalse(victim.exists())

    def test_subdirectory_named_like_an_archive_is_kept(self):
        root = self.host_dir()
        decoy = root / "weird_-_dir_(eeee5555).zip"
        decoy.mkdir()
        (decoy / "inner.txt").write_text("inside")
        os.utime(decoy, (self.now - 10_000, self.now - 10_000))
        self.assertEqual(self.main.sweep_orphaned_archives(now=self.now), [])
        self.assertTrue(decoy.is_dir())

    def test_symlink_target_outside_the_served_dir_is_not_deleted(self):
        """A served directory is world-readable; following its symlinks out of
        scope and unlinking whatever is on the other end is not our business."""
        root = self.host_dir()
        outside = self.work / "precious.zip"
        outside.write_bytes(b"do not delete")
        os.utime(outside, (self.now - 10_000, self.now - 10_000))
        link = root / "link_-_to_(ffff6666).zip"
        link.symlink_to(outside)
        self.assertEqual(self.main.sweep_orphaned_archives(now=self.now), [])
        self.assertTrue(outside.exists())
        self.assertTrue(link.is_symlink())

    def test_disabled_auto_delete_sweeps_nothing(self):
        root = self.host_dir()
        keeper = self.make_zip(root, "Aging_-_Out_(0000aaaa).zip", 100_000)
        os.environ["AUTO_DELETE_ENABLED"] = "0"
        self.assertEqual(self.main.sweep_orphaned_archives(now=self.now), [])
        self.assertTrue(keeper.exists())

    def test_direct_telegram_mode_is_a_no_op(self):
        """Empty COPY_FILES_PATH means archives went to Telegram, not to disk."""
        os.environ["COPY_FILES_PATH"] = ""
        self.assertEqual(self.main.sweep_orphaned_archives(now=self.now), [])

    def test_missing_host_directory_does_not_raise(self):
        os.environ["COPY_FILES_PATH"] = str(self.work / "never-created")
        self.assertEqual(self.main.sweep_orphaned_archives(now=self.now), [])

    def test_one_unlinkable_file_does_not_abort_the_sweep(self):
        """Fail per entry, not per scan: a single EACCES must not strand the rest."""
        root = self.host_dir()
        blocked = self.make_zip(root, "Blocked_-_One_(1111bbbb).zip", TTL + GRACE + 10)
        good = self.make_zip(root, "Reclaimable_-_One_(2222cccc).zip", TTL + GRACE + 10)
        real_unlink = os.unlink

        def selective(path):
            if Path(path) == blocked:
                raise PermissionError(13, "Permission denied", str(path))
            return real_unlink(path)

        saved = self.main.os
        self.main.os = _OsProxy(selective)
        self.addCleanup(setattr, self.main, "os", saved)
        removed = self.main.sweep_orphaned_archives(now=self.now)
        self.assertEqual(removed, [good])
        self.assertFalse(good.exists())
        self.assertTrue(blocked.exists())

    def test_already_deleted_file_is_not_an_error(self):
        """schedule_file_deletion() can race this sweep; FileNotFoundError is
        the success case, not a failure to log."""
        root = self.host_dir()
        self.make_zip(root, "Racing_-_One_(3333dddd).zip", TTL + GRACE + 10)
        real_unlink = os.unlink

        def racing(path):
            real_unlink(path)
            raise FileNotFoundError(2, "No such file", str(path))

        saved = self.main.os
        self.main.os = _OsProxy(racing)
        self.addCleanup(setattr, self.main, "os", saved)
        self.assertEqual(self.main.sweep_orphaned_archives(now=self.now), [])


class TestStagingSweep(SweepCase):
    def stage(self, platform: str, kind: str, object_id: str, age: float) -> Path:
        path = Path(self.main.TMP_DIR) / platform / kind / object_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "01 - Artist - Title.flac").write_bytes(b"audio")
        os.utime(path, (self.now - age, self.now - age))
        return path

    def setUp(self):
        super().setUp()
        tmp = self.work / "tmp"
        for platform in ("deezer", "qobuz", "tidal", "amazon", "spotify", "apple", "yt", "sc"):
            (tmp / platform).mkdir(parents=True, exist_ok=True)
        (tmp / "deezer" / "track").mkdir(parents=True, exist_ok=True)
        (tmp / "deezer" / "album").mkdir(parents=True, exist_ok=True)
        self.main.TMP_DIR = str(tmp)
        self.idle()

    def test_idle_removes_aged_staging_dir(self):
        victim = self.stage("qobuz", "album", "12345", TTL + GRACE + 10)
        removed = self.main.sweep_orphaned_staging(now=self.now)
        self.assertEqual(removed, [victim])
        self.assertFalse(victim.exists())

    def test_idle_keeps_fresh_staging_dir(self):
        keeper = self.stage("tidal", "track", "999", 30)
        self.assertEqual(self.main.sweep_orphaned_staging(now=self.now), [])
        self.assertTrue(keeper.exists())

    def test_active_download_blocks_the_whole_scan(self):
        victim = self.stage("amazon", "album", "B01", TTL + GRACE + 10)
        self.busy(active=1)
        self.assertEqual(self.main.sweep_orphaned_staging(now=self.now), [])
        self.assertTrue(victim.exists())

    def test_queued_download_also_blocks_the_scan(self):
        """A queued slot becomes active within seconds; treating it as idle
        would delete the workdir of a promotion that has not landed yet."""
        victim = self.stage("spotify", "album", "SP1", TTL + GRACE + 10)
        self.busy(queued=1)
        self.assertEqual(self.main.sweep_orphaned_staging(now=self.now), [])
        self.assertTrue(victim.exists())

    def test_per_platform_skeleton_dirs_are_preserved(self):
        self.stage("apple", "album", "145", TTL + GRACE + 10)
        self.main.sweep_orphaned_staging(now=self.now)
        for platform in ("deezer", "qobuz", "apple"):
            self.assertTrue(
                (Path(self.main.TMP_DIR) / platform).is_dir(),
                f"{platform} skeleton was deleted; handlers expect it to exist",
            )

    def test_loose_files_at_the_tmp_root_are_ignored(self):
        stray = Path(self.main.TMP_DIR) / "stray.zip"
        stray.write_bytes(b"x")
        os.utime(stray, (self.now - 100_000, self.now - 100_000))
        self.assertEqual(self.main.sweep_orphaned_staging(now=self.now), [])
        self.assertTrue(stray.exists())

    def test_disabled_auto_delete_blocks_staging_sweep(self):
        victim = self.stage("deezer", "track", "555", TTL + GRACE + 10)
        os.environ["AUTO_DELETE_ENABLED"] = "0"
        self.assertEqual(self.main.sweep_orphaned_staging(now=self.now), [])
        self.assertTrue(victim.exists())

    def test_missing_tmp_dir_does_not_raise(self):
        self.main.TMP_DIR = str(self.work / "no-such-tmp")
        self.assertEqual(self.main.sweep_orphaned_staging(now=self.now), [])


class TestLoopWiring(SweepCase):
    """The ticker sleeps 600s before its first pass, so these exercise
    run_cleanup_sweeps() directly rather than background_cleanup()."""

    def test_pass_calls_both_sweeps_and_reports_counts(self):
        root = self.host_dir()
        self.make_zip(root, "Loop_-_Victim_(9999eeee).zip", TTL + GRACE + 10)
        tmp = self.work / "tmp"
        staged = tmp / "qobuz" / "album" / "777"
        staged.mkdir(parents=True)
        (staged / "01 - A - B.flac").write_bytes(b"audio")
        os.utime(staged, (self.now - 100_000, self.now - 100_000))
        self.main.TMP_DIR = str(tmp)
        self.idle()

        counts = _run(self.main.run_cleanup_sweeps())
        self.assertEqual(counts["archives"], 1)
        self.assertEqual(counts["staging"], 1)
        self.assertFalse(staged.exists())

    def test_pass_offloads_work_from_the_event_loop(self):
        """Both sweeps are blocking walks; they must run in a worker thread so a
        fat COPY_FILES_PATH cannot stall Telegram polling."""
        self.host_dir()
        self.main.TMP_DIR = str(self.work / "empty-tmp")
        self.idle()
        seen = {}
        real = self.main.sweep_orphaned_archives

        def spy():
            seen["thread"] = threading.current_thread().name
            seen["main_thread"] = threading.main_thread().name
            return real()

        saved = self.main.sweep_orphaned_archives
        self.main.sweep_orphaned_archives = spy
        self.addCleanup(setattr, self.main, "sweep_orphaned_archives", saved)
        _run(self.main.run_cleanup_sweeps())
        self.assertIn("thread", seen)
        self.assertNotEqual(seen["thread"], seen["main_thread"])

    def test_busy_scheduler_still_lets_the_archive_half_run(self):
        """The archive sweep only touches COPY_FILES_PATH, which no in-flight
        handler writes to, so it must not be gated on scheduler occupancy."""
        root = self.host_dir()
        victim = self.make_zip(root, "Hosted_-_Anyway_(8888dddd).zip", TTL + GRACE + 10)
        self.busy(active=2, queued=1)
        counts = _run(self.main.run_cleanup_sweeps())
        self.assertEqual(counts["archives"], 1)
        self.assertEqual(counts["staging"], 0)
        self.assertFalse(victim.exists())

    def test_real_ticker_reclaims_orphans_when_interval_elapses(self):
        """End-to-end through background_cleanup(): with the interval shrunk to
        zero the ticker performs a genuine pass and reclaims a real archive."""
        root = self.host_dir()
        victim = self.make_zip(root, "Ticker_-_Victim_(9999eeee).zip", TTL + GRACE + 10)
        self.idle()
        saved_interval = self.main.CLEANUP_INTERVAL_SECONDS
        self.main.CLEANUP_INTERVAL_SECONDS = 0
        self.addCleanup(setattr, self.main, "CLEANUP_INTERVAL_SECONDS", saved_interval)

        async def scenario():
            task = asyncio.ensure_future(self.main.background_cleanup())
            try:
                for _ in range(40):
                    if not victim.exists():
                        break
                    await asyncio.sleep(0.01)
            finally:
                task.cancel()

        _run(scenario())
        self.assertFalse(victim.exists(), "the live ticker never reclaimed the orphan")

    def test_ticker_survives_a_raising_sweep(self):
        """A raising sweep must not take the task down, or orphan reclamation
        stops permanently for the rest of the container's life. Observed by
        letting the ticker pass through an exception and then reclaim on the
        next cycle."""
        root = self.host_dir()
        calls = {"n": 0}
        real = self.main.sweep_orphaned_archives

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("sweep exploded")
            return real()

        saved = self.main.sweep_orphaned_archives
        saved_interval = self.main.CLEANUP_INTERVAL_SECONDS
        self.main.sweep_orphaned_archives = flaky
        self.main.CLEANUP_INTERVAL_SECONDS = 0
        self.addCleanup(setattr, self.main, "sweep_orphaned_archives", saved)
        self.addCleanup(setattr, self.main, "CLEANUP_INTERVAL_SECONDS", saved_interval)

        async def scenario():
            task = asyncio.ensure_future(self.main.background_cleanup())
            try:
                for _ in range(60):
                    await asyncio.sleep(0.01)
                    if calls["n"] >= 2:
                        break
            finally:
                outcome["cancelled"] = not task.done()
                task.cancel()

        outcome = {}
        _run(scenario())
        self.assertGreaterEqual(calls["n"], 2, "ticker died on the first sweep error")
        self.assertTrue(outcome["cancelled"], "task had already finished, not looping")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


if __name__ == "__main__":
    unittest.main()
