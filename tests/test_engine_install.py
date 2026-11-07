"""Offline checks for the direct-engine engine install: imports, CLI entry points, pins.

The engine dependency graph in requirements.txt + the Dockerfile overrides three
declared version caps (streamrip's Pillow/tomlkit/rich, pyplayready's
cryptography) with --no-deps installs. Overriding a cap is
only safe while the newer version actually runs the code, so this module asserts
that at the call sites the bot really uses rather than at import level alone.

Run with `python3 -m unittest discover tests`. No network, no Telegram, no
credentials: nothing here performs a download.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_TOKEN", "123456:TEST")


def _have(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _cli(name: str) -> str | None:
    """Resolve an engine CLI the same way dl_utils.provider_engines._which does."""
    found = shutil.which(name)
    if found:
        return found
    local = Path(__file__).resolve().parent.parent / "bin" / name
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    return None


def _runs(cli: str) -> tuple[bool, str]:
    """`--help` exits 0 only if the whole entry-point import chain loads."""
    try:
        proc = subprocess.run(
            [cli, "--help"],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as e:
        return False, str(e)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return False, f"exit {proc.returncode}: {' / '.join(tail)}"
    return True, ""


class TestEngineEntryPoints(unittest.TestCase):
    """Every engine the bot can dispatch to must actually start."""

    ENGINES = ("rip", "gamdl", "dotify")

    def test_installed_engines_start(self):
        missing = [n for n in self.ENGINES if _cli(n) is None]
        if len(missing) == len(self.ENGINES):
            self.skipTest("no direct-engine engines installed in this environment")
        for name in self.ENGINES:
            if name in missing:
                continue
            with self.subTest(engine=name):
                ok, err = _runs(_cli(name))
                # A half-installed engine is worse than an absent one: the
                # dispatcher picks it and every request for that platform dies
                # at exec time instead of falling back.
                self.assertTrue(ok, f"{name} --help failed: {err}")

    def test_streamrip_config_api(self):
        if not _have("streamrip"):
            self.skipTest("streamrip is not installed in this environment")
        from streamrip.config import Config, set_user_defaults  # noqa: F401


@unittest.skipUnless(_have("streamrip"), "streamrip is not installed")
class TestStreamripOverrides(unittest.TestCase):
    """streamrip caps Pillow<11 and tomlkit<0.8; the bot runs newer. Prove it."""

    def test_artwork_downscale_on_modern_pillow(self):
        from PIL import Image

        from streamrip.media.artwork import downscale_image

        path = Path(self.tmp.name) / "cover.jpg"  # type: ignore[attr-defined]
        Image.new("RGB", (1400, 1400), (10, 20, 30)).save(path, "JPEG")
        downscale_image(str(path), 1000)
        with Image.open(path) as im:
            self.assertEqual(max(im.size), 1000)

    def test_config_written_by_tomlkit_13_round_trips(self):
        """provider_engines._streamrip_config writes with tomlkit >=0.13."""
        import tempfile

        import tomlkit
        from streamrip.config import Config, set_user_defaults

        d = Path(tempfile.mkdtemp(prefix="deadmoon-sr-"))
        cfg_path = d / "config.toml"
        set_user_defaults(str(cfg_path))

        doc = tomlkit.parse(cfg_path.read_text())
        doc["downloads"]["folder"] = str(d)
        doc["downloads"]["requests_per_minute"] = 30
        doc["qobuz"]["quality"] = 3
        doc["qobuz"]["use_auth_token"] = True
        doc["qobuz"]["email_or_userid"] = "12345"
        doc["qobuz"]["password_or_token"] = "secret"
        doc["qobuz"]["app_id"] = "9999"
        doc["qobuz"]["secrets"] = ["aaa", "bbb"]
        cfg_path.write_text(doc.as_string())

        loaded = Config(str(cfg_path)).session
        self.assertEqual(loaded.qobuz.quality, 3)
        self.assertTrue(loaded.qobuz.use_auth_token)
        self.assertEqual(loaded.qobuz.app_id, "9999")
        self.assertEqual(loaded.qobuz.secrets, ["aaa", "bbb"])
        self.assertEqual(loaded.downloads.requests_per_minute, 30)

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory(prefix="deadmoon-sr-art-")
        self.addCleanup(self.tmp.cleanup)


@unittest.skipUnless(_have("pyplayready"), "pyplayready is not installed")
class TestPlayReadyOverrides(unittest.TestCase):
    """Verify pyplayready with the explicitly pinned cryptography runtime."""

    def test_wrapping_key_and_aes_keywrap_round_trip(self):
        from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, aes_key_wrap
        from pyplayready.crypto.key_wrap import derive_wrapping_key

        root = derive_wrapping_key()
        self.assertEqual(len(root), 16)

        content_key = b"0123456789abcdef0123456789abcdef"
        wrapped = aes_key_wrap(root, content_key)
        self.assertNotEqual(wrapped, content_key)
        self.assertEqual(aes_key_unwrap(root, wrapped), content_key)

    def test_revocation_list_module_loads(self):
        """The other cryptography consumer in pyplayready (rsa/hashes)."""
        from pyplayready.misc import revocation_list  # noqa: F401


@unittest.skipUnless(_have("pywidevine"), "pywidevine is not installed")
class TestWidevineProtobuf(unittest.TestCase):
    """Dotify and Widevine share the modern protobuf runtime."""

    def test_license_protocol_messages_serialise(self):
        from pywidevine.license_protocol_pb2 import License, LicenseRequest, SignedMessage

        req = LicenseRequest()
        req.type = 1
        req.request_time = 1730000000
        blob = req.SerializeToString()
        back = LicenseRequest()
        back.ParseFromString(blob)
        self.assertEqual(back.request_time, 1730000000)

        for msg in (SignedMessage(), License()):
            self.assertTrue(msg.DESCRIPTOR.fields)


class TestDotifyWidevineRuntime(unittest.TestCase):
    def test_modern_protobuf_and_quality_priority(self):
        import google.protobuf as protobuf
        from dotify.interface.enums import AudioQuality

        self.assertTrue(protobuf.__version__.startswith("6.33."))
        self.assertEqual(AudioQuality.FLAC_MP4_24.value, "flac-mp4-24")
        self.assertEqual(AudioQuality.AAC_HIGH.actual_file_format, "m4a")
        self.assertEqual(AudioQuality.VORBIS_HIGH.actual_file_format, "ogg")

    def test_ffmpeg_remux_is_stream_copy(self):
        import inspect
        from dotify.downloader.audio import SpotifyAudioDownloader

        remux = inspect.getsource(SpotifyAudioDownloader._ffmpeg_remux)
        self.assertIn('"copy"', remux)
        self.assertNotIn('"-c:a"', remux)


class TestRequirementsPinsAreConsistent(unittest.TestCase):
    """requirements.txt must not list a package the Dockerfile installs --no-deps.

    Duplicating an engine wheel here would put its stale caps back in front of
    the resolver and make `pip install -r requirements.txt` unsatisfiable again.
    """

    NO_DEPS = ("streamrip", "gamdl", "pyplayready", "dotify-cli")

    def test_engines_absent_from_requirements(self):
        text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = line.split("==")[0].split(">=")[0].split("<")[0].strip().lower()
            with self.subTest(requirement=line):
                self.assertNotIn(
                    name,
                    [n.lower() for n in self.NO_DEPS],
                    f"{name} is installed --no-deps by the Dockerfile and must "
                    "not also be resolved from requirements.txt",
                )

    def test_modern_protobuf_is_pinned_in_requirements(self):
        text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
        self.assertIn("protobuf==6.33.6", text)
        self.assertNotIn("protobuf==3.20.1", text)


if __name__ == "__main__":
    unittest.main()
