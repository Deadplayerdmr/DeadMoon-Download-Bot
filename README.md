# 🌙 DeadMoon Download Bot V3

A private, whitelisted Telegram bot that acquires audio from streaming platforms and returns it in its **original uncompressed/native audio format** by default (`FORMAT=audio`), or as a `.zip` archive / hosted link when `FORMAT=zip` (or whenever `COPY_FILES_PATH` is set).

Fetched streams are staged in an isolated temporary directory, delivered with exact on-disk file size metadata, handed to an external acoustic analysis pipeline, and automatically purged from both Telegram and local storage on a configurable timer (`AUTO_DELETE_SECONDS`, default 1800s). Nothing is retained permanently.

Derived from [kernoeb/Telegramusic](https://github.com/kernoeb/Telegramusic) and [kmille/deezer-downloader](https://github.com/kmille/deezer-downloader), extended with a multi-platform router, direct acquisition engines, automatic credential offloading, and interactive Telegram administrator authentication.

For the internal design — request lifecycle, module boundaries, concurrency invariants, and how to add a provider — see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## ✨ What's New in V3

- **Uncompressed Native Audio Default (`FORMAT=audio`):** Tracks, albums, and discographies are uploaded directly to Telegram as native audio attachments (`.flac`, `.m4a`, `.mp3`, `.ogg`, `.opus`, `.wav`) with embedded artwork and metadata. Set `FORMAT=zip` (or configure `COPY_FILES_PATH`) to package releases into single/multipart `.zip` archives or hosted links.
- **Exact File & Archive Size Reporting:** Every track, album, single `.zip`, multipart `.zip`, and hosted link message includes the exact on-disk payload size (`📦 Size: 34.2 MB` / `📦 Archive Size: 142.8 MB (.zip)`).
- **Interactive Admin `/authenticate*` Suite:** Link a Tidal TV device directly from Telegram via `link.tidal.com` (`/authenticatetidal`), auto-discover Qobuz app secrets (`/authenticateqobuz`), rotate Deezer ARLs (`/authenticatedeezer`), or save Apple Music and SoundCloud tokens without SSHing into the server.
- **Automatic Credential Offloading to `local_resources/`:** Multi-field credentials for Tidal (`tidal_config.toml`), Qobuz (`qobuz_config.toml`), and Apple Music (`apple_cookies.txt`) are automatically migrated out of `token.env` into `0600` owner-only files under `local_resources/`.
- **Per-Platform Artist Discography Toggles:** All 8 supported platforms have individual `<PLATFORM>_ARTIST_DOWNLOAD_ENABLED` switches in `token.env` (all `0` by default to prevent accidental full-catalogue pulls).
- **Public vs. Admin Status Separation:** `/start` and `/help` display a clean public availability checklist (`✅` / `▫️`), while engine names, credential presence, queue depth, and active user IDs remain strictly inside the admin-only `/status` report.
- **Kinetic DeadMoon Boot Engine:** 12-frame ANSI ASCII boot animation on interactive terminals (`./update.sh` and TTY sessions) with automatic single-frame log-safe fallback inside detached Docker logs.

---

## 🎧 Supported Platforms

| Platform | Track | Album | Artist | Direct Engine |
|---|:---:|:---:|:---:|---|
| **Deezer** | yes | yes | opt-in | Bundled `deezer_download` ARL engine |
| **Qobuz** | yes | yes | opt-in | `streamrip` 2.1.0 |
| **Tidal** | yes | yes | opt-in | `streamrip` 2.1.0 (`dl_utils.streamrip_runner`) |
| **Amazon Music** | yes | yes | opt-in | External `AMAZON_CLI` (`amazon-cli`) |
| **Spotify** | yes | yes | opt-in | `dotify-cli` 3.0.6 (web session + Widevine) |
| **Apple Music** | yes | yes | opt-in | `gamdl` 3.9.1 |
| **YouTube** | yes | yes | opt-in | `yt-dlp` (Music artist/channel links supported) |
| **SoundCloud** | yes | — | opt-in | `yt-dlp` |

Mobile share links and URL shorteners are resolved before routing, so a link copied from a phone app works identically to a desktop URL.

---

## 🚀 Quick Start

```bash
cp token.env.example token.env   # fill in TELEGRAM_TOKEN, ADMIN_ID, and platform credentials
./update.sh                      # migrates inline credentials, plays boot splash, and runs docker compose
```

Or start directly with Docker Compose:

```bash
docker compose up -d --build
```

`token.env` is the single configuration surface. Credentials and feature flags are read from disk on each request so rotations take effect without recreating the container (`TELEGRAM_TOKEN`, `BOT_LANG`, `MAX_CONCURRENT_DOWNLOADS`, and `MAX_QUEUED_DOWNLOADS` are read at startup and require a restart).

> **Security Note:** `token.env`, `data/whitelist.json`, and live files inside `local_resources/` are gitignored and must never be committed. `token.env.example` and `local_resources/*.example` are sanitized templates.

---

## ⚙️ Configuring `token.env`

### Telegram & Identity

| Key | Purpose |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather). Required. |
| `ADMIN_ID` | Numeric Telegram User ID of the administrator. Gates `/status`, `/whitelist`, `/allow`, `/block`, `/blockall`, `/arl`, `/newarl`, `/cancel all`, and all `/authenticate*` commands. |
| `BOT_LANG` | Reply language code (`en`, `fr`, `es`, `pt`, `in`, `de`, `zh`, `ar`). Defaults to `en`. |

### Per-Platform Enable Flags

All eight default to `1`. Set any platform to `0` to take it offline; links for a disabled platform are refused before any download slot is consumed.

```ini
DEEZER_ENABLED=1
QOBUZ_ENABLED=1
TIDAL_ENABLED=1
AMAZON_ENABLED=1
SPOTIFY_ENABLED=1
APPLE_ENABLED=1
YOUTUBE_ENABLED=1
SOUNDCLOUD_ENABLED=1
```

### Per-Platform Artist Discography Toggles

All eight default to `0` (disabled). Set a platform's flag to `1` to allow artist discography downloads on that platform. `MAX_ARTIST_RELEASES` caps catalogue size before any audio is fetched.

```ini
DEEZER_ARTIST_DOWNLOAD_ENABLED=0
QOBUZ_ARTIST_DOWNLOAD_ENABLED=0
TIDAL_ARTIST_DOWNLOAD_ENABLED=0
AMAZON_ARTIST_DOWNLOAD_ENABLED=0
SPOTIFY_ARTIST_DOWNLOAD_ENABLED=0
APPLE_ARTIST_DOWNLOAD_ENABLED=0
YOUTUBE_ARTIST_DOWNLOAD_ENABLED=0
SOUNDCLOUD_ARTIST_DOWNLOAD_ENABLED=0
```

### Cross-Platform Fallback (`FALLBACK_ENABLED`)

- `FALLBACK_ENABLED=0` (default): Every platform uses only its own direct engine and credentials. Missing credentials or upstream failures return a diagnostic naming the required key or config file.
- `FALLBACK_ENABLED=1`: Uncredentialed requests or failed direct downloads on non-Spotify platforms attempt a single cross-matched fallback download via `DEEZER_TOKEN` (ARL) for tracks and albums. **Spotify is always direct-only** and never falls back to Deezer.

---

## 🔐 Credentials & `local_resources/` Offloading

Multi-field credentials live in `local_resources/` with `0600` permissions and are linked in `token.env`:

| Resource File | `token.env` Path Variable | Maintained By |
|---|---|---|
| `local_resources/tidal_config.toml` | `TIDAL_CONFIG_PATH` | `/authenticatetidal` or auto-migrated from `token.env` |
| `local_resources/qobuz_config.toml` | `QOBUZ_CONFIG_PATH` | `/authenticateqobuz` or auto-migrated from `token.env` |
| `local_resources/apple_cookies.txt` | `APPLE_COOKIES_PATH` | `/authenticateapple`, cookie export, or auto-migrated |
| `local_resources/spotify_cookies.txt` | `SPOTIFY_COOKIES_PATH` | Exported Netscape cookies from `open.spotify.com` (`sp_dc`) |
| `local_resources/spotify_credentials.json` | `SPOTIFY_CREDENTIALS_PATH` | Optional JSON `{"sp_dc": "..."}` adapter |
| `local_resources/device.wvd` | `SPOTIFY_WVD_PATH`, `AMAZON_WVD_PATH` | Provisioned Widevine device file for Spotify / Amazon |
| `local_resources/cookies.txt` | `COOKIES_PATH` | Optional Netscape cookies for `yt-dlp` |

If you paste legacy inline values (`TIDAL_OAUTH_TOKEN`, `QOBUZ_EMAIL`/`QOBUZ_PASSWORD`, `APPLE_MEDIA_USER_TOKEN`) into `token.env`, both `./update.sh` and `main.py` startup automatically migrate them into `local_resources/` and blank the inline secret lines in `token.env`.

### Platform Setup Summary

- **Deezer:** Set `DEEZER_TOKEN` to your `arl` cookie from deezer.com, or run `/authenticatedeezer <arl>` (or `/newarl <arl>`) in Telegram. Optional `arl_fetcher/` (TypeScript/bun) automates ARL login.
- **Tidal:** Run `/authenticatetidal` (no arguments) in Telegram. The bot generates a clickable `https://link.tidal.com/XXXXX` TV device code, polls in the background until approved, and writes `local_resources/tidal_config.toml`. Refreshed tokens are synced back to disk automatically.
- **Qobuz:** Run `/authenticateqobuz <email> <password>` or `/authenticateqobuz <user_id> <user_auth_token> [app_id] [secrets]`. If `app_id` and `secrets` are omitted, the bot discovers them automatically via `streamrip` and writes `local_resources/qobuz_config.toml`.
- **Apple Music:** Run `/authenticateapple <media_user_token>` in Telegram, or export `local_resources/apple_cookies.txt` from music.apple.com. With `ENABLE_FLAC=1`, Gamdl uses `alac,aac-web`; override via `APPLE_CODEC_PRIORITY`.
- **Spotify:** Place your exported `local_resources/spotify_cookies.txt` (containing `sp_dc` from a Spotify Premium web session) and `local_resources/device.wvd` into `local_resources/`. Streams are remuxed natively via `--audio-remux-mode ffmpeg` without lossy transcoding.
- **Amazon Music:** Set `AMAZON_DEVICE_TOKEN`, place `local_resources/device.wvd`, and provide the `amazon-cli` binary on `PATH` or in `./bin/`.
- **YouTube & SoundCloud:** Work out of the box via `yt-dlp`. Optional `COOKIES_PATH` and `/authenticatesoundcloud <oauth_token>` are supported for restricted or HQ streams.

---

## 📦 Output, Delivery, Concurrency & Cleanup

| Key | Default | Purpose |
|---|---:|---|
| `ENABLE_FLAC` | `1` | Requests lossless (up to 24-bit/192 kHz FLAC / ALAC where offered); `0` requests 320 kbps MP3/AAC. |
| `FORMAT` | `audio` | `audio` uploads individual tracks in their original downloaded format directly to Telegram (when `COPY_FILES_PATH` is blank). `zip` packages releases into a `.zip` archive. |
| `SEND_ALBUM_COVER` | `1` | Attach release cover art (`cover.jpg`). |
| `COPY_FILES_PATH` | *(blank)* | Optional host directory for finished `.zip` archives. When set, releases are packaged as `.zip` archives there and delivered via `FILE_LINK_TEMPLATE`. When blank and `FORMAT=zip`, archives upload directly to Telegram (`<48 MB` multipart splits). |
| `FILE_LINK_TEMPLATE` | `https://example.com/download/{0}` | Public URL template used when `COPY_FILES_PATH` is set (`{0}` is replaced with the URL-quoted hashed zip filename). |
| `AUTO_DELETE_ENABLED` | `1` | Automatically delete bot messages and staged files after `AUTO_DELETE_SECONDS`. Also gates the 10-minute background orphan sweeper. |
| `AUTO_DELETE_SECONDS` | `1800` | Retention window in seconds (default 30 minutes, plus a 60s grace buffer on background sweeps). |
| `MAX_RETRIES` | `10` | Per-track retry budget in `token.env.example` (falls back to `5` if unset). |
| `ENGINE_TIMEOUT_SECONDS` | `3600` | Wall-clock ceiling (in seconds) for a single engine subprocess. |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Maximum simultaneous downloads across all users (read at startup). |
| `MAX_QUEUED_DOWNLOADS` | `1` | Maximum requests waiting in queue before new callers are declined (read at startup). |
| `MAX_ARTIST_RELEASES` | `100` | Hard cap on releases pulled per artist discography link, enforced before download starts. |
| `ARTIST_API_DELAY` | `0.6` | Seconds slept between catalogue calls during an artist walk. |
| `STREAMRIP_MAX_CONNECTIONS` | `3` | Concurrent connections inside `streamrip` (paired with a 30 req/min rate limit). |

---

## 💬 Telegram Commands

### Public User Commands

| Command | Description |
|---|---|
| `/start` | Display the DeadMoon welcome message, public platform availability board (`✅` / `▫️`), artist status, and inline usage hint. Also handles `/start track_<id>` and `/start album_<id>` Deezer deep-links. |
| `/help` | Display the public platform availability board and usage instructions. |
| *(send any link)* | Resolve, download, and return native audio attachments (or a `.zip` archive / hosted link) with exact file size metadata. |
| `@bot (album\|track\|artist) <query>` | Inline search mode for Deezer tracks, albums, and artists. |
| `/cancel` | Cancel your active or queued download in the current chat. |

### Administrator Commands (`ADMIN_ID` Only)

| Command | Description |
|---|---|
| `/status` | Full DeadMoon Status Report: enabled platforms, direct engines, credential readiness, artist flags, active/queued downloads, active user IDs, and runtime configuration. |
| `/cancel all` | Cancel all active and queued downloads across all users and chats. |
| `/whitelist` | Show all authorized user and group/channel IDs, resolved to display names. |
| `/allow <id> [<id> ...]` | Add one or more user/group IDs (space- or comma-separated) to the persistent whitelist. |
| `/block <id> [<id> ...]` | Remove IDs from the whitelist and leave unauthorized groups/channels. |
| `/blockall` | Remove all whitelisted IDs except `ADMIN_ID`. |
| `/arl` | Display the currently configured Deezer ARL token (masked). |
| `/newarl <arl>` | Validate and save a new Deezer ARL token, enable Deezer, and hot-reload the session in-process. |
| `/authenticatedeezer <arl>` | Alias for `/newarl <arl>`. |
| `/authenticatetidal` | Start an interactive Tidal TV device login (`https://link.tidal.com/XXXXX`) or paste a JSON/key-value/JWT credential set (`/authenticatetidal <payload>`). |
| `/cancelauth` | Cancel an in-flight `/authenticatetidal` device polling task. |
| `/authenticateqobuz <args>` | Save Qobuz `<email> <password>` or `<user_id> <token> [app_id] [secrets]` into `local_resources/qobuz_config.toml` (auto-discovers `app_id` and secrets if omitted). |
| `/authenticateapple <token>` | Write a Netscape cookie file for `media-user-token` to `local_resources/apple_cookies.txt` and enable Apple Music. |
| `/authenticatesoundcloud <token>` | Save `SOUNDCLOUD_OAUTH_TOKEN` to `token.env` and enable SoundCloud. |

---

## 🔗 Shortlinks & Mobile Share Links

Tracking parameters (`utm_*`, `si`, `ref_*`, `igshid`, `mc_*`, piwik/matomo) are stripped before routing, while semantic single-track selectors (`?i=<songId>` on Apple Music album URLs and `?trackAsin=<asin>` on Amazon Music album URLs) are preserved:

| Platform | Accepted Forms |
|---|---|
| **Deezer** | `deezer.page.link/*`, `dzr.page.link/*`, `link.deezer.com/*`, `deezer.com/[intl/](track\|album\|artist)/*` |
| **Spotify** | `spotify.link/*`, `spoti.fi/*`, `open.spotify.com/[intl-xx/](track\|album\|artist)/*` |
| **Tidal** | `t.idal.com/*`, `tidal.com/[browse/](track\|album\|artist)/*`, `listen.tidal.com/*` |
| **Qobuz** | `open.qobuz.com/*`, `play.qobuz.com/*`, `www.qobuz.com/<lang>/album/*` |
| **Amazon Music** | `music.amazon.*/(albums\|tracks\|artists)/*`, `amzn.to/*`, `a.co/*` |
| **Apple Music** | `music.apple.com/<storefront>/(album\|artist\|song)/*`, `itunes.apple.com/*`, `apple.co/*` |
| **YouTube** | `youtu.be/*`, `youtube.com/watch*`, `youtube.com/shorts/*`, `music.youtube.com/*` |
| **SoundCloud** | `on.soundcloud.com/*`, `soundcloud.app.goo.gl/*`, `soundcloud.com/*`, `m.soundcloud.com/*` |

---

## 🗂️ Archive Layout (When Packaging `.zip`)

Every platform produces the exact same directory hierarchy:

```text
<Artist> - <Album> [Year]/
├── 01 - <Artist> - <Title>.flac
├── 02 - <Artist> - <Title>.flac
└── cover.jpg
```

Artist discographies nest one folder per release under `<Artist>/`, and archives are named `<Clean_Artist>_-_<Clean_Title>_(<8-char md5>).zip`.

---

## 🐳 Docker Compose Bind Mounts

| Host Path | Container Path | Purpose |
|---|---|---|
| `./local_resources/` | `/usr/src/app/local_resources/` (rw) | Private `0600` credential/cookie files and refreshed OAuth state. |
| `/var/www/html/.../download` | `/files` | `COPY_FILES_PATH` target directory for hosted `.zip` links. |
| `./data` | `/usr/src/app/data` | Persistent `whitelist.json` storage across container rebuilds. |
| `./token.env` | `/usr/src/app/token.env` | Live configuration file rewritten in-place by `/newarl` and `/authenticate*`. |

---

## 🧪 Running the Offline Test Suite

```bash
python3 -m unittest discover -s tests -v
```

413 offline tests — no network calls, no live Telegram connection, and no credentials required. When running inside the engine virtual environment, ensure the venv's `bin` directory is on `PATH` and `ffmpeg`/`ffprobe` are installed so all engine call-site and audio-property assertions execute without skipping.

---

## 🛠️ Troubleshooting

| Symptom | Check |
|---|---|
| `"You are not authorized to use this bot"` | User or group ID is not whitelisted. Run `/whitelist`, then `/allow <id>`. |
| Platform replies `"not supported"` | Its `<PLATFORM>_ENABLED` flag is `0`. Check `/status`. |
| Artist link refused | `<PLATFORM>_ARTIST_DOWNLOAD_ENABLED` is `0` (default across all platforms). |
| `"Another download is already running"` | `MAX_CONCURRENT_DOWNLOADS` and `MAX_QUEUED_DOWNLOADS` are full, or user already has an active download in that chat. |
| Platform reports missing credentials | Populate the named `token.env` key or `local_resources/` file, or run the matching `/authenticate*` command. |
| Tidal HTTP 401 / 403 | Run `/authenticatetidal` (without arguments) for TV-device login; Web Player tokens (`cid=3235`) reject direct stream manifests. |
| Tidal HTTP 429 | Keep the built-in 30 req/min cap; lower `STREAMRIP_MAX_CONNECTIONS` if rate-limiting persists. |
| Artist pull stops at 100 releases | Capped by `MAX_ARTIST_RELEASES` before download starts. |

---

## 📜 Upstream, Lineage & License

- [kernoeb/Telegramusic](https://github.com/kernoeb/Telegramusic) — original Telegram bot shell.
- [kmille/deezer-downloader](https://github.com/kmille/deezer-downloader) — Deezer ARL download implementation.
- [nathom/streamrip](https://github.com/nathom/streamrip), [glomatico/gamdl](https://github.com/glomatico/gamdl), [spotify-downloader/dotify-cli](https://github.com/spotify-downloader/dotify-cli), and [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) — direct acquisition engines.

Licensed under the **Mozilla Public License 2.0 (MPL-2.0)** — see [LICENSE](LICENSE).