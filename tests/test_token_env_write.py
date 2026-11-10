"""Offline checks for the /authenticatedeezer write path in main.set_deezer_token.

This is the only place the bot rewrites a credentials file at runtime, so it
carries two contracts that are easy to break quietly:

  * a freshly created token.env must not land world-readable with the ARL in it;
  * an existing token.env must keep the permissions the operator set, because in
    docker-compose it is a bind mount and a chmod here would mutate the host file.

Run with `python3 -m unittest discover tests`. No network, no Telegram.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")


def _main_importable() -> bool:
    """main imports handlers.deezer, which pulls pycryptodome via deezer_download."""
    return importlib.util.find_spec("Crypto") is not None


def _original_token_env() -> Path:
    import utils

    return utils.TOKEN_ENV_PATH


def _set_token_env_path(value) -> None:
    """Point config resolution at a nonexistent file so the environment wins.

    Same trick tests/test_platform_config.py uses: read_token_env_value()
    resolves utils.TOKEN_ENV_PATH on every call, so reassigning it is enough to
    make the whole process ignore any real token.env on disk.
    """
    import utils

    utils.TOKEN_ENV_PATH = value


ARL = "A" * 64


@unittest.skipUnless(_main_importable(), "pycryptodome is not installed")
class TestSetDeezerToken(unittest.TestCase):
    """Importing main is not side-effect free, so bracket it with a hermetic harness.

    main.py sys.exit(1)s at import when ADMIN_ID resolves to 0 -- and unittest
    discovery runs every class in one process, so an unguarded exit takes the
    whole suite down rather than failing this class alone. It also creates
    DATA_DIR/"whitelist.json" relative to the CWD, which without a chdir would
    write a throwaway whitelist over the repository's real one.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._saved_cwd = os.getcwd()
        cls._saved_token_env = _original_token_env()
        os.chdir(cls._tmp.name)
        # Resolve ADMIN_ID/TELEGRAM_TOKEN from the environment, never the repo's
        # real token.env, so the import is deterministic on a fresh clone.
        _set_token_env_path(Path(cls._tmp.name) / "absent-token.env")
        os.environ["ADMIN_ID"] = "4242"
        try:
            import main

            cls.main = main
        except SystemExit as e:
            raise unittest.SkipTest(
                f"main exited at import (code {e.code}); "
                "ADMIN_ID/TELEGRAM_TOKEN isolation failed"
            ) from e

    @classmethod
    def tearDownClass(cls):
        _set_token_env_path(cls._saved_token_env)
        os.chdir(cls._saved_cwd)
        cls._tmp.cleanup()
        sys.modules.pop("main", None)

    def setUp(self):
        self._tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp2.cleanup)
        self.path = Path(self._tmp2.name) / "token.env"
        self._saved = self.main.TOKEN_ENV_PATH
        self.main.TOKEN_ENV_PATH = str(self.path)
        self.addCleanup(setattr, self.main, "TOKEN_ENV_PATH", self._saved)
        self._saved_env = os.environ.get("DEEZER_TOKEN")
        self.addCleanup(
            lambda: os.environ.__setitem__("DEEZER_TOKEN", self._saved_env)
            if self._saved_env is not None
            else os.environ.pop("DEEZER_TOKEN", None)
        )

    def _write(self, text: str, mode: int = 0o644):
        self.path.write_text(text, encoding="utf-8")
        os.chmod(self.path, mode)

    # ---------------- mode handling ----------------

    def test_fresh_file_is_created_owner_only(self):
        self.assertTrue(self.main.set_deezer_token(ARL))
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_existing_file_permissions_are_not_mutated(self):
        """token.env is bind-mounted; chmod'ing it would change the host file."""
        self._write("OTHER=1\n", mode=0o604)
        self.assertTrue(self.main.set_deezer_token(ARL))
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o604")

    def test_token_is_present_after_create(self):
        self.main.set_deezer_token(ARL)
        self.assertIn(f"DEEZER_TOKEN={ARL}", self.path.read_text())

    # ---------------- content handling ----------------

    def test_existing_key_is_replaced_in_place(self):
        self._write("# header\nDEEZER_TOKEN=old\nOTHER=keep\n")
        self.assertTrue(self.main.set_deezer_token(ARL))
        text = self.path.read_text()
        self.assertIn(f"DEEZER_TOKEN={ARL}", text)
        self.assertNotIn("old", text)
        self.assertIn("OTHER=keep", text)
        self.assertIn("# header", text)

    def test_missing_key_is_inserted_after_leading_comments(self):
        self._write("# a\n# b\nTELEGRAM_TOKEN=x:y\n")
        self.main.set_deezer_token(ARL)
        lines = self.path.read_text().splitlines()
        self.assertEqual(lines[:2], ["# a", "# b"])
        self.assertEqual(lines[2], f"DEEZER_TOKEN={ARL}")
        self.assertEqual(lines[3], "TELEGRAM_TOKEN=x:y")

    def test_last_line_without_newline_does_not_swallow_the_key(self):
        self._write("FOO=bar")  # no trailing newline
        self.main.set_deezer_token(ARL)
        lines = self.path.read_text().splitlines()
        self.assertIn("FOO=bar", lines)
        self.assertIn(f"DEEZER_TOKEN={ARL}", lines)

    def test_empty_file_is_handled(self):
        self._write("")
        self.assertTrue(self.main.set_deezer_token(ARL))
        self.assertEqual(self.path.read_text().strip(), f"DEEZER_TOKEN={ARL}")

    # ---------------- process sync ----------------

    def test_environment_is_updated_so_the_new_arl_takes_effect(self):
        """init_deezer_session() reads DEEZER_TOKEN from the environment, so a
        file-only write would leave the running process on the old ARL."""
        os.environ.pop("DEEZER_TOKEN", None)
        self.main.set_deezer_token(ARL)
        self.assertEqual(os.environ.get("DEEZER_TOKEN"), ARL)

    # ---------------- failure modes ----------------

    def test_unreadable_file_returns_false_not_raises(self):
        """A missing file is created; an unwritable directory must fail closed."""
        os.chmod(self._tmp2.name, 0o500)
        self.addCleanup(os.chmod, self._tmp2.name, 0o700)
        self.assertFalse(self.main.set_deezer_token(ARL))


if __name__ == "__main__":
    unittest.main()
