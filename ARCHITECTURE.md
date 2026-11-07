# DeadMoon Download Bot — Architecture

Technical reference for the acquisition pipeline. Written to be loaded as context by an
AI coding agent: it states the invariants that are easy to break silently, and names the
file and function that owns each one. Operational configuration lives in
[README.md](README.md).

Lineage: the bot shell came from `kernoeb/Telegramusic`, the Deezer algorithm from
`kmille/deezer-downloader`. Everything under `dl_utils/`, `handlers/streaming.py` and
`tests/` is local work.

## 1. Purpose and shape

A whitelisted Telegram bot turns a streaming-platform link into a zip of tagged audio,
delivered back to Telegram or as a hosted link. Archives are staged under a temp tree,
consumed by an external MIR analysis pipeline, and purged on a timer (`AUTO_DELETE_SECONDS`,
default 1800s). The bot is a **transient acquisition front-end**, not a library: no
retention path exists, and nothing should be added that creates one.

Two constraints shape the whole design:

1. **One link → one archive, identically laid out, regardless of platform.** The downstream
   analyzer parses folder and filename structure, so layout drift is a pipeline break, not
   a cosmetic difference. `dl_utils/archive_delivery.py` is the single owner of layout.
2. **Scarce, rate-limited upstream and one Telegram identity.** Download concurrency is
   globally bounded by a scheduler (`utils.py`) and every handler must participate in it,
   because a leaked slot reduces capacity for every user for the rest of the process
   lifetime.

## 2. File tree and module responsibilities

```
main.py                     entry point; access control, admin/user commands, router wiring,
                            temp-dir bootstrap, background cleanup task
bot.py                      constructs Bot + Dispatcher; no logic
utils.py                    config readers, i18n, the download scheduler, TMP_DIR/paths
handlers/
  deezer.py                 Deezer: ARL engine, track/album/artist handlers, shortlinks,
                            inline search, zip delivery + auto-delete scheduling
  streaming.py              Qobuz/Tidal/Amazon/Spotify/Apple router + /status report text
  yt_dlp.py                 YouTube and SoundCloud handlers via yt-dlp
dl_utils/
  link_resolver.py          offline classify() + async shortlink expansion (aiohttp+certifi)
  provider_engines.py       Direct CLI dispatch + optional Deezer ARL fallback, staging,
                            workdir → EngineResult discovery
  metadata_bridge.py        canonical metadata from public endpoints (iTunes, Deezer,
                            Odesli/song.link, Spotify oEmbed) — names the archive
  archive_delivery.py       build_release_entries, zip/multipart writers, captions, hashing,
                            schedule_messages_deletion / schedule_file_deletion
  deezer_download.py        Deezer cryptographic download algorithm (ARC4 + MD5 obfuscation)
  deezer_utils.py           Deezer helpers: key derivation, lyric/album fetch, search
arl_fetcher/                optional bun/TypeScript helper that logs in and prints an ARL
local_resources/            operator credential/cookie files + sanitized templates
langs.json                  string → locale → text; __() indexes it
tests/                      392 offline tests; no network, no Telegram, no credentials
```

Boundary rules that matter:

- `handlers/*` may not implement zipping, naming, or deletion. They resolve, schedule,
  download, and call `archive_delivery`.
- `dl_utils/provider_engines.py` returns an `EngineResult` (files + best-effort metadata).
  It never touches Telegram and never writes to `COPY_FILES_PATH`.
- `dl_utils/archive_delivery.py` never downloads. It takes metadata + file lists.
- Only `utils.py` and `dl_utils/platform_config.py` read configuration. Handlers read env
  vars directly and become untestable.

## 3. Request lifecycle

```
Telegram update
  └─ AccessControlMiddleware (main.py)         whitelist gate; returns before any handler
      └─ router chain: youtube → soundcloud → deezer → streaming
          └─ offline parse: link_resolver.extract_url() → classify()
              │  (no network; canonical URLs resolve here)
              └─ else shortlink handler → link_resolver.resolve_link()   [HTTP, follows redirects]
                  └─ platform_enabled() check → refuse without a slot if disabled
                      └─ kind == artist? → artist_download_enabled() check → refuse without a slot
                          └─ slot = request_download(user_id)            ← SYNC, no awaits
                              │  if not slot.granted: __("running_download"); return
                              └─ await announce_if_queued(event, slot)   ← blocks until promoted
                                  └─ workdir = staging_dir(platform, kind, object_id)
                                      └─ metadata_bridge.release_info()   (non-fatal on failure)
                                          └─ provider_engines.download_release()
                                              ├─ direct: streamrip / dotify-cli / gamdl / amazon-cli
                                              └─ optional: Deezer ARL cross-match fallback
                                                  └─ archive_delivery.build_release_entries()
                                                      └─ deliver_zip / create_and_send_zip
                                                          ├─ COPY_FILES_PATH set → hashed zip + FILE_LINK_TEMPLATE link
                                                          └─ unset → Telegram upload, <48MB multipart splits
                                                              └─ schedule_messages_deletion / schedule_file_deletion
                                                                  └─ finally: release_download(slot); cleanup_dir(workdir)
```

Concrete anchors, in `handlers/streaming.py::_handle_release`:

```python
slot = request_download(user_id)
if not slot.granted:
    await event.answer(__("running_download"), reply_markup=types.ReplyKeyboardRemove())
    return                                    # ← no slot was taken; nothing to release
workdir = staging_dir(platform, kind, link.object_id)
try:
    await announce_if_queued(event, slot)
    ...
finally:
    release_download(slot)
    cleanup_dir(workdir)
```

Ordering inside the `try` is load-bearing in two places:

- **Metadata before audio.** `metadata_bridge.release_info()` is cheap and its result names
  the archive, the caption, and the folder layout. Its failure is swallowed with a log line
  because `provider_engines.infer_release_from_paths()` can reconstruct identity from the
  file tree. Making it fatal would fail requests that would otherwise succeed.
- **`release_download` then `cleanup_dir`, both in `finally`.** Reversed, or with either in
  the `try`, an exception leaks capacity or leaves staged audio on disk.

### Deezer's path is the same shape

`handlers/deezer.py` predates the multi-platform work and keeps its own ARL engine, but it
uses the identical scheduler contract and the shared `archive_delivery` helpers. Do not
fork a second copy of the zip logic for a new platform — import it. `handlers/deezer.py`
re-exports `build_release_entries`, `create_and_send_zip`, `create_and_send_artist_zip`,
`send_track_audio`, `send_album_audio`, `schedule_messages_deletion`,
`schedule_file_deletion` and `_safe_delete` for exactly this reason, and its public
behaviour must stay backward-compatible: `/start deezer_track_<id>` deep links and the
inline-mode handlers call into `handle_track_link` / `handle_album_link` directly.

## 4. Configuration: `utils.py`

```python
read_token_env_value(key) -> str          # parse token.env on every call
get_env_or_token_file(key, default="")    # token.env first, then os.environ, then default
get_bool_env(key, default)                # 1/true/yes/on → True
get_int_env(key, default) / get_float_env(key, default)
```

`get_env_or_token_file` prefers the **file** over the process environment. That is
deliberate: `env_file:` in compose injects values only at container creation, so without
this, rotating a token would require recreating the container. `/newarl` rewrites
`token.env` in place and the next request sees the new ARL.

Consequences to respect:

- Anything read through these helpers is hot-reloadable. Keep credential reads inside the
  request path; do not cache them in module globals.
- Things read at **import time** are not: `MAX_CONCURRENT_DOWNLOADS`,
  `MAX_QUEUED_DOWNLOADS`, `LANG`, `TMP_DIR`, `COPY_FILES_PATH` resolution and the whitelist
  path are module-level. Changing them needs a restart. Do not move them into the request
  path casually — the scheduler reads the limits on every allocation.
- Tests neutralise the file precedence by repointing `utils.TOKEN_ENV_PATH` at a
  nonexistent path, which makes the process environment the only source. `TOKEN_ENV_PATH`
  is resolved on each call, so this works.

`dl_utils/platform_config.py` is the only consumer of the per-platform key grammar:
`<PREFIX>_ENABLED`, `DEEZER_ARTIST_DOWNLOAD_ENABLED`, and `FALLBACK_ENABLED`, plus
`has_credentials(platform)` predicates per platform. `DIRECT_ENGINES` maps each platform
to its only engine. File credentials count only when present, non-empty, and not an
unchanged example template. The pre-filled Apple cookie path follows the same rule, so a
missing/example file does not mask a usable `APPLE_MEDIA_USER_TOKEN`. Add new keys here,
not in handlers.

## 5. Concurrency invariants (`utils.py`)

State: `_active_slots: dict[tuple, DownloadSlot]`, `_queued_slots: deque[DownloadSlot]`,
both module-level and unguarded by a lock.

The lock-freedom is the invariant, and it holds because of one rule:

> **`request_download()` and `release_download()` contain no `await`.**

Because they are fully synchronous, the event loop cannot interleave another coroutine
between reading and mutating scheduler state. Two callers can never be granted the same
slot, and no `asyncio.Lock` is needed. Inserting an `await` into either function is a
silent race, not a style regression — it will double-grant slots under load and the test
suite's single-threaded assertions will not catch it.

Other invariants:

- **`request_download` is the only allocator.** Never append to `_active_slots` /
  `_queued_slots` directly.
- **Every granted slot is released exactly once.** `release_download` is idempotent
  (`slot._released` guard) and tolerates `None`, so `finally: release_download(slot)` is
  always safe. Passing a slot that was never granted is also handled (the `ValueError`
  branch) — it returns without promoting, which is correct.
- **`SLOT_USER_BUSY`**: one in-flight request per `(user_id, chat_id)`. The same user can
  have independent work in a DM and different groups without one cancelling another.
- **Cancellation is chat-scoped.** `/cancel` kills the slot process tree, cancels its handler
  task, cleans registered staging directories, releases it, and promotes the next queued
  slot synchronously. Admin `/cancel all` selects every slot; a non-admin `all` argument
  never widens scope.
- **Slot capacity is not a global per-user lock.** Legacy non-Telegram callers may omit
  `chat_id`, but handlers always provide it.
- **`granted` is `SLOT_START or SLOT_QUEUED`.** A queued slot is already owned; refusing to
  release it later leaks queue capacity.
- **Promotion is a `while` loop, not an `if`** — raising `MAX_CONCURRENT_DOWNLOADS` at
  runtime promotes multiple waiters in one release.
- `scheduler_snapshot()` is read-only and backs `/status`; it must not mutate.

`announce_if_queued(event, slot)` is the only awaiting step in the scheduler surface, and it
awaits `slot.wait_for_turn()` — an `asyncio.Event` set by `_grant()` on promotion. It returns
immediately for non-queued slots. The "you are queued" message is deleted once the turn
arrives, and the delete failure is ignored deliberately: the message is gone either way.

## 6. Link resolution (`dl_utils/link_resolver.py`)

Two passes, and the order matters for latency and for testability:

1. **Offline `classify(url)`** — regex over canonical hosts, returns `ResolvedLink`
   (`platform`, `kind`, `object_id`, `track_id`, `storefront`, normalised `url`). No
   network. Every canonical link is answered here.
2. **Async `resolve_link(url)`** — only for shortener hosts (`SHORTLINK_HOSTS` /
   `is_shortlink()`). Follows redirects with `aiohttp` on a `certifi` SSL context, then
   re-runs `classify()` on the final URL.

`strip_tracking_params()` keeps `_MEANINGFUL_QUERY_KEYS` (`i`, `trackasin`, `albumasin`) and
drops `utm_*`, `ref_*`, `igshid`, `mc_*`, piwik/matomo. Those three kept keys are semantic:

- Apple `/album/<slug>/<id>?i=<songId>` → single track, not album.
- Amazon `/albums/<asin>?trackAsin=<tasin>` → single track, not album.

Losing either silently converts a 1-track request into a full-album download. There are
regression tests for both; do not "simplify" the query handling to a blanket strip.

Spotify `intl-xx/` locales are normalised away so one pattern serves every regional link.
`extract_url()` pulls the first URL out of arbitrary message text, because mobile clients
frequently append a caption or the link arrives inside forwarded text.

`_effective_kind(link)` in `handlers/streaming.py` is where the `?i=` / `trackAsin` downgrade
to `KIND_TRACK` is applied — the resolver preserves the parameter, the handler interprets it.

## 7. Engine dispatch (`dl_utils/provider_engines.py`)

`download_release(platform, url, kind, workdir, progress_cb=None)`:

```
if not pcfg.has_credentials(platform):
    if not FALLBACK_ENABLED: raise EngineError(required token.env key/file)
    report missing credentials; return await download_via_deezer_fallback(...)
engine = _TOKEN_ENGINES[platform]                 # functools.partial normalises arity
try:    return await engine(url, kind, workdir)  # adapter passes slot progress_cb
except: if not FALLBACK_ENABLED: raise
        _clear_partial(workdir)                   # no failed audio in fallback
        report failure; return await download_via_deezer_fallback(...)
```

`FALLBACK_ENABLED=0` is the default. There is no secondary provider chain: missing
credentials fail before an engine runs, and direct-engine errors propagate unchanged. With
fallback enabled, uncredentialed requests and direct failures each make one Deezer ARL
attempt. `download_via_deezer_fallback` independently requires `DEEZER_TOKEN` and supports
tracks/albums only. It uses Song.link, ISRC, or artist/title search to find the matching
Deezer object, then reuses the existing ARL downloader.

- **`_clear_partial(workdir)` before a post-failure fallback is mandatory.** Otherwise
  `discover_release()` can treat half an album from the failed engine as a complete result.
- `_TOKEN_ENGINES` entries all have the signature `(url, kind, workdir)`. Qobuz and Tidal
  share `download_via_streamrip`, bound with `functools.partial`.
- `platform_status()` exposes `enabled`, `has_credentials`, `engine`, and
  `artist_download` (non-null only for Deezer). `/status` never derives routing from a mode.

Engines: `download_via_streamrip` (Qobuz/Tidal), `download_via_dotify` (Spotify),
`download_via_gamdl` (Apple), and `download_via_amazon_cli` (Amazon). Deezer uses
`deezer_download.py`; YouTube/SoundCloud use `handlers/yt_dlp.py`. Subprocess engines go
through `_run_subprocess`, which merges stderr into stdout, reads lines asynchronously,
forwards track progress, kills the whole process group on timeout/cancellation, returns
only the last 25 lines, and is bounded by `ENGINE_TIMEOUT_SECONDS`.

`download_artist_discography(..., progress_cb=None)` prefixes each track update with the
current release. `dl_utils.dotify_shim` reads public Spotify embed metadata for album/track
identity and `isPlayable`, and stages Netscape cookies or a legacy-path JSON `sp_dc` bundle
with `0600` permissions. The engine uses dotify-cli's web session and WVD, never Librespot
audio. It invokes dotify once per playable track with request-local `HOME`,
`XDG_CONFIG_HOME`, and `XDG_CACHE_HOME` directories (`0700`) under a fresh per-invocation
runtime directory in `workdir`, preventing shared config/cache races. Unavailable/failed
tracks are recorded in `metadata.tracks_api_data`, and a partial album is returned when at
least one file exists. Standalone tracks retain their song title while preserving the
parent album context. `archive_delivery.summarize_track_issues` turns the missing IDs into
the standard `Failures:` caption lines.

Spotify selects `flac-mp4-24,flac-mp4,vorbis-high,aac-high,aac-medium` with FLAC preference
and `vorbis-high,aac-high,aac-medium` otherwise. A Widevine 403/access-denied response for a
lossless tier triggers one same-track retry using the configured `aac-high,aac-medium` tiers;
the AAC list is then reused for the remaining tracks in that release invocation.
`--audio-remux-mode ffmpeg` stream-copies the decrypted native codec. There is no
lossy-to-lossy or lossy-to-FLAC transcode. Spotify
is excluded from both global Deezer fallback branches regardless of `FALLBACK_ENABLED`.
The old `download_via_zotify` symbol is only an alias to `download_via_dotify` for external
callers; it cannot invoke the retired protocol.

`_which()` checks `shutil.which` then `<repo>/bin/<name>`. Keep that order so a pinned local
engine build can be tested against a checkout.

### Gamdl 3.9.1 arguments

`_gamdl_command()` owns the Apple argv. With `ENABLE_FLAC=1` it passes
`--song-codec-priority alac,aac-web`; otherwise it passes `aac-web`. These are actual
`SongCodec` values. `APPLE_CODEC_PRIORITY` is validated against the 3.9.1 set before a
subprocess starts. `--no-album-folder-template` requires a template string; it is not a
boolean flag. Tests parse the complete generated argv with Gamdl's real Click command.

### Discovery: workdir → EngineResult

`find_audio()` → `discover_release()` (audio + cover + release dir) →
`infer_release_from_paths()` / `tracks_from_paths()` → `EngineResult`. Filenames are parsed
by `_split_track_stem()` on the `NN - Artist - Title` convention the engines are configured
to emit. This is the fallback identity path when `metadata_bridge` fails; it must stay
symmetric with `archive_delivery`'s writer, or a round-trip breaks.

### Dependency strategy (why the Dockerfile looks like that)

The direct engines declare restrictive pins: `streamrip` wants
`Pillow<11`/`tomlkit<0.8`/`rich<14` and `pyplayready` wants `cryptography<46`.

Tested call sites use newer versions: `requirements.txt` pins every engine *dependency*
explicitly, and the Dockerfile installs the engine wheels themselves with `--no-deps`.
Each override is backed by a real call-site test in `tests/test_engine_install.py`
(streamrip `downscale_image` on Pillow 13, its config round-trip on tomlkit 0.15,
pyplayready key wrapping on cryptography 45, pywidevine protobuf serialisation).

The retired Spotify audio stack's old protobuf cap is gone. `dotify-cli` 3.0.6,
`pywidevine` 1.9.0, and their generated protocol modules use `protobuf` 6.33.6. Dotify's
FFmpeg remux implementation is inspected by the offline suite to ensure it uses stream
copy rather than an audio re-encode.

There is no `|| true` on any engine install. Masked failures previously produced a healthy
image that silently degraded every request; the final `RUN` smoke-tests the engine entry
points and critical imports so a broken engine fails the build instead.

## 8. Packaging and delivery (`dl_utils/archive_delivery.py`)

Canonical layout, every platform:

```
single/album : <Artist> - <Album> [Year]/NN - <Artist> - <Title>.<ext> + cover.jpg
artist       : <Artist>/<Artist> - <Album> [Year]/NN - <Artist> - <Title>.<ext> + cover.jpg
```

`build_release_entries(metadata, dl_tracks_info, parent_dir=None)` produces the entry list;
`parent_dir` is what nests a release under an artist folder. `_write_zip_sync` and
`_write_multipart_zips_sync` are the only writers, and both run under
`asyncio.to_thread` — never call them directly from a coroutine.

`hashed_archive_basename()` yields `<Clean_Artist>_-_<Clean_Title>_(<8-char md5>).zip`. The
hash is not decoration: it makes repeat pulls of the same release distinguishable on a
publicly served `COPY_FILES_PATH`, where an overwrite would race the analyzer's read.

Delivery branches on `COPY_FILES_PATH`: set → write the zip there and reply with
`FILE_LINK_TEMPLATE.format(quote(basename))` — one positional `{0}`, URL-quoted, since
artist/title can carry spaces and punctuation; unset → upload to Telegram, splitting into
multipart zips under `TELEGRAM_MAX_UPLOAD_BYTES` (48 MB, leaving headroom under the bot's
50 MB cap). If `COPY_FILES_PATH` is set but `FILE_LINK_TEMPLATE` is empty, the bot replies
with the hosted *filename* rather than a URL rather than formatting a blank template.

Auto-delete: `schedule_messages_deletion` (bot messages, including the hosted-link message)
and `schedule_file_deletion` (staged archive) both fire after `AUTO_DELETE_SECONDS` when
`AUTO_DELETE_ENABLED`. `_safe_delete` swallows Telegram errors — a message already removed
must not abort cleanup.

`main.py::background_cleanup` is a `CLEANUP_INTERVAL_SECONDS` (600 s) ticker whose body is
`run_cleanup_sweeps()`, split out so the work is testable without waiting on the interval.
Each pass runs both sweeps under `asyncio.to_thread` — they are blocking filesystem walks,
and a fat `COPY_FILES_PATH` must never stall Telegram polling. The whole pass is wrapped in
`try/except`: a raising sweep is logged and the ticker keeps looping, because a dead task
would silently end orphan reclamation for the rest of the container's life.

Two sweeps, two scopes, two different safety rules:

- `sweep_orphaned_archives()` unlinks aged `*.zip` files directly inside `COPY_FILES_PATH`
  whose `st_mtime` is older than `auto_delete_seconds() + SWEEP_GRACE_SECONDS` (60 s). It is
  the reclaim path for archives written before an unclean shutdown, which
  `schedule_file_deletion()` no longer knows about. It is **not** gated on scheduler
  occupancy: no in-flight handler writes to `COPY_FILES_PATH`, so it can never disturb a
  download. It no-ops when `auto_delete_enabled()` is false or `copy_files_path()` is empty
  (direct-Telegram mode). Entries are matched case-insensitively on `.zip`, must satisfy
  `is_file(follow_symlinks=False)`, and are stat'd with `follow_symlinks=False` — a symlink
  in a world-readable served directory pointing outside it is not this sweeper's to delete.
  Per-entry `OSError` is logged and the scan continues; `FileNotFoundError` means
  `schedule_file_deletion()` won the race and is treated as success.
- `sweep_orphaned_staging()` removes `<platform>/<kind>/<id>` request directories under
  `TMP_DIR` on the same age rule, but **refuses to run at all unless
  `scheduler_snapshot()` reports zero active and zero queued downloads**. A live workdir is
  indistinguishable from an orphaned one from outside the handler that owns it, and deleting
  the wrong one corrupts an in-flight archive. Queued counts as busy: a queued slot becomes
  active within seconds, and the promotion may already be underway. Only leaf request dirs
  are candidates — the per-platform skeletons `main.py` creates at startup are recreated on
  every boot and are preserved, as are loose files at the `TMP_DIR` root.

Both sweeps are covered in `tests/test_background_sweep.py`, including a live-ticker test
that shrinks `CLEANUP_INTERVAL_SECONDS` to 0 so `background_cleanup()` performs a real pass.

## 9. Adding a provider

1. `dl_utils/platform_config.py`: add the platform constant, `PLATFORMS`,
   `PLATFORM_LABELS`, `_DEFAULT_ENABLED`, `_DEFAULT_ARTIST_DOWNLOAD`, `DIRECT_ENGINES`,
   `_ENV_PREFIX`, a `has_<platform>_credentials()`, and register it in `_HAS_CREDENTIALS`.
2. `dl_utils/link_resolver.py`: extend `CANONICAL_HOST_REGEX`, the per-platform normaliser,
   and `SHORTLINK_HOSTS` for its shortener. Preserve any query key that changes semantics.
3. `dl_utils/provider_engines.py`: write `download_via_<x>(url, kind, workdir) ->
   EngineResult`, return through `_result_from_workdir(...)`, register in `_TOKEN_ENGINES`
   with the 3-arg signature. Every platform has a direct engine.
4. `handlers/streaming.py`: append the platform to the `_register_platform_handler` loop.
   That registers the canonical filter; the shortlink handler already dispatches by
   `ROUTED_PLATFORMS`.
5. `main.py`: add the platform to the `TMP_DIR` subtree bootstrap list.
6. `token.env` **and** `token.env.example`: document every new key. The example must stay
   credential-free.
7. Tests: link classification, artist-toggle refusal without a slot, archive layout, and the
   engine's discovery path — all offline, with the subprocess boundary faked.

Copy `_handle_release` rather than writing a new flow. The slot contract, the
metadata-before-audio order, and the `finally` cleanup are the parts that are easy to get
wrong and hard to notice.

## 10. Testing conventions

`python3 -m unittest discover tests` — 392 tests, no network, no Telegram, no credentials.

- `tests/test_platform_config.py` repoints `utils.TOKEN_ENV_PATH` at a nonexistent file so
  the environment is the only config source. Follow this; never assert against a real
  `token.env`.
- `tests/test_engine_install.py` is the only suite that touches third-party engine code. It
  guards each class with `skipUnless(_have(...))` so a bare checkout skips engine
  assertions and the image runs them. Keep that shape: engine behaviour must be asserted at
  the call site, not the import site, because the whole `--no-deps` strategy depends on
  runtime compatibility that an import cannot prove.
- Handler tests fake the engine seam (`download_release`) and assert the scheduler contract:
  slot refused → no workdir created; disabled platform/artist → refusal **before**
  `request_download`; `finally` released even on engine error.
- `tests/test_archive_layout.py::TestDeezerReExports` guards the Deezer backward-compatibility
  requirement. `handlers/deezer.py` imports the packaging symbols from `archive_delivery`, so
  the assertion is object identity (`assertIs`), not mere presence — a rebound alias would let
  Deezer fork the layout silently. The whole class is `skipUnless(find_spec("Crypto"))`
  because importing `handlers.deezer` pulls `dl_utils.deezer_download`, which needs
  pycryptodome; a bare checkout without it skips rather than errors. This is the only test
  that imports `handlers.deezer` at all.
- `tests/test_background_sweep.py` covers the orphan sweeper. Two shapes are worth
  preserving: the sweeps are called directly with an injected `now` so age boundaries are
  asserted deterministically instead of by sleeping, and the live-ticker test patches
  `CLEANUP_INTERVAL_SECONDS` to 0 rather than parsing `background_cleanup`'s source —
  patching the interval is why that constant lives at module level. The `.zip` matcher is
  tested against a symlink pointing outside `COPY_FILES_PATH`, which is the case that
  forces `follow_symlinks=False` on both the `is_file` check and the `stat`.
- Verify changes in both environments — bare `python3` and a venv built from the
  Dockerfile's install sequence. The `TestStreamripConfig` 0600 assertion is the standing
  example of a bug that only appears once the engines are actually installed.
- **Run the engine environment with its `bin` on `PATH`.** `_cli()` in
  `tests/test_engine_install.py` mirrors `provider_engines._which()` and resolves engine
  CLIs through `shutil.which`, so invoking `/venv/bin/python -m unittest` without the venv
  on `PATH` silently skips `test_installed_engines_start`. Unexpected skips mean the
  environment is not what you assumed, not that the engine is absent.
- **`main.py` is not import-safe by default.** It `sys.exit(1)`s at import when `ADMIN_ID`
  resolves to 0 and creates `data/whitelist.json` relative to the CWD. Because discovery is
  one process, an unguarded import aborts the entire suite rather than failing one class.
  `tests/test_token_env_write.py` shows the required harness: `chdir` into a temp dir, point
  `utils.TOKEN_ENV_PATH` at a nonexistent file, set `ADMIN_ID` in the environment, and catch
  `SystemExit` to degrade to a skip.

## 11. Known sharp edges

- **Awaiting inside the scheduler** breaks slot exclusivity silently. Highest-severity edge
  in the codebase.
- **Leaking a slot** permanently subtracts capacity; only a restart recovers it.
- **`token.env` precedence over env vars** surprises operators who edit compose instead of
  the file. Both must agree, or the file wins.
- **Layout drift** in `build_release_entries` breaks the external analyzer without breaking
  any bot test.
- **Partial audio surviving a failed engine** produces a wrong-named archive;
  `_clear_partial` is the only defence.
- **Credential-bearing temp files must be created at 0600, not chmod'ed after writing.**
  `write_text()`/`open(..., "w")` land at the umask default (0644) and `workdir` sits under
  the shared `TMP_DIR`, so a post-write chmod leaves a real window where the Qobuz auth
  token, the Tidal refresh token or the Apple `media-user-token` is world-readable. Use
  `os.open(path, O_WRONLY|O_CREAT|O_TRUNC, 0o600)` — the mode applies at creation, and
  because umask only clears bits it cannot weaken 0600. Applied in
  `provider_engines._streamrip_config()` and `_apple_cookies_file()`, and in
  `main.set_deezer_token()` for the fresh-`token.env` case. `os.open` deliberately does not
  alter an *existing* file's mode, which is what keeps `set_deezer_token` from chmod'ing the
  host's bind-mounted `token.env`.
- **`MAX_ARTIST_RELEASES` is enforced before download**, so a 400-release artist costs
  nothing. Moving that check after the fetch turns a polite refusal into wasted work.
- **Artist toggles default off everywhere but Deezer.** Intentional: discography pulls are
  the only unbounded-cost operation in the bot.
- **`data/` is gitignored** and holds only runtime state such as the whitelist. Real files
  under `local_resources/` are also ignored; only the README and sanitized templates ship.
