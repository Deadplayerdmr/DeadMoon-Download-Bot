# dl_utils/platform_config.py
"""Per-platform configuration read from token.env (then the environment).

Every flag is resolved lazily, on each request, rather than frozen at import
time so live edits to token.env take effect immediately without recreating the
Docker container.
"""

import json
import os
from pathlib import Path

from utils import get_bool_env, get_env_or_token_file, get_float_env, get_int_env

DEEZER = "deezer"
QOBUZ = "qobuz"
TIDAL = "tidal"
AMAZON = "amazon"
SPOTIFY = "spotify"
APPLE = "apple"
YOUTUBE = "yt"
SOUNDCLOUD = "sc"

PLATFORMS = (
    DEEZER,
    QOBUZ,
    TIDAL,
    AMAZON,
    SPOTIFY,
    APPLE,
    YOUTUBE,
    SOUNDCLOUD,
)

PLATFORM_LABELS = {
    DEEZER: "Deezer",
    QOBUZ: "Qobuz",
    TIDAL: "Tidal",
    AMAZON: "Amazon Music",
    SPOTIFY: "Spotify",
    APPLE: "Apple Music",
    YOUTUBE: "YouTube",
    SOUNDCLOUD: "SoundCloud",
}

# All platforms enabled by default for track/album links; artist discography
# downloads default ON only for Deezer and OFF for everything else.
_DEFAULT_ENABLED = {
    DEEZER: True,
    QOBUZ: True,
    TIDAL: True,
    AMAZON: True,
    SPOTIFY: True,
    APPLE: True,
    YOUTUBE: True,
    SOUNDCLOUD: True,
}

_DEFAULT_ARTIST_DOWNLOAD = {
    DEEZER: True,
    QOBUZ: False,
    TIDAL: False,
    AMAZON: False,
    SPOTIFY: False,
    APPLE: False,
    YOUTUBE: False,
    SOUNDCLOUD: False,
}

_ENV_PREFIX = {
    DEEZER: "DEEZER",
    QOBUZ: "QOBUZ",
    TIDAL: "TIDAL",
    AMAZON: "AMAZON",
    SPOTIFY: "SPOTIFY",
    APPLE: "APPLE",
    YOUTUBE: "YOUTUBE",
    SOUNDCLOUD: "SOUNDCLOUD",
}


def _env_key(platform: str, suffix: str) -> str:
    return f"{_ENV_PREFIX.get(platform, platform.upper())}_{suffix}"


def platform_enabled(platform: str) -> bool:
    return get_bool_env(
        _env_key(platform, "ENABLED"), _DEFAULT_ENABLED.get(platform, True)
    )


def artist_download_enabled(platform: str) -> bool:
    if platform != DEEZER:
        return False
    return get_bool_env("DEEZER_ARTIST_DOWNLOAD_ENABLED", True)


def artist_disabled_message(platform: str) -> str:
    return (
        "⚠️ Full artist discography downloads are only supported for Deezer links. "
        "Please send an album or track link instead."
    )


def fallback_enabled() -> bool:
    return get_bool_env("FALLBACK_ENABLED", False)


_CREDENTIAL_REQUIREMENTS = {
    DEEZER: "DEEZER_TOKEN",
    QOBUZ: "QOBUZ_USER_AUTH_TOKEN + QOBUZ_APP_ID (or QOBUZ_EMAIL + QOBUZ_PASSWORD)",
    TIDAL: "TIDAL_OAUTH_TOKEN (or TIDAL_USER_ID + TIDAL_ACCESS_TOKEN + TIDAL_REFRESH_TOKEN)",
    AMAZON: "AMAZON_DEVICE_TOKEN + AMAZON_WVD_PATH",
    SPOTIFY: "SPOTIFY_COOKIES_PATH + SPOTIFY_WVD_PATH",
    APPLE: "APPLE_COOKIES_PATH or APPLE_MEDIA_USER_TOKEN",
}

DIRECT_ENGINES = {
    DEEZER: "deezer",
    QOBUZ: "streamrip",
    TIDAL: "streamrip",
    AMAZON: "amazon-cli",
    SPOTIFY: "dotify-cli",
    APPLE: "gamdl",
    YOUTUBE: "yt-dlp",
    SOUNDCLOUD: "yt-dlp",
}


def missing_credentials_message(platform: str) -> str:
    if platform == SPOTIFY:
        configured = get_env_or_token_file("SPOTIFY_CREDENTIALS_PATH", "").strip()
        path = Path(configured).expanduser() if configured else None
        return (
            f"❌ {spotify_credentials_error(path)} Configure these paths in token.env. "
            "Spotify downloads are direct-only; no Deezer or other cross-platform "
            "fallback is used."
        )

    label = PLATFORM_LABELS.get(platform, platform)
    requirement = _CREDENTIAL_REQUIREMENTS.get(platform, "the required credentials")
    return (
        f"❌ {label} direct engine ({DIRECT_ENGINES.get(platform, 'direct')}) requires "
        f"{requirement} in token.env. Credential files must exist, be non-empty, and not "
        "be an unchanged example template. Cross-platform fallback is disabled "
        "(FALLBACK_ENABLED=0)."
    )


def platform_disabled_message(platform: str) -> str:
    label = PLATFORM_LABELS.get(platform, platform)
    return (
        f"⚠️ {label} downloads are currently disabled on this bot. "
        "Send a link for another supported platform instead."
    )


def _any_credential(*keys: str) -> bool:
    return any(get_env_or_token_file(key, "").strip() for key in keys)


def _all_credential(*keys: str) -> bool:
    return all(get_env_or_token_file(key, "").strip() for key in keys)


_MIN_ARL_LENGTH = 50


def has_deezer_credentials() -> bool:
    token = get_env_or_token_file("DEEZER_TOKEN", "").strip()
    return len(token) >= _MIN_ARL_LENGTH


def has_qobuz_credentials() -> bool:
    return (
        _all_credential("QOBUZ_USER_AUTH_TOKEN", "QOBUZ_APP_ID")
        or _all_credential("QOBUZ_USER_ID", "QOBUZ_USER_AUTH_TOKEN")
        or _all_credential("QOBUZ_USER_ID", "QOBUZ_SECRETS")
        or _all_credential("QOBUZ_EMAIL", "QOBUZ_PASSWORD")
        or _all_credential("QOBUZ_EMAIL_OR_USER_ID", "QOBUZ_PASSWORD_OR_TOKEN")
    )


def tidal_oauth_fields() -> dict:
    fields = {}
    for key, env in (
        ("user_id", "TIDAL_USER_ID"),
        ("access_token", "TIDAL_ACCESS_TOKEN"),
        ("refresh_token", "TIDAL_REFRESH_TOKEN"),
        ("token_expiry", "TIDAL_TOKEN_EXPIRY"),
    ):
        value = get_env_or_token_file(env, "").strip()
        if value:
            fields[key] = value

    blob = get_env_or_token_file("TIDAL_OAUTH_TOKEN", "").strip()
    if blob:
        parsed = _parse_oauth_blob(blob)
        for key in ("user_id", "access_token", "refresh_token", "token_expiry"):
            if key not in fields:
                value = parsed.get(key)
                if value is not None and str(value).strip():
                    fields[key] = str(value).strip()
    return fields


def _parse_oauth_blob(blob: str) -> dict:
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    for wrapper in ("tokens", "oauth", "credential", "auth"):
        inner = data.get(wrapper)
        if isinstance(inner, dict):
            data = inner
            break
    aliases = {
        "user_id": ("user_id", "userId", "user"),
        "access_token": ("access_token", "accessToken", "token"),
        "refresh_token": ("refresh_token", "refreshToken"),
        "token_expiry": ("token_expiry", "expiryTimestamp", "expiry", "expires_at"),
    }
    out = {}
    for canonical, names in aliases.items():
        for name in names:
            if name in data and data[name] not in (None, ""):
                out[canonical] = data[name]
                break
    return out


def has_tidal_credentials() -> bool:
    fields = tidal_oauth_fields()
    return bool(
        fields.get("user_id")
        and fields.get("access_token")
        and fields.get("refresh_token")
    )


_CREDENTIAL_EXAMPLES = {
    "SPOTIFY_COOKIES_PATH": "spotify_cookies.txt.example",
    "SPOTIFY_CREDENTIALS_PATH": "spotify_credentials.json.example",
    "SPOTIFY_WVD_PATH": "device.wvd.example",
    "APPLE_COOKIES_PATH": "apple_cookies.txt.example",
    "AMAZON_WVD_PATH": "device.wvd.example",
    "COOKIES_PATH": "cookies.txt.example",
    "QOBUZ_CONFIG_PATH": "qobuz_config.toml.example",
    "TIDAL_CONFIG_PATH": "tidal_config.toml.example",
}


def _credential_file_ready(key: str) -> bool:
    configured = get_env_or_token_file(key, "").strip()
    if not configured:
        return False
    path = Path(configured).expanduser()
    try:
        if path.name.endswith(".example") or not path.is_file() or path.stat().st_size == 0:
            return False
        example_name = _CREDENTIAL_EXAMPLES.get(key, path.name + ".example")
        example = Path(__file__).resolve().parent.parent / "local_resources" / example_name
        if example.is_file() and path.read_bytes() == example.read_bytes():
            return False
    except OSError:
        return False
    return True


def spotify_cookie_values(path: Path) -> dict:
    """Return Spotify web cookies without accepting bearer tokens as sp_dc."""
    from http.cookiejar import MozillaCookieJar

    try:
        jar = MozillaCookieJar(str(path))
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, ValueError):
        return {}
    return {
        cookie.name: cookie.value
        for cookie in jar
        if cookie.domain == ".spotify.com" and cookie.name == "sp_dc" and cookie.value
    }


def spotify_credentials_data(path: Path) -> dict | None:
    """Read a legacy-path JSON web bundle containing an sp_dc cookie."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    cookies = data.get("cookies")
    if isinstance(cookies, dict) and isinstance(cookies.get("sp_dc"), str):
        data = {**data, "sp_dc": cookies["sp_dc"]}
    if not isinstance(data.get("sp_dc"), str) or not data["sp_dc"].strip():
        return None
    return data


def _spotify_auth_ready() -> bool:
    cookies = get_env_or_token_file("SPOTIFY_COOKIES_PATH", "").strip()
    if cookies and _credential_file_ready("SPOTIFY_COOKIES_PATH"):
        if spotify_cookie_values(Path(cookies).expanduser()).get("sp_dc"):
            return True
    legacy = get_env_or_token_file("SPOTIFY_CREDENTIALS_PATH", "").strip()
    return bool(
        legacy
        and _credential_file_ready("SPOTIFY_CREDENTIALS_PATH")
        and spotify_credentials_data(Path(legacy).expanduser())
    )


def spotify_credentials_error(path: Path | None = None) -> str:
    """Explain the required web session and WVD without exposing secret values."""
    if path is not None and path.is_file() and spotify_credentials_data(path) is None:
        detail = f"SPOTIFY: {path} is not a supported web-session bundle; "
    else:
        detail = "SPOTIFY: "
    return (
        detail
        + "dotify-cli requires SPOTIFY_COOKIES_PATH (a Netscape cookies file with the "
        "Netscape header and a valid .spotify.com sp_dc cookie) and SPOTIFY_WVD_PATH "
        "(a provisioned Widevine "
        ".wvd). Legacy Librespot AP credentials and SPOTIFY_OAUTH_TOKEN bearer tokens "
        "cannot replace them. See local_resources/README.md."
    )


def has_amazon_credentials() -> bool:
    return bool(
        get_env_or_token_file("AMAZON_DEVICE_TOKEN", "").strip()
        and _credential_file_ready("AMAZON_WVD_PATH")
    )


def has_spotify_credentials() -> bool:
    return _spotify_auth_ready() and _credential_file_ready("SPOTIFY_WVD_PATH")


def has_apple_credentials() -> bool:
    return _credential_file_ready("APPLE_COOKIES_PATH") or _any_credential(
        "APPLE_MEDIA_USER_TOKEN"
    )


_HAS_CREDENTIALS = {
    DEEZER: has_deezer_credentials,
    QOBUZ: has_qobuz_credentials,
    TIDAL: has_tidal_credentials,
    AMAZON: has_amazon_credentials,
    SPOTIFY: has_spotify_credentials,
    APPLE: has_apple_credentials,
    YOUTUBE: lambda: True,
    SOUNDCLOUD: lambda: True,
}


def has_credentials(platform: str) -> bool:
    check = _HAS_CREDENTIALS.get(platform)
    return bool(check and check())


def platform_status(platform: str) -> dict:
    return {
        "platform": platform,
        "label": PLATFORM_LABELS.get(platform, platform),
        "enabled": platform_enabled(platform),
        "has_credentials": has_credentials(platform),
        "engine": DIRECT_ENGINES.get(platform, "direct"),
        "artist_download": artist_download_enabled(platform) if platform == DEEZER else None,
    }


def flac_enabled() -> bool:
    return get_bool_env("ENABLE_FLAC", True)


def max_retries(default: int = 5) -> int:
    return get_int_env("MAX_RETRIES", default)


def max_artist_releases() -> int:
    value = get_int_env("MAX_ARTIST_RELEASES", 100)
    return value if value > 0 else 100


def artist_api_delay() -> float:
    value = get_float_env("ARTIST_API_DELAY", 0.6)
    return value if value >= 0 else 0.0


def auto_delete_enabled() -> bool:
    return get_bool_env("AUTO_DELETE_ENABLED", True)


def auto_delete_seconds() -> int:
    value = get_int_env("AUTO_DELETE_SECONDS", 1800)
    return value if value > 0 else 1800


def copy_files_path() -> str:
    return get_env_or_token_file("COPY_FILES_PATH", "").strip()


def file_link_template() -> str:
    return get_env_or_token_file("FILE_LINK_TEMPLATE", "").strip()


def output_format() -> str:
    return get_env_or_token_file("FORMAT", "").strip().lower()
