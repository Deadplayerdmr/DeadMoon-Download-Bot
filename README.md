# DeadMoon Download Bot

A private, whitelisted Telegram bot that acquires audio from streaming platforms and
returns it as original-format Telegram audio by default, or as a zip archive/hosted
link when `FORMAT=zip`. Fetched streams are staged in a
temporary directory, handed to an external acoustic analysis pipeline (LUFS loudness
distribution, dynamic range, spectral features, melodic intervals), and purged from both
Telegram and local storage. Nothing is retained permanently.

Derived from [kernoeb/Telegramusic](https://github.com/kernoeb/Telegramusic) and
[kmille/deezer-downloader](https://github.com/kmille/deezer-downloader), extended with a
multi-platform router and dedicated direct acquisition engines.

For the internal design — request lifecycle, module boundaries, concurrency invariants,
and how to add a provider — see [ARCHITECTURE.md](ARCHITECTURE.md).

## Supported platforms

| Platform      | Track | Album | Artist | Notes |
|---------------|:-----:|:-----:|:------:|-------|
| Deezer        | yes   | yes   | opt-in | Reference implementation; direct ARL engine |
| Qobuz         | yes   | yes   | opt-in | Streamrip |
| Tidal         | yes   | yes   | opt-in | Streamrip; TV-device login recommended |
| Amazon Music  | yes   | yes   | opt-in | External Amazon CLI |
| Spotify       | yes   | yes   | opt-in | Dotify; direct-only |
| Apple Music   | yes   | yes   | opt-in | Gamdl |
| YouTube       | yes   | yes   | opt-in | Via `yt-dlp`; Music artist/channel links |
| SoundCloud    | yes   | —     | opt-in | Via `yt-dlp` |

Mobile share links and shorteners are resolved before routing, so a link copied off a
phone works the same as a desktop URL. See
[Shortlinks and mobile share links](#shortlinks-and-mobile-share-links).

## Quick start

```bash
cp token.env.example token.env   # then edit it
docker compose up -d --build
```

`token.env` is the single configuration surface. Every key is read live from that file on
each use, so edits take effect without recreating the container — this matters most for
rotating tokens (`/authenticatedeezer`, `/authenticateqobuz`, `/authenticatetidal`, `/authenticateapple`, `/authenticatesoundcloud`).

> `token.env` is gitignored and holds credentials. `token.env.example` is the committed
> template and must never contain a real value.

## Configuring `token.env`

### Telegram and identity

| Key | Purpose |
|-----|---------|
| `TELEGRAM_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather). Required. |
| `ADMIN_ID` | Numeric Telegram ID of the admin. Gates `/authenticatedeezer`, `/authenticate*`, `/cancelauth`, `/status`, `/whitelist`, `/allow`, `/block`, and `/blockall`. |
| `BOT_LANG` | Response language code. `langs.json` is keyed by string first and locale second, so this selects the locale inside each entry: `fr`, `en`, `es`, `pt`, `in`, `de`, `zh`, `ar`. Unknown or unset falls back to `en`. |

### Per-platform enable flags

`DEEZER_ENABLED`, `QOBUZ_ENABLED`, `TIDAL_ENABLED`, `AMAZON_ENABLED`, `SPOTIFY_ENABLED`,
`APPLE_ENABLED`, `YOUTUBE_ENABLED`, `SOUNDCLOUD_ENABLED`

All default to `1`. Set to `0` to take a platform offline: links for it get a polite
"not supported" reply and no download slot is consumed.

### Artist downloads

Every platform has a `<PREFIX>_ARTIST_DOWNLOAD_ENABLED` toggle and **all eight default to
`0`**: `DEEZER`, `QOBUZ`, `TIDAL`, `AMAZON`, `SPOTIFY`, `APPLE`, `YOUTUBE`, and
`SOUNDCLOUD`. Set the desired toggle to `1` to enable discographies. `MAX_ARTIST_RELEASES`
caps catalogue downloads before audio is fetched; yt-dlp artist discovery is also bounded.
Disabled artist links are refused before a download slot is acquired.

### Direct engines and optional Deezer fallback

Each platform has exactly one acquisition engine:

| Platform | Engine |
|----------|--------|
| Deezer | Bundled `deezer_download` ARL downloader |
| Qobuz, Tidal | `streamrip` |
| Spotify | `dotify-cli` 3.0.6 (web session + Widevine) |
| Apple Music | `gamdl` 3.9.1 |
| Amazon Music | External `AMAZON_CLI` (`amazon-cli` in `/status`) |
| YouTube, SoundCloud | `yt-dlp` |

With the default `FALLBACK_ENABLED=0`, a request needs its own platform credentials. Missing
credentials or an engine failure returns an error naming the required key/file; no other
store is tried.

Set `FALLBACK_ENABLED=1` only when cross-store substitution is desired for platforms other
than Spotify. An uncredentialed request or a failed direct download then attempts one Deezer
ARL fallback. The bot cross-matches the track/album through public metadata and uses
`DEEZER_TOKEN`; it reports the fallback in progress messages. Deezer fallback supports tracks
and albums, not artist pages, and still requires a valid ARL. **Spotify is always direct-only:**
neither missing credentials nor a Spotify engine failure can trigger a Deezer/ISRC bridge,
even when the global fallback flag is enabled.

### Credentials, by platform

#### Deezer — ARL

`DEEZER_TOKEN` is the ARL cookie, the long hex-ish value of the `arl` cookie on a logged-in
deezer.com session.

1. Log in at <https://www.deezer.com> in a browser.
2. DevTools → Storage → Cookies → `https://www.deezer.com` → copy the `arl` value.
3. Put it in `DEEZER_TOKEN`, or send `/authenticatedeezer <arl>` to the bot at runtime.

Optional: `DEEZER_USERNAME`/`DEEZER_PASSWORD` (login fallback), `DEEZER_PROXY`,
`DEEZER_SESSION_REINIT_THRESHOLD`.

The `arl_fetcher/` helper (a small TypeScript/bun program) automates ARL retrieval from a
credential pair if you would rather not copy cookies by hand.

#### Qobuz — `user_auth_token` + `app_id`

Qobuz authenticates against a signed request; the app id/secret pair and the user token
both come from a registered client. The easiest route is [`streamrip`](https://github.com/nathom/streamrip),
which this bot already ships:

```bash
streamrip          # interactive: add qobuz, log in with email + password
```

Then lift the values out of `~/.config/streamrip/config.toml` (or
`~/Library/Application Support/streamrip/config.toml` on macOS):

```toml
[qobuz]
email_or_userid = "YOUR_QOBUZ_USER_ID_OR_EMAIL_HERE"
password_or_token = "YOUR_QOBUZ_TOKEN_OR_MD5_PASSWORD_HERE"
app_id = "YOUR_QOBUZ_APP_ID_HERE"
secrets = ["YOUR_QOBUZ_SECRET_HERE"]
```

Map them to `QOBUZ_EMAIL` + `QOBUZ_PASSWORD` (the bot MD5-hashes a non-hex password for
you) or, for a direct token, `QOBUZ_USER_ID` + `QOBUZ_USER_AUTH_TOKEN`, plus
`QOBUZ_APP_ID` and comma-separated `QOBUZ_SECRETS`.

#### Tidal — OAuth device login

`TIDAL_OAUTH_TOKEN` accepts the whole credential blob in one line — either the JSON
`streamrip` writes or the `key=value;key=value` form — and the bot parses
`access_token`, `refresh_token`, `user_id` and `token_expiry` out of it. Alternatively set
`TIDAL_USER_ID`, `TIDAL_ACCESS_TOKEN`, `TIDAL_REFRESH_TOKEN` and `TIDAL_TOKEN_EXPIRY`
individually.

To mint them, run `streamrip`, add Tidal, and complete the device-code login at
<https://link.tidal.com>. Copy the resulting config block. Optional:
`TIDAL_COUNTRY_CODE`, `TIDAL_CUSTOM_API`.

Tokens expire. When one does, paste a fresh `TIDAL_OAUTH_TOKEN` — no restart needed.

### Credential files in `local_resources/`

Spotify, Apple, Amazon, yt-dlp, Tidal and Qobuz can use private files rather than inline
secrets. Compose mounts `./local_resources` **read-write** at `/usr/src/app/local_resources`
so `/authenticate*` and Tidal token refresh can persist. See
[`local_resources/README.md`](local_resources/README.md) for generation steps and the exact
file formats. Sanitized templates are provided for Spotify and Apple.

| Real file | `token.env` setting | Used by |
|-----------|---------------------|---------|
| `tidal_config.toml` | `TIDAL_CONFIG_PATH` | Streamrip OAuth credentials |
| `qobuz_config.toml` | `QOBUZ_CONFIG_PATH` | Streamrip Qobuz credentials |
| `spotify_cookies.txt` | `SPOTIFY_COOKIES_PATH` | Dotify web session |
| `device.wvd` | `SPOTIFY_WVD_PATH`, `AMAZON_WVD_PATH` | Spotify Widevine / Amazon CLI |
| `spotify_credentials.json` | `SPOTIFY_CREDENTIALS_PATH` | Optional JSON `sp_dc` adapter |
| `apple_cookies.txt` | `APPLE_COOKIES_PATH` | Gamdl |
| `cookies.txt` | `COOKIES_PATH` | yt-dlp |

Use the absolute container paths shown in `token.env.example`. A file counts as configured
only when it exists, is non-empty, and is not an unchanged `.example` template. Real files
are gitignored; do not put live values in the `.example` files.

#### Spotify — web cookies and Widevine via dotify-cli

Export a Netscape cookie file from an authorized, logged-in Spotify web session and save it
as `local_resources/spotify_cookies.txt`. It must contain the Netscape header and the
`.spotify.com` `sp_dc` cookie. Use a cookie from a **Spotify Premium** web session to
receive 256 kbps AAC or FLAC where Spotify and the release offer those formats. Free-tier
`sp_dc` cookies are capped by Spotify at 128 kbps AAC. Provision a Widevine device with
your authorized tooling and save its binary as `local_resources/device.wvd`, then set `SPOTIFY_WVD_PATH`. See
[`local_resources/README.md`](local_resources/README.md) for the complete provisioning and
validation requirements. A legacy Librespot AP JSON or `SPOTIFY_OAUTH_TOKEN` bearer token
cannot replace these inputs. For compatibility, `SPOTIFY_CREDENTIALS_PATH` can instead point
to a JSON object containing `sp_dc`; the bot converts it to a private `0600` cookie file.

`ENABLE_FLAC=1` selects the native priority
`flac-mp4-24,flac-mp4,vorbis-high,aac-high,aac-medium` by default. The web/Widevine session
can obtain MP4 FLAC and AAC; Vorbis requires a desktop session and is skipped when that
session cannot provide it. Available native FLAC, AAC (`.m4a`), or Vorbis (`.ogg`) is
stream-copied with `--audio-remux-mode ffmpeg`. Lossy streams are never converted to MP3 or
FLAC. If Spotify denies a lossless Widevine license with HTTP 403, the engine retries that
track once with `aac-high,aac-medium` and reuses the AAC list for the rest of that release.
An L3 device or lossy-only release therefore correctly produces native AAC rather than an
inflated lossless file.

For albums, public Spotify embed metadata identifies grayed-out tracks before download. The
engine skips them, attempts every playable track, and succeeds if at least one track was
downloaded. Missing tracks appear in the caption's standard `Failures:` summary. All audio
comes directly from Spotify; no Deezer or other platform fallback is permitted.

#### Apple Music — cookies or `media-user-token` via Gamdl

Export a Netscape HTTP Cookie File while signed in to <https://music.apple.com> with an
active Apple Music subscription. Save it as `local_resources/apple_cookies.txt`; it must
contain the `media-user-token` cookie. Alternatively, set `APPLE_MEDIA_USER_TOKEN` directly in `token.env`. The
pre-filled cookie path is optional: when the cookie file is absent, empty, or an unchanged
example, Gamdl uses the media token instead.

With `ENABLE_FLAC=1`, Gamdl receives `--song-codec-priority alac,aac-web`; with
`ENABLE_FLAC=0`, it receives `aac-web`. `APPLE_CODEC_PRIORITY` can override this with a
comma-separated list of valid Gamdl 3.9.1 `SongCodec` values.

#### Amazon Music — device token + WVD

Amazon needs `AMAZON_DEVICE_TOKEN`, a provisioned Widevine device at `AMAZON_WVD_PATH`, and
an external `AMAZON_CLI` executable. Follow the downloader's provisioning documentation and
store the device as `local_resources/device.wvd`.

#### YouTube / SoundCloud — yt-dlp cookies

For resources requiring an authenticated session, export Netscape cookies to
`local_resources/cookies.txt`. `SOUNDCLOUD_OAUTH_TOKEN` can optionally provide a SoundCloud
OAuth token.

### Output, delivery and cleanup

Telegram progress messages report metadata lookup, each track/release, the selected
native engine, packaging, and structured failures. Subprocess output is consumed live
within the overall engine deadline; Telegram edits are throttled to two seconds. Deezer
lossless-to-MP3 fallbacks and unavailable tracks appear in progress updates and as
`Fallback:` / `Failures:` caption lines, including hosted-link deliveries. Captions identify the actual delivered source and inspect the
resulting audio, for example `FLAC 24-bit/96 kHz` or `MP3 320 kbps`, rather than merely
echoing the requested `ENABLE_FLAC` setting.

| Key | Default | Purpose |
|-----|---------|---------|
| `ENABLE_FLAC` | `1` | `1` → 24-bit/192 kHz FLAC where the platform offers it; `0` → 320 kbps MP3/AAC. Also passed to the image build as a build arg. |
| `FORMAT` | — | Explicit output format override. |
| `SEND_ALBUM_COVER` | `1` | Include `cover.jpg` in the archive. |
| `COPY_FILES_PATH` | — | Host directory for finished zips. When set, the archive is written there and the user gets a `FILE_LINK_TEMPLATE` URL instead of a Telegram upload. When empty, archives go to Telegram directly and albums over 48 MB are split into multipart zips. |
| `FILE_LINK_TEMPLATE` | — | URL template for the hosted link. Takes the archive basename as a single positional `{0}`, URL-quoted, e.g. `https://example.com/download/{0}`. If `COPY_FILES_PATH` is set but this is empty, the bot replies with the bare filename instead of a URL. |
| `AUTO_DELETE_ENABLED` | `1` | Purge bot messages and staged files afterwards. Also gates the background orphan sweeper — set it to `0` and nothing is ever deleted from disk automatically. |
| `AUTO_DELETE_SECONDS` | `1800` | Age at which purge happens (30 minutes). A background pass every 10 minutes additionally reclaims archives orphaned by a container restart: any `*.zip` in `COPY_FILES_PATH` older than this plus a 60-second grace buffer is unlinked, and idle staging directories under `TMP_DIR` are removed only while no download is active or queued. |
| `MAX_RETRIES` | `5` | Per-track download retries on the direct engines. |
| `ENGINE_TIMEOUT_SECONDS` | — | Wall-clock ceiling for one engine subprocess. |

### Concurrency

| Key | Default | Purpose |
|-----|---------|---------|
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Simultaneous downloads across all users. |
| `MAX_QUEUED_DOWNLOADS` | `1` | Callers waiting for a slot before they are refused. |
| `MAX_ARTIST_RELEASES` | `100` | Hard cap on releases pulled per artist link, enforced **before** the download starts. |
| `ARTIST_API_DELAY` | `0.6` | Seconds slept between releases in an artist walk. |
| `STREAMRIP_MAX_CONNECTIONS` | `3` | Parallel connections inside `streamrip`. |

Once `MAX_CONCURRENT_DOWNLOADS` slots are taken and `MAX_QUEUED_DOWNLOADS` callers are
already waiting, the next caller is told a download is running and asked to wait instead of
being queued silently. Both values are read at import time, so changing them does need a
restart (unlike the credential keys).

## Telegram commands

### User

| Command | Effect |
|---------|--------|
| `/start` | Public enabled/disabled platform board, artist availability, inline-mode hint. With a Deezer deep-link payload (`/start deezer_track_123`) it downloads that item directly. |
| `/help` | Same as `/start` without the payload path. |
| *(send a link)* | Resolve, route, download, return original audio or a ZIP/hosted link according to `FORMAT`. |
| `@bot album <query>` | Inline mode: search albums. Also `track` and `artist`. |
| `/cancel` | Cancel only your active/queued download in the current chat. `/cancel all` from a non-admin has no global effect. |

### Admin (`ADMIN_ID` only)

| `/status` | Enabled platforms, engines, credential presence, live active/queued slot counts, FLAC/auto-delete/hosting state, artist cap. Only the administrator can invoke it. |
| `/cancel all` | Cancel all active and queued downloads across every user and chat, including the admin's. |

| Command | Effect |
|---------|--------|
| `/whitelist` | List authorised IDs, resolving each to a name where Telegram allows. |
| `/allow <id> [<id> ...]` | Authorise IDs. Space-, comma- or newline-separated all parse. |
| `/block <id> [...]` | Revoke IDs. |
| `/blockall` | Revoke everything except the admin. |
| `/authenticatedeezer <arl>` | Save the ARL, enable Deezer and refresh its session. |
| `/authenticatetidal` | Interactive TV-device login through `link.tidal.com`. |
| `/authenticatetidal <json_or_fields>` | Paste OAuth JSON, key/value fields or a JWT credential set. |
| `/cancelauth` | Cancel the pending Tidal device polling task. |
| `/authenticateqobuz <email> <password>` | MD5-hash the password, discover missing app credentials and save Qobuz TOML. |
| `/authenticateqobuz <user_id> <token> [app_id] [secrets]` | Save direct Qobuz token authentication. |
| `/authenticateapple <media_user_token>` | Write Netscape Apple cookies and enable Apple Music. |
| `/authenticatesoundcloud <oauth_token>` | Save SoundCloud OAuth and enable SoundCloud. |

`/start` and `/help` intentionally expose only public feature availability. Engine names,
credential presence, active users, queue counts, hosting, and technical defaults appear only
in the admin `/status` report.

`/status` is the first thing to reach for when a platform misbehaves. Each line
shows whether the platform is enabled, its direct engine, and whether usable credentials
are configured (`creds ✓` or `no creds`).

Access control runs as middleware over every update, so a non-whitelisted user is refused
before any handler sees the message. Whitelist state persists to `data/whitelist.json`
(mounted, survives rebuilds) and membership changes in the admin's chats are picked up
automatically.

## Shortlinks and mobile share links

Links are normalised before routing, and tracking parameters (`utm_*`, `si`, `ref_*`,
`igshid`, `mc_*`, piwik/matomo) are stripped while meaningful ones are preserved:

| Platform | Accepted forms |
|----------|----------------|
| Deezer | `deezer.page.link/*`, `dzr.page.link/*`, `link.deezer.com/*`, `deezer.com/[intl/]track\|album\|artist/*` |
| Spotify | `spotify.link/*`, `spoti.fi/*`, `open.spotify.com/intl-xx/(track\|album\|artist)/*` — the `intl-xx/` segment is normalised away |
| Tidal | `t.idal.com/*`, `tidal.com/browse/(track\|album\|artist)/*`, `listen.tidal.com/*` → `https://tidal.com/(kind)/<id>` |
| Qobuz | `open.qobuz.com/*`, `play.qobuz.com/*`, `www.qobuz.com/<lang>/album/*` |
| Amazon Music | `music.amazon.*/(albums\|tracks\|artists)/*`, `amzn.to/*`, `a.co/*` |
| Apple Music | `music.apple.com/<storefront>/(album\|artist\|song)/*`, `itunes.apple.com/...`, `apple.co/*` |
| SoundCloud | `on.soundcloud.com/*`, `soundcloud.app.goo.gl/*`, `soundcloud.com/*` |

Two query parameters change what gets downloaded and are deliberately kept:

- **Apple** `/album/<slug>/<albumId>?i=<songId>` → the single track `songId`, not the whole album.
- **Amazon** `/albums/<albumAsin>?trackAsin=<trackAsin>` → the single track `trackAsin`, not the whole album.

## Archive layout

Every platform produces the same tree, so the analysis pipeline does not care where a
release came from:

```
<Artist> - <Album> [Year]/
├── 01 - <Artist> - <Title>.flac
├── 02 - <Artist> - <Title>.flac
└── cover.jpg
```

Artist discographies nest one directory per release:

```
<Artist>/
├── <Artist> - <Album A> [2019]/…
└── <Artist> - <Album B> [2022]/…
```

Archives are named `<Clean_Artist>_-_<Clean_Title>_(<8-char md5>).zip`, the hash making
repeat pulls of the same release distinguishable.

## Deployment

```bash
docker compose up -d --build     # build and start
docker compose logs -f worker    # watch request traffic and engine output
docker compose restart           # pick up token.env without recreating
docker compose down
```

The compose file binds:

| Host | Container | Why |
|------|-----------|-----|
| `./local_resources/` | `/usr/src/app/local_resources/` (rw) | Credential/cookie files for direct engines and yt-dlp. |
| `/var/www/html/.../download` | `/files` | `COPY_FILES_PATH` — the served archive directory. |
| `./data` | `/usr/src/app/data` | Persistent `whitelist.json`. |
| `./token.env` | `/usr/src/app/token.env` | `/authenticatedeezer` rewrites this file, so it must exist in-container. |

Base image is `python:3.13.13-alpine3.23` with `ffmpeg`, `nodejs` and `npm`, `git`
and `curl`, plus the build headers for Python audio-tagging and image packages.

### Dependency installation

The direct engines carry restrictive dependency pins (`streamrip` wants `Pillow<11` and
`tomlkit<0.8`; `pyplayready` wants `cryptography<46`). Tested call sites run with the newer
versions explicitly pinned in `requirements.txt`, while the Dockerfile installs the engine
wheels themselves with `--no-deps`. Removing the legacy Spotify audio stack also removes its
old protobuf constraint: `dotify-cli` 3.0.6 and `pywidevine` 1.9.0 use `protobuf` 6.33.6.

There is deliberately no `|| true` on those installs: a masked engine failure used to
produce a healthy-looking image that silently degraded every request. A broken engine now
fails the build, and the final `RUN` in the Dockerfile smoke-tests `rip`, `gamdl`, `dotify`,
Streamrip configuration, PlayReady, Widevine, and the native quality enum.

### Running tests

```bash
python3 -m unittest discover tests
```

477 offline tests — no network, no Telegram, no credentials. `tests/test_engine_install.py`
additionally exercises the real engine call sites (streamrip artwork scaling on Pillow 13,
its config round-trip on modern tomlkit, pyplayready key wrapping on cryptography 45,
pywidevine protobuf serialisation, and dotify native-quality/remux behavior) and skips
itself wherever those packages are absent, so a bare checkout can still run its portable
checks. The engine environment also needs `ffmpeg`/`ffprobe` for the audio-quality tests.

| Environment | Result |
|-------------|--------|
| Bare checkout (no engine packages) | 477 collected — pass with dependency-gated skips |
| Engine venv, `bin` on `PATH`, plus FFmpeg | 477 collected — 477 pass, 0 skip |

The bare-checkout skips are all dependency gates (`Crypto`, `tomlkit`, the engine CLIs). Every one
of them executes inside the image.

`test_live_and_example_have_the_same_key_sets` compares against the local `token.env`, which
is gitignored, so it `skipTest`s when absent — a clean clone or an unpacked release archive
reports the corresponding dependency skips rather than erroring. Every other config assertion
reads `token.env.example`, which is committed and always asserted.

**Put the venv's `bin` on `PATH` when running against an engine environment.** Invoking
`/path/to/venv/bin/python -m unittest` without it leaves `shutil.which("rip")` blind to the
engine CLIs, so `test_installed_engines_start` reports "no direct-engine CLIs installed" and
skips — a false negative that looks like a missing install rather than a missing `PATH`. The
same trap applies to `pycryptodome`-gated classes: they skip when `Crypto` is unresolvable,
so a green run with unexpected skips means the environment was not what you assumed.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| "I don't have permission" | ID not whitelisted. `/whitelist`, then `/allow <id>`. |
| Platform replies "not supported" | Its `*_ENABLED` flag is `0`, or no engine resolved. `/status`. |
| Artist link refused | That `*_ARTIST_DOWNLOAD_ENABLED` is `0` (the default on every platform). |
| "A download is already running" | `MAX_CONCURRENT_DOWNLOADS` reached. Raise it, or wait. |
| Archive never arrives | `FORMAT=zip` and `COPY_FILES_PATH`/`FILE_LINK_TEMPLATE` mismatch, or the `/files` bind is missing. |
| Platform reports missing credentials | Populate the named `token.env` key or real file in `local_resources/`. Non-Spotify platforms may opt into `FALLBACK_ENABLED=1` with a valid Deezer ARL; Spotify always requires its own web cookies and WVD. |
| Tidal 401/403 | Run `/authenticatetidal` for TV-device login. A `cid=3235` Web Player token cannot reliably access direct manifests. |
| Tidal 429 | Keep the built-in 30 requests/minute cap; reduce `STREAMRIP_MAX_CONNECTIONS` if needed. |
| Artist pull stopped early at 100 releases | `MAX_ARTIST_RELEASES`, intentional. |
| Build fails on an engine step | Read the failing `RUN`; the smoke test names the broken entry point. |

## Upstream and lineage

- [kernoeb/Telegramusic](https://github.com/kernoeb/Telegramusic) — the original Telegram bot shell.
- [kmille/deezer-downloader](https://github.com/kmille/deezer-downloader) — the Deezer ARL algorithm.
- [nathom/streamrip](https://github.com/nathom/streamrip), [martisetov/gamdl](https://github.com/martisetov/gamdl), [spotify-downloader/dotify-cli](https://github.com/spotify-downloader/dotify-cli) — the direct acquisition engines.

Licensed under the GNU General Public License v3.0 — see [LICENSE](LICENSE). Because this
bot is built on GPL upstreams, modifications must be distributed under the same terms.
