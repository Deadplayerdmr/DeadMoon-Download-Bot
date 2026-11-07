# 🌙 DeadMoon-Download-BotV3.0

```text
      *   d~~b  .        .================.        .   d~~b   *
   .    d  b      .==='  .------------.  '===.      d  b     .
      *      /   .-'    .      o    '-.   \         *
               /  .'   o       .      .   '.  \        .
    *         [===]  .    /        \    .  [===]   .       *
        (( .--[===]     \____/    \____/   [===]--. ))   d~b
        ((    [===]   o      .----.      O [===]    ))   d b
    .    '--- [===]    .    /      \   .   [===] ---'
       *       \  '.     .            o  .'  /       *
    .           '.  '-.    o   .   .  .-'  .'     .       *
         *        '===='--------------'===='          .
  ============================================================================
    D E A D M O O N   A U D I O   A C Q U I S I T I O N   B O T   ( v 3 . 0 )
  ============================================================================
```

A private, whitelisted multi-platform Telegram audio acquisition bot (`Python 3.13` / `aiogram 3.28`) that turns streaming links into deterministically structured, tagged `.zip` archives (or hosted download links).

Designed as a transient front-end for downstream **Music Information Retrieval (MIR)** acoustic analysis pipelines (measuring LUFS loudness distribution, dynamic range, spectral features, and melodic intervals). Fetched streams are staged in an isolated temporary tree, packaged with uniform folder/filename geometry across all 8 platforms, and automatically purged on a configurable timer.

> **Upstream Lineage & Attribution:** Originally derived from [**kernoeb/Telegramusic**](https://github.com/kernoeb/Telegramusic) (Telegram bot shell) and [**kmille/deezer-downloader**](https://github.com/kmille/deezer-downloader) (Deezer ARL decryption algorithm). Heavily expanded in V3.0 with an 8-platform direct-engine router, lock-free concurrency scheduler, dynamic chat/user whitelisting, public metadata bridge, crash-safe orphan sweepers, and a 392-test offline verification suite.

For internal architecture, request lifecycle, concurrency invariants, and instructions on adding new providers, see [**ARCHITECTURE.md**](ARCHITECTURE.md).

---

## ✨ Key Features in V3.0

- **8-Platform Direct Acquisition Matrix:** Native support for **Deezer**, **Spotify**, **Qobuz**, **Tidal**, **Apple Music**, **Amazon Music**, **YouTube**, and **SoundCloud**.
- **Deterministic MIR Archive Layout:** Every platform outputs the exact same `.zip` directory hierarchy (`<Artist> - <Album> [Year]/NN - <Artist> - <Title>.<ext> + cover.jpg`) and MD5-suffixed basename (`<Clean_Artist>_-_<Clean_Title>_(<8-char md5>).zip`).
- **Private Whitelist & Access Control Middleware:** Built-in `/whitelist`, `/allow`, `/block`, and `/blockall` commands with automatic group authorization when added by the administrator.
- **Lock-Free Synchronous Download Scheduler:** Bounded global concurrency (`MAX_CONCURRENT_DOWNLOADS` + `MAX_QUEUED_DOWNLOADS`), per-chat `/cancel` and admin `/cancel all` process-tree termination, and live queue promotion.
- **Zero-Restart Hot Configuration (`token.env`):** Live credential rotation (`/newarl`, `SPOTIFY_AUDIO_QUALITY`, `TIDAL_OAUTH_TOKEN`, `APPLE_MEDIA_USER_TOKEN`) takes effect on the very next request without recreating the Docker container.
- **Atomic `0600` Credential Staging & Dual Orphan Sweepers:** Ephemeral engine configs and cookies are created atomically with `0600` permissions and cleaned up via `finally:` blocks plus a 10-minute background sweeper.
- **392 Offline Unit & Engine Call-Site Tests:** Full test coverage (`python3 -m unittest discover -s tests -v`) requiring zero network calls, zero Telegram tokens, and zero live store credentials.

---

## 🎵 Supported Platforms & Engines

| Platform | Track | Album | Artist | Direct Engine | Notes |
| :--- | :---: | :---: | :---: | :--- | :--- |
| **Deezer** | ✅ | ✅ | ✅ | Bundled `deezer_download` | Direct ARL + Blowfish CBC decryption (`FLAC` / `MP3`) |
| **Spotify** | ✅ | ✅ | ❌ | `dotify-cli` 3.0.6 | Web session (`sp_dc`) + Widevine L3 (`device.wvd`), stream-copied via FFmpeg |
| **Qobuz** | ✅ | ✅ | ❌ | `streamrip` 2.1.0 | Up to 24-bit/192 kHz Hi-Res FLAC |
| **Tidal** | ✅ | ✅ | ❌ | `streamrip` 2.1.0 | OAuth device login (`FLAC` / `AAC`) |
| **Apple Music** | ✅ | ✅ | ❌ | `gamdl` 3.9.1 | `alac,aac-web` via `apple_cookies.txt` or `APPLE_MEDIA_USER_TOKEN` |
| **Amazon Music** | ✅ | ✅ | ❌ | `amazon-cli` | Device token + Widevine `device.wvd` |
| **YouTube** | ✅ | ✅ | ❌ | `yt-dlp` | Supports `youtu.be`, `music.youtube.com`, and Shorts |
| **SoundCloud** | ✅ | ❌ | ❌ | `yt-dlp` | Supports mobile `on.soundcloud.com` shortlinks |

Mobile share links and URL shorteners (`spotify.link`, `spoti.fi`, `deezer.page.link`, `t.idal.com`, `apple.co`, `amzn.to`, `on.soundcloud.com`, `bit.ly`) are automatically expanded and stripped of tracking parameters before routing.

---

## 🚀 Quick Start

```bash
git clone [https://github.com/](https://github.com/)<YOUR-USERNAME>/DeadMoon-Download-BotV3.0.git
cd DeadMoon-Download-BotV3.0
cp token.env.example token.env   # Edit token.env with your TELEGRAM_TOKEN, ADMIN_ID, and store credentials
chmod 600 token.env
docker compose up -d --build
```

`token.env` is the single configuration surface. Every key (except `TELEGRAM_TOKEN` and scheduler capacity caps) is read live from `token.env` on each request, so edits take effect immediately without recreating the container.

> **Security Note:** `token.env` and live files inside `local_resources/` are gitignored. Never commit real tokens or `.wvd` binaries.

---

## ⚙️ Configuring `token.env`

### 1. Telegram & Identity

| Key | Purpose |
| :--- | :--- |
| `TELEGRAM_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather). Required. |
| `ADMIN_ID` | Numeric Telegram User ID of the administrator. Gates `/status`, `/cancel all`, `/arl`, `/newarl`, `/whitelist`, `/allow`, `/block`, and `/blockall`. |
| `BOT_LANG` | Localization language code (`en`, `fr`, `es`, `pt`, `in`, `de`, `zh`, `ar`). Defaults to `en`. |

### 2. Platform Switches & Fallback

- **Per-Platform Enablement:** `DEEZER_ENABLED`, `QOBUZ_ENABLED`, `TIDAL_ENABLED`, `AMAZON_ENABLED`, `SPOTIFY_ENABLED`, `APPLE_ENABLED`, `YOUTUBE_ENABLED`, `SOUNDCLOUD_ENABLED` (all default to `1`). Setting a platform to `0` refuses its links before acquiring a download slot.
- **Artist Discographies:** `DEEZER_ARTIST_DOWNLOAD_ENABLED=1` enables full artist discography pulls on Deezer (capped at `MAX_ARTIST_RELEASES=100` before download starts). Artist links on other platforms are politely refused without consuming a slot.
- **Cross-Platform Deezer Fallback (`FALLBACK_ENABLED`):** Defaults to `0` (disabled). When set to `1`, uncredentialed requests or direct-engine failures on non-Spotify platforms attempt a single cross-match via Odesli/song.link, ISRC, or public metadata search and download via your `DEEZER_TOKEN` ARL. **Spotify is strictly direct-only** and never falls back to Deezer.

### 3. Store Credentials & `local_resources/`

Compose mounts `./local_resources` read-only at `/usr/src/app/local_resources`. A credential file counts as ready only when it exists, is non-empty, and is not byte-identical to its `.example` template.

| Platform | `token.env` Keys / `local_resources/` Files | Setup Summary |
| :--- | :--- | :--- |
| **Deezer** | `DEEZER_TOKEN` | Paste your `arl` cookie from a logged-in `deezer.com` browser session, or update live in Telegram via `/newarl <arl>`. |
| **Spotify** | `SPOTIFY_COOKIES_PATH` (`spotify_cookies.txt`)<br>`SPOTIFY_WVD_PATH` (`device.wvd`) | Export a Netscape cookie file containing `.spotify.com` `sp_dc` from a Spotify Premium web session and provide a provisioned Widevine L3 `.wvd` file. Default `SPOTIFY_AUDIO_QUALITY=aac-high,aac-medium` (256 kbps native AAC `.m4a` stream-copied via FFmpeg) with `SPOTIFY_BULK_WAIT_SECONDS=4` avoids Widevine L3 `403`/`429` license rate-limits. |
| **Qobuz** | `QOBUZ_USER_ID` + `QOBUZ_USER_AUTH_TOKEN`<br>`QOBUZ_APP_ID` + `QOBUZ_SECRETS` | Run `streamrip` interactively once or provide `QOBUZ_EMAIL` + `QOBUZ_PASSWORD`. |
| **Tidal** | `TIDAL_OAUTH_TOKEN` (or individual fields) | Complete device login via `streamrip` (`link.tidal.com`) and paste the JSON or `key=value` OAuth blob. |
| **Apple Music** | `APPLE_COOKIES_PATH` (`apple_cookies.txt`)<br>or `APPLE_MEDIA_USER_TOKEN` | Export Netscape cookies from `music.apple.com` containing `media-user-token`, or paste the token value directly into `APPLE_MEDIA_USER_TOKEN`. |
| **Amazon Music**| `AMAZON_DEVICE_TOKEN` + `AMAZON_WVD_PATH` | Provide your Amazon device token and Widevine `device.wvd`. |
| **YouTube / SC**| `COOKIES_PATH` / `SOUNDCLOUD_OAUTH_TOKEN` | Optional Netscape `cookies.txt` for age-gated streams or `SOUNDCLOUD_OAUTH_TOKEN`. |

### 4. Output, Hosting & Auto-Delete

| Key | Default | Purpose |
| :--- | :--- | :--- |
| `ENABLE_FLAC` | `1` | `1` = Lossless (`FLAC` / `ALAC`) where offered; `0` = Lossy (`MP3 320` / `AAC`). |
| `COPY_FILES_PATH` | `/files` | When set, finished `.zip` archives are written to this mounted host directory and the bot replies with a URL formatted from `FILE_LINK_TEMPLATE`. When blank, archives upload directly to Telegram (splitting albums >48 MB into multipart zips). |
| `FILE_LINK_TEMPLATE` | `https://example.com/download/{0}` | Public download URL template where `{0}` is replaced with the URL-quoted hashed `.zip` basename. |
| `AUTO_DELETE_ENABLED` | `1` | Automatically deletes bot messages and staged `.zip` archives after `AUTO_DELETE_SECONDS` (default `1800` = 30 mins), backed by a 10-minute crash-orphan sweeper. |

---

## 🤖 Telegram Commands

### User Commands
| Command | Description |
| :--- | :--- |
| `/start`, `/help` | Display welcome guide, active platforms, and inline search syntax. |
| `<Platform URL>` | Send any supported track, album, or Deezer artist link (including mobile shortlinks) to download. |
| `@bot (album\|track\|artist) <query>` | Inline search mode for Deezer tracks, albums, and artists. |
| `/cancel` | Cancel your active or queued download in the current chat. |

### Admin Commands (`ADMIN_ID` only)
| Command | Description |
| :--- | :--- |
| `/status` | Inspect enabled platforms, direct engines, credential readiness (`creds ✓` / `no creds`), live active/queued slots, and hosting settings. |
| `/cancel all` | Terminate all active and queued downloads across all users and chats. |
| `/whitelist` | List all authorized user IDs and group/channel IDs with resolved names. |
| `/allow <id1> [id2, ...]` | Add one or more user/chat IDs to the persistent whitelist (`data/whitelist.json`). |
| `/block <id1> [id2, ...]` | Revoke whitelist access and automatically leave unauthorized groups. |
| `/blockall` | Revoke all whitelisted IDs except `ADMIN_ID`. |
| `/arl` | Display the currently configured Deezer ARL token (masked). |
| `/newarl <arl>` | Atomically rewrite `DEEZER_TOKEN` in `token.env` and hot-reload the Deezer session in-process without restarting the container. |

---

## 📦 Deterministic Archive Layout

Every platform produces the exact same archive structure for downstream acoustic parsers:

```text
# Single Track or Album:
<Artist> - <Album> [Year]/
├── 01 - <Artist> - <Title>.<ext>
├── 02 - <Artist> - <Title>.<ext>
└── cover.jpg

# Artist Discography (Deezer):
<Artist>/
├── <Artist> - <Album A> [2019]/
└── <Artist> - <Album B> [2024]/
```

Archives are named `<Clean_Artist>_-_<Clean_Title>_(<8-char md5>).zip` so repeat pulls never collide on a shared `COPY_FILES_PATH`.

---

## 🧪 Running the Offline Test Suite

```bash
python3 -m unittest discover -s tests -v
```

Runs **392 hermetic unit and engine call-site tests** covering link classification, local HTTP shortlink redirect chains, scheduler slot exclusivity, archive geometry, orphan sweeping, and `--no-deps` runtime compatibility (`Pillow 13`, `tomlkit`, `cryptography 45`, `protobuf 6.33.6`, `pywidevine 1.9.0`, `dotify-cli 3.0.6`, `streamrip 2.1.0`, `gamdl 3.9.1`).

---

## 🙏 Credits, Engines & Upstream Lineage

### Core Bot & Decryption Lineage
- [**kernoeb/Telegramusic**](https://github.com/kernoeb/Telegramusic) — Original Telegram bot shell and MPL-2.0 foundation.
- [**kmille/deezer-downloader**](https://github.com/kmille/deezer-downloader) — Reference Deezer ARL session & Blowfish CBC 2048-byte stripe decryption algorithm (`dl_utils/deezer_download.py`).

### Direct Platform Acquisition Engines
- [**glomatico/dotify**](https://github.com/glomatico/dotify) (`dotify-cli==3.0.6`) & [**glomatico/unplayplay**](https://github.com/glomatico/unplayplay) — Direct Spotify Web (`sp_dc`) + Widevine L3 acquisition engine with lossless FFmpeg stream-copy remuxing.
- [**nathom/streamrip**](https://github.com/nathom/streamrip) (`streamrip==2.1.0`) & [**DevLARley/deezer-py**](https://github.com/DevLARley/deezer-py) — Direct Hi-Res FLAC acquisition engine for **Qobuz** and **Tidal**.
- [**glomatico/gamdl**](https://github.com/glomatico/gamdl) (`gamdl==3.9.1`) — Direct **Apple Music** (`alac` / `aac-web`) acquisition engine.
- [**yt-dlp/yt-dlp**](https://github.com/yt-dlp/yt-dlp) (`yt-dlp==2026.10.26`) — Direct extraction engine for **YouTube**, **YouTube Music**, and **SoundCloud**.
- **Amazon Music CLI (`amazon-cli`)** — External Widevine-backed acquisition binary dispatched for **Amazon Music**.
- **`arl_fetcher/` (Bun / TypeScript)** — Bundled headless helper for automated Deezer `arl` token retrieval.

### DRM, Cryptographic & Provisioning Tooling
- [**devine-dl/pywidevine**](https://github.com/devine-dl/pywidevine) (`pywidevine==1.9.0`) — Google Widevine CDM protocol implementation and `.wvd` v2 container parser.
- [**devine-dl/pyplayready**](https://github.com/devine-dl/pyplayready) (`pyplayready==0.8.5`) — Microsoft PlayReady DRM key-wrapping and license protocol implementation used by `gamdl`.
- [**Legrandin/pycryptodome**](https://github.com/Legrandin/pycryptodome) (`pycryptodome==3.23.0`) & [**pyca/cryptography**](https://github.com/pyca/cryptography) (`cryptography==45.0.7`) — Blowfish CBC stripe decryption, ARC4/MD5 key derivation, and AES key-wrap primitives.
- [**hyugogirubato/KeyDive**](https://github.com/hyugogirubato/KeyDive) & [**Frida**](https://frida.re/) — Widevine L3 CDM extraction and verification tooling used during device provisioning.

### Audio Processing, Tagging & Metadata Bridges
- [**FFmpeg / ffprobe**](https://ffmpeg.org/) — Lossless container remuxing (`-c copy`) and post-download stream inspection (bit depth, sample rate, bitrate verification).
- [**quodlibet/mutagen**](https://github.com/quodlibet/mutagen) (`mutagen==1.47.0`) & [**beardypig/pymp4**](https://github.com/beardypig/pymp4) (`pymp4==1.4.0`) — FLAC, ID3v2.3, and MP4/M4A metadata tagging and container parsing.
- [**python-pillow/Pillow**](https://github.com/python-pillow/Pillow) (`Pillow==13.0.0`) — Cover art scaling, thumbnail generation, and PNG-to-JPEG normalization.
- [**aiogram/aiogram**](https://github.com/aiogram/aiogram) (`aiogram==3.28.2`) & [**aio-libs/aiohttp**](https://github.com/aio-libs/aiohttp) (`aiohttp==3.13.5`) — Asynchronous Telegram Bot API framework and non-blocking HTTP/shortlink resolution client.
- [**Odesli / Song.link API**](https://odesli.co/), **iTunes Search API**, **Deezer Public API**, **Spotify Embed/oEmbed**, and **Qobuz JSON-LD** — Public metadata enrichment and ISRC cross-matching in `dl_utils/metadata_bridge.py`.

## 📜 License

Distributed under the **Mozilla Public License Version 2.0 (MPL-2.0)** — see [LICENSE](LICENSE) for full details.