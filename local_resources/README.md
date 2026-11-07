# Local credential resources

Compose mounts this directory read-only at `/usr/src/app/local_resources` inside the
container. Put real files here, keep their permissions private, and point `token.env` at the
container paths below. The `.example` files contain placeholders, not usable credentials.
`/status` reports `no creds` for a missing, empty, or unchanged example file.

```dotenv
SPOTIFY_COOKIES_PATH=/usr/src/app/local_resources/spotify_cookies.txt
SPOTIFY_WVD_PATH=/usr/src/app/local_resources/device.wvd
SPOTIFY_CREDENTIALS_PATH=/usr/src/app/local_resources/spotify_credentials.json
APPLE_COOKIES_PATH=/usr/src/app/local_resources/apple_cookies.txt
AMAZON_WVD_PATH=/usr/src/app/local_resources/device.wvd
COOKIES_PATH=/usr/src/app/local_resources/cookies.txt
```

Do not commit real credential files to source control. The `.gitignore` rule ignores real
resources but explicitly unignores `local_resources/*.example` so every template is tracked.
A release archive made explicitly for private deployment can contain the populated files;
protect that archive as carefully as the original credentials.

## Spotify — `spotify_cookies.txt` and `device.wvd` for dotify-cli

Spotify audio uses `dotify-cli` 3.0.6 with `--session-type web` and direct Widevine
license/decryption. It does not use Zotify or a Librespot audio session. Two independent
inputs are required:

1. While logged in to an authorized account at <https://open.spotify.com>, export browser
   cookies in **Netscape HTTP Cookie File** format. Save the file as
   `local_resources/spotify_cookies.txt`. It must retain the
   `# Netscape HTTP Cookie File` header and contain a `.spotify.com` `sp_dc` cookie.
   Use a cookie from a **Spotify Premium** web session to receive 256 kbps AAC or FLAC
   where Spotify and the release offer those formats. Free-tier `sp_dc` cookies are capped
   by Spotify at 128 kbps AAC. `spotify_cookies.txt.example` shows the seven tab-separated
   fields. The bot stages a private `0600` copy in the request workdir and runs dotify with
   stdin disconnected.
2. Export a Widevine device using your authorized provisioning tooling and save the binary
   as `local_resources/device.wvd`. Point `SPOTIFY_WVD_PATH` at that file. L3 can be
   sufficient when Spotify and the release permit it, but quality is always whatever native
   stream the account/device can actually access. This repository does not provision WVDs.

As a compatibility adapter only, `SPOTIFY_CREDENTIALS_PATH` may point to JSON containing
`{"sp_dc": "..."}` (or a `cookies` object with that field). It is converted to a private
Netscape cookie file. An old Librespot AP JSON containing `username`, `auth_type`, and
`auth_data` is not accepted. `SPOTIFY_OAUTH_TOKEN` and other Web API bearer tokens cannot
replace `sp_dc`. Both the web session and WVD must be usable before `/status` reports
`creds ✓`.

Spotify never uses the optional Deezer fallback, even when `FALLBACK_ENABLED=1` for other
platforms. Native AAC (`.m4a`), Vorbis (`.ogg`), and FLAC (`.flac`) are remuxed with
FFmpeg stream copy, never transcoded to MP3 or fake FLAC. Public embed metadata identifies
unplayable album tracks; those are skipped and included in the delivery's `Failures:`
summary while all playable tracks continue.

## `apple_cookies.txt` — Gamdl

The account must have an active Apple Music subscription. While signed in at
<https://music.apple.com>, export cookies with a browser extension or browser tool that
writes the **Netscape HTTP Cookie File** format. Save the export as
`local_resources/apple_cookies.txt`. It must include the `.apple.com` `media-user-token`
cookie. `apple_cookies.txt.example` shows the header and seven tab-separated fields.

Alternatively, leave `APPLE_COOKIES_PATH` empty and set `APPLE_MEDIA_USER_TOKEN` directly in
`token.env`. The bot creates a temporary Netscape file with mode `0600` for Gamdl.

## `cookies.txt` — yt-dlp

For YouTube or SoundCloud resources that require an authenticated browser session, export
cookies in Netscape HTTP Cookie File format and save them as
`local_resources/cookies.txt`. Keep `COOKIES_PATH` pointed at the container path above.
Leave this file absent when the public resources you request do not need cookies. Use only
sessions and content you are authorized to access.

## `device.wvd` — Spotify and Amazon Music

When using an Amazon Music downloader that accepts a Widevine device file, generate or
export a `.wvd` device with that downloader's own provisioning tooling, then save it as
`local_resources/device.wvd`. Set `SPOTIFY_WVD_PATH` for Spotify. For Amazon, also set
`AMAZON_DEVICE_TOKEN` and `AMAZON_WVD_PATH`, and set `AMAZON_CLI` if the executable is not
named `amazon-music-downloader`. Only share one WVD between services when your authorized
provisioning workflow explicitly supports that reuse.

A WVD is device-specific cryptographic material. Do not download an unknown device file,
share it publicly, or treat an empty/placeholder file as a valid provisioned device. This
repository does not provision Widevine devices. `device.wvd.example` is documentation only;
renaming it does not create a provisioned device.

## Qobuz and Tidal — streamrip templates

`qobuz_config.toml.example` and `tidal_config.toml.example` document streamrip's `[qobuz]`
and `[tidal]` authentication blocks and their corresponding `token.env` variables. They are
references, not files loaded by the bot: the engine creates a private per-download config
from `QOBUZ_*` and `TIDAL_*`. Do not put live tokens in either `.example` file.

For Qobuz, use `QOBUZ_USER_ID` + `QOBUZ_USER_AUTH_TOKEN` for token authentication, or
`QOBUZ_EMAIL` + `QOBUZ_PASSWORD` for password authentication, with the app ID/secrets your
authorized setup requires. For Tidal, use `TIDAL_USER_ID`, `TIDAL_ACCESS_TOKEN`,
`TIDAL_REFRESH_TOKEN`, and optionally `TIDAL_TOKEN_EXPIRY` and `TIDAL_COUNTRY_CODE`.

## Template checklist

Copy a template to the live filename only after replacing every placeholder:

- `spotify_cookies.txt.example` → `spotify_cookies.txt`
- `spotify_credentials.json.example` → optional `spotify_credentials.json` web adapter
- `apple_cookies.txt.example` → `apple_cookies.txt`
- `cookies.txt.example` → `cookies.txt`
- `device.wvd.example` → replace with a provisioned binary `device.wvd`
- `qobuz_config.toml.example` and `tidal_config.toml.example` → map values into `token.env`

Every unchanged template is treated as unconfigured, including copies under a live filename.
