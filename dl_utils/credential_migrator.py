"""Move multi-field credentials from token.env into private resource files."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from utils import get_env_or_token_file, update_token_env_keys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_RESOURCES = PROJECT_ROOT / "local_resources"
CONTAINER_RESOURCES = Path("/usr/src/app/local_resources")
TIDAL_CONTAINER_PATH = str(CONTAINER_RESOURCES / "tidal_config.toml")
QOBUZ_CONTAINER_PATH = str(CONTAINER_RESOURCES / "qobuz_config.toml")
APPLE_CONTAINER_PATH = str(CONTAINER_RESOURCES / "apple_cookies.txt")


def resolve_resource_path(configured: str | os.PathLike) -> Path:
    """Resolve Docker resource paths to the checkout when running on the host."""
    path = Path(configured).expanduser()
    try:
        relative = path.relative_to(CONTAINER_RESOURCES)
    except ValueError:
        return path
    candidate = LOCAL_RESOURCES / relative
    if not path.exists() and candidate.exists():
        return candidate
    return path


def atomic_write_text(path: str | os.PathLike, text: str) -> Path:
    """Atomically replace a credential file with owner-only permissions."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return destination


def _toml_document(path: Path):
    try:
        from tomlkit import document, parse
    except ImportError:
        return None

    try:
        return parse(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return document()


def _stdlib_toml_section(path: Path, section: str) -> dict:
    try:
        with path.open("rb") as handle:
            values = tomllib.load(handle).get(section, {})
        return dict(values) if isinstance(values, dict) else {}
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return {}


def read_toml_section(path: str | os.PathLike, section: str) -> dict:
    destination = Path(path)
    doc = _toml_document(destination)
    if doc is None:
        return _stdlib_toml_section(destination, section)
    values = doc.get(section, {})
    return dict(values) if values else {}


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"Unsupported credential TOML value: {type(value).__name__}")


def _write_stdlib_toml_section(destination: Path, section: str, values: dict) -> Path:
    merged = _stdlib_toml_section(destination, section)
    merged.update(values)
    lines = [f"[{section}]", ""]
    lines.extend(f"{key} = {_toml_value(value)}" for key, value in merged.items())
    return atomic_write_text(destination, "\n".join(lines) + "\n")


def write_toml_section(
    path: str | os.PathLike, section: str, values: dict
) -> Path:
    destination = Path(path)
    doc = _toml_document(destination)
    if doc is None:
        return _write_stdlib_toml_section(destination, section, values)

    from tomlkit import table

    current = doc.get(section)
    if current is None:
        current = table()
        doc[section] = current
    for key, value in values.items():
        current[key] = value
    return atomic_write_text(destination, doc.as_string())


def configured_resource_path(key: str, default_name: str) -> Path:
    configured = get_env_or_token_file(key, "").strip()
    return resolve_resource_path(configured or LOCAL_RESOURCES / default_name)


def write_tidal_credentials(fields: dict, path: str | os.PathLike | None = None) -> Path:
    from dl_utils import platform_config as pcfg

    claims = pcfg.decode_tidal_jwt_claims(str(fields.get("access_token") or ""))
    merged = {k: v for k, v in fields.items() if v not in (None, "")}
    for key in ("user_id", "token_expiry", "country_code"):
        if claims.get(key):
            merged[key] = claims[key]
    destination = Path(path) if path else configured_resource_path(
        "TIDAL_CONFIG_PATH", "tidal_config.toml"
    )
    values = {
        "user_id": str(merged.get("user_id") or ""),
        "token_type": "Bearer",
        "access_token": str(merged.get("access_token") or ""),
        "refresh_token": str(merged.get("refresh_token") or ""),
        "token_expiry": str(merged.get("token_expiry") or ""),
        "country_code": str(merged.get("country_code") or "US"),
    }
    return write_toml_section(destination, "tidal", values)


def write_qobuz_credentials(fields: dict, path: str | os.PathLike | None = None) -> Path:
    destination = Path(path) if path else configured_resource_path(
        "QOBUZ_CONFIG_PATH", "qobuz_config.toml"
    )
    secrets = fields.get("secrets") or []
    if isinstance(secrets, str):
        secrets = [item.strip() for item in secrets.split(",") if item.strip()]
    values = {
        "use_auth_token": bool(fields.get("use_auth_token", True)),
        "email_or_userid": str(fields.get("email_or_userid") or ""),
        "password_or_token": str(fields.get("password_or_token") or ""),
        "app_id": str(fields.get("app_id") or ""),
        "secrets": list(secrets),
    }
    return write_toml_section(destination, "qobuz", values)


def apple_cookie_text(media_user_token: str) -> str:
    lines = ["# Netscape HTTP Cookie File", "# Generated by DeadMoon /authenticateapple"]
    for domain, include_subdomains in (
        (".apple.com", "TRUE"),
        ("music.apple.com", "FALSE"),
        (".music.apple.com", "TRUE"),
    ):
        lines.append(
            f"{domain}\t{include_subdomains}\t/\tTRUE\t0\tmedia-user-token\t{media_user_token}"
        )
    return "\n".join(lines) + "\n"


def write_apple_cookies(
    media_user_token: str, path: str | os.PathLike | None = None
) -> Path:
    destination = Path(path) if path else configured_resource_path(
        "APPLE_COOKIES_PATH", "apple_cookies.txt"
    )
    return atomic_write_text(destination, apple_cookie_text(media_user_token))


def parse_tidal_auth_input(raw: str) -> dict:
    """Parse an OAuth JSON object, key=value list, or bare JWT access token."""
    from dl_utils import platform_config as pcfg

    raw = raw.strip()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        fields = pcfg._parse_oauth_blob(raw)
        for wrapper in ("tokens", "oauth", "credential", "auth"):
            if isinstance(data.get(wrapper), dict):
                data = data[wrapper]
                break
        fields["country_code"] = data.get("country_code") or data.get("countryCode") or ""
        fields["token_type"] = data.get("token_type") or data.get("tokenType") or "Bearer"
    else:
        fields = {}
        for part in re.split(r"[;\n,]+|\s+(?=[A-Za-z_][A-Za-z0-9_]*=)", raw):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            aliases = {
                "userId": "user_id",
                "accessToken": "access_token",
                "refreshToken": "refresh_token",
                "expiry": "token_expiry",
                "expires_at": "token_expiry",
                "countryCode": "country_code",
            }
            key = aliases.get(key.strip(), key.strip())
            if key in {
                "user_id", "access_token", "refresh_token", "token_expiry",
                "country_code", "token_type",
            }:
                fields[key] = value.strip().strip('"')
        if not fields and raw.count(".") == 2:
            fields["access_token"] = raw
    fields.setdefault("token_type", "Bearer")
    claims = pcfg.decode_tidal_jwt_claims(str(fields.get("access_token") or ""))
    for key in ("user_id", "token_expiry", "country_code", "client_id"):
        if not fields.get(key) and claims.get(key):
            fields[key] = claims[key]
    return fields


def _is_example(path: Path, example_name: str) -> bool:
    example = LOCAL_RESOURCES / example_name
    try:
        return path.name.endswith(".example") or (
            example.is_file() and path.read_bytes() == example.read_bytes()
        )
    except OSError:
        return False


def migrate_inline_credentials_to_local_resources() -> dict[str, Path]:
    """Migrate each complete inline credential set once; never print secrets."""
    from dl_utils import platform_config as pcfg

    migrated: dict[str, Path] = {}

    tidal_inline = any(
        get_env_or_token_file(key, "").strip()
        for key in (
            "TIDAL_OAUTH_TOKEN", "TIDAL_ACCESS_TOKEN", "TIDAL_REFRESH_TOKEN", "TIDAL_USER_ID"
        )
    )
    if tidal_inline:
        fields = pcfg.tidal_oauth_fields()
        if fields.get("access_token"):
            path = write_tidal_credentials(
                fields, LOCAL_RESOURCES / "tidal_config.toml"
            )
            update_token_env_keys({
                "TIDAL_CONFIG_PATH": TIDAL_CONTAINER_PATH,
                "TIDAL_USER_ID": "",
                "TIDAL_ACCESS_TOKEN": "",
                "TIDAL_REFRESH_TOKEN": "",
                "TIDAL_TOKEN_EXPIRY": "",
                "TIDAL_OAUTH_TOKEN": "",
            })
            migrated["tidal"] = path

    qobuz_inline = any(
        get_env_or_token_file(key, "").strip()
        for key in (
            "QOBUZ_USER_AUTH_TOKEN", "QOBUZ_EMAIL", "QOBUZ_PASSWORD",
            "QOBUZ_APP_ID", "QOBUZ_SECRETS",
        )
    )
    if qobuz_inline:
        fields = pcfg.qobuz_fields()
        path = write_qobuz_credentials(fields, LOCAL_RESOURCES / "qobuz_config.toml")
        update_token_env_keys({
            "QOBUZ_CONFIG_PATH": QOBUZ_CONTAINER_PATH,
            "QOBUZ_USER_AUTH_TOKEN": "",
            "QOBUZ_EMAIL": "",
            "QOBUZ_PASSWORD": "",
            "QOBUZ_APP_ID": "",
            "QOBUZ_SECRETS": "",
            "QOBUZ_USER_ID": "",
        })
        migrated["qobuz"] = path

    apple_token = get_env_or_token_file("APPLE_MEDIA_USER_TOKEN", "").strip()
    if apple_token:
        path = LOCAL_RESOURCES / "apple_cookies.txt"
        if not path.is_file() or path.stat().st_size == 0 or _is_example(
            path, "apple_cookies.txt.example"
        ):
            write_apple_cookies(apple_token, path)
            update_token_env_keys({
                "APPLE_COOKIES_PATH": APPLE_CONTAINER_PATH,
                "APPLE_MEDIA_USER_TOKEN": "",
            })
            migrated["apple"] = path

    return migrated


def main() -> int:
    migrated = migrate_inline_credentials_to_local_resources()
    for platform, path in migrated.items():
        print(f"[credentials] migrated {platform} credentials to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
