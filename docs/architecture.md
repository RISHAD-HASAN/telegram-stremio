# Architecture and maintenance

This document is for people who change the code. It describes how the parts
fit together, which modules are hard to change safely, and the routine
maintenance tasks.

## Overview

One Python process runs three things on one asyncio event loop:

- a **FastAPI** application served by uvicorn (web panel, admin API, Stremio
  addon, stream endpoints, WebDAV),
- one or more **PyroFork** Telegram clients (the main bot, optional extra
  bots, an optional user session),
- **background tasks** (dead-link check, subscription expiry, keep-alive ping,
  metadata and library sync).

MongoDB is the only persistent state, apart from the Telegram session files
PyroFork writes next to the code.

```text
Telegram channel ── new post ──▶ pyrofork/plugins/receiver.py
                                   │ name_cleaning → metadata/plan → metadata/entry
                                   ▼
                              helper/database.py ──▶ MongoDB (tracking + storage)
                                   ▲
Player ── /stremio/<token>/... ────┤  fastapi/routes/stremio_routes.py
Player ── /dl/<token>/<id>/<name> ─┘  fastapi/routes/stream_routes.py
                                         │ helper/custom_dl.py (ByteStreamer)
                                         ▼
                                      Telegram (file chunks)
```

## Entry points

| Command | File | Purpose |
| --- | --- | --- |
| `python -m Backend` | `Backend/__main__.py` | The server |
| `python update.py` | `update.py` | Self-update; run by `start.sh` and by the Restart App button |
| `python season2mpv.py` | `season2mpv.py` | mpv client, standard library only |
| `python bump-version.py` | `bump-version.py` | Raises the version in `pyproject.toml`, `Backend/__init__.py`, `uv.lock` |
| `python tools/<name>.py` | `tools/` | Offline library tools, see [library-tools.md](library-tools.md) |

### Start-up order

`Backend/__init__.py` checks the Python version and that the core packages can
be imported. `Backend/__main__.py` then:

1. checks the required settings (`config.config_problems`) and that both
   databases answer (`config.database_problems`); exits with code 1 otherwise,
2. connects the databases and loads the settings (`SettingsManager.initialize`;
   on an empty database the settings are seeded from the environment),
3. installs the session middleware with the stored session secret,
4. starts the main bot, the user session if one is saved, the extra bot
   clients and the helper bots,
5. sets the bot's command menu,
6. starts the web server and the background tasks, then idles.

The process must be started from the repository root: PyroFork loads the
plugins from `Backend/pyrofork/plugins` and the log goes to `./log.txt`.

## Directory layout

```text
Backend/
  __init__.py          version, shared state, environment check
  __main__.py          start-up and shutdown
  config.py            config.env / environment, pre-flight checks
  logger.py            logging to console and log.txt, time zone
  fastapi/
    main.py            app object, page routes, most admin route declarations
    routes/            api_routes (admin API logic), stremio_routes (addon),
                       stream_routes (/dl, /sub, /thumb), template_routes (pages),
                       webdav_routes, cf_routes (Cloudflare Worker callbacks)
    security/          credentials.py (admin session), tokens.py (access tokens)
    templates/         Jinja pages of the web panel
    themes.py          colour themes
  helper/              everything else; see below
    metadata/          name → title resolution
      providers/       one module per metadata source
  pyrofork/
    bot.py             client objects
    clients.py         extra bot clients and their load balancing
    plugins/           message handlers: receiver, start, subscription, group_security
  tests/               quality checker tests
tests/                 pytest suite
  manual/              standalone check scripts, not collected by pytest
tools/                 offline library tools
docs/                  documentation
```

## How a file is indexed

1. `pyrofork/plugins/receiver.py` receives a post in an auth channel. The
   caption, or the file name when there is no caption, is the name to work
   with.
2. `helper/name_cleaning.py` strips channel tags, URLs, emoji and decoration.
3. `helper/metadata/parse.py` extracts season, episode, quality and flags
   (special, split part, version).
4. `helper/metadata/plan.py` decides what kind of thing the file is: movie,
   episode, special, something to skip (creditless opening and so on).
5. `helper/metadata/entry.py` (`metadata()`) resolves the title. The provider
   order depends on the kind of file: anime channels try Kitsu, then TVDB,
   TMDb, Cinemeta; elsewhere movies try TMDb, then Cinemeta, and series try
   TVDB, then Cinemeta, then TMDb. Providers without a key are passed over. `resolvers.py` holds the per-provider logic, `common.py` the
   title scoring and thresholds.
6. `helper/metadata/episode_maps.py` converts absolute episode numbers and
   Kitsu entries (one per season or cour) into the season and episode of the
   TVDB/IMDb numbering, using the ani.zip mapping data. The data is cached in
   `Backend/helper/metadata/.episode_maps_cache/` and rebuilt when missing.
7. `helper/database.py` inserts or updates the document. With Replace Mode,
   `helper/quality_checker.py` and `helper/release_identity.py` decide
   whether the new file replaces an existing one.

Related modules: `split_files.py` and `virtual_dl.py` (multi-part files),
`zip_stream.py` (stored zip archives), `subtitles.py`, `gap_filler.py`
(`/missing`), `nyaa_resolver.py` (`/resolve_nyaa`), `manual_add.py` (manual
sessions), `skip_channel.py`.

## How a stream is served

`/dl/<token>/<id>/<name>` in `stream_routes.py` validates the token
(`security/tokens.py`: existence, expiry, limits), decodes the id
(`helper/encrypt.py`) into a chat and message, picks the Telegram client with
the lowest load and fewest recent failures (`select_best_client` in the same
file) and streams the byte range through
`ByteStreamer` in `helper/custom_dl.py`. Usage is recorded for the token's
limits and statistics (`helper/utils.py`, `helper/analytics.py`).

Optional paths: a user's own bots (`helper/own_bots.py`), a Cloudflare Worker
(`helper/cf_stream.py`, `cf_live.py`, `routes/cf_routes.py`), an HTTP or
MediaFlow proxy (URL rewriting in `stremio_routes.py`).

## Database

Every URI in `DATABASE` is opened with the database name `dbFyvio`.

- **Tracking** (first URI): `settings` (one document, `_id: "app_settings"`),
  `state` (which storage database is current), tokens, users, subscription
  plans, requests, custom catalogs, analytics.
- **Storage** (second and later URIs): collections `movie` and `tv`. A TV
  document nests `seasons[].episodes[].telegram[]`; a movie has `telegram[]`.
  Each `telegram` entry is one file: quality label, encoded message
  reference, name, size and, for split files, the parts.
  `helper/modal.py` has the Pydantic models.

When a storage database fills up, new titles go to the next one;
`db.current_db_index` tracks it.

## Settings

`helper/settings_manager.py` owns the runtime settings. `_DEFAULTS` is the
full list with default values. Adding a setting means: add it to `_DEFAULTS`,
expose it in the settings API in `api_routes.py`, add the control to
`templates/settings.html`. Values in `config.env` only seed the document on
first start (`_seed_from_env`).

## External APIs

| API | Module | Notes |
| --- | --- | --- |
| Telegram MTProto | PyroFork | Flood waits are handled per client |
| TMDb | `metadata/providers/tmdb.py` | Needs a key |
| TVDB | `metadata/providers/tvdb.py` | Needs a key |
| Cinemeta | `metadata/providers/cinemeta.py` | No key; responses are redirects, follow them |
| Kitsu, ani.zip | `metadata/providers/kitsu.py`, `episode_maps.py` | No key. Failed ani.zip replies must not be cached. |
| AniList | `metadata/providers/anilist.py` | GraphQL; rate limited |
| Jikan | `metadata/providers/jikan.py` | Rate limited; used for filler flags |
| Cinemeta episode titles | `metadata/providers/episode_lookup.py` | Finds a show from an episode title when the file name has no show title |
| nyaa.si | `helper/nyaa_resolver.py`, `tools/nyaa_*.py` | HTML and RSS; be polite with request rates |
| GitHub | `helper/version_check.py` | Reads the version file of `PROJECT_REPO` |

`PROJECT_REPO` and `PROJECT_BRANCH` in `Backend/config.py` name this
repository. They are used for the version check and in the User-Agent sent to
the anime APIs. Change them when the repository moves.

## Modules that need care

- **`helper/metadata/plan.py`, `parse.py`, `episode_maps.py`.** Small changes
  shift where real files land. The tests in `tests/test_name_pipeline.py`,
  `test_episode_resolution.py`, `test_special_placement.py` and
  `test_title_matching.py` pin the current behaviour with invented show
  names; add a case there before changing a rule. Thresholds worth knowing:
  a titled special takes the season 0 slot whose name scores at least 0.85
  and leads the runner-up by 0.03; a numbered special pointing at a clip
  shorter than ten minutes is mapped to the n-th full-length special.
- **`helper/custom_dl.py`, `pyrofork/clients.py`.** Streaming performance and
  Telegram rate limits depend on the chunking and client selection here.
  They can only be tested against real Telegram.
- **`helper/database.py`** is large and holds most write paths, including the
  replace and delete logic. `tests/test_server.py` and
  `tests/test_rescan_purge.py` cover parts of it with fake collections.
- **`update.py`** resets a deployed folder to another repository's code. The
  safety checks (no update source, ordinary checkout, option-like values) are
  covered by `tests/test_updater.py`; keep them passing.
- **`requirements.txt` pins.** The direct dependencies are pinned exactly and
  `uv.lock` records the same versions; `tests/test_dependency_pins.py` fails
  when the two disagree. From FastAPI 0.16x on, `app.routes` no longer lists
  the routes that came in through `include_router`, so a route count read
  from that list is not a health check. `tests/test_access_control.py` walks
  the routers instead and asks every GET address without a login.

## Known limits

- An absolute episode number beyond the last season the provider lists falls
  back to season 1.
- Files named as a range (`Show E001-E167.mkv`) are not split into episodes.
- Second halves of split-cour seasons keep the episode numbers of the first
  half's season when the provider lists them as one season.
- Titled specials are matched against English special names only.
- A show that no provider lists can still be matched to another title with a
  similar name through the Cinemeta fallback. Seen with an invented name
  during testing (match score 0.75). An ID in the caption prevents it.
- The rules for extras look at the name without bracketed tags. With a bare
  tag, `Show Movie OVA 1080p.mkv` is indexed as a film, while
  `Show Movie OVA [1080p].mkv` is skipped as intended.
- `Show OP 01.mkv` is read as an episode of "Show OP" (only `NCOP` / `NCED`
  and names that are nothing but `OP 01` are skipped).
- `tools/indexer.py --apply` is switched off. `apply_to_mongodb()` would need
  the server's `encode_string()` for stream ids, message ids in the export,
  removal of the old entry, the right storage database and a backup taken
  before the change.
- `asyncio.get_event_loop()` at import time and `datetime.utcnow()` are
  deprecated in recent Python versions; they still work on 3.11 to 3.13.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```

The suite runs without MongoDB, Telegram or network access. `uv` users can
run `uv sync` and `uv run pytest` instead; `uv.lock` is committed.

A local instance for manual testing needs a MongoDB server and a test bot.
Use a separate bot token and separate databases for it, never the production
ones: the server writes settings and the bot menu at start-up.

## Common tasks

**Release a version.** Update `CHANGELOG.md`, run
`python bump-version.py patch` (or `minor`, `major`), run the tests, commit,
tag `v<version>`. Running installations compare their version with
`Backend/__init__.py` on the branch of their update source, or of this
repository when they have none, so the update notice appears once the bumped
version is on that branch.

**Take changes from upstream.** Add
`https://github.com/weebzone/Telegram-Stremio` as a remote and compare release
by release. The histories are unrelated, so apply upstream changes as
patches, file by file, and never resolve a conflict by taking a whole upstream
file: this fork's indexing rules live in the same files. Comment lines differ
in style (`#-----` upstream, `#` here), which makes plain three-way merges
noisy. Run the full test suite afterwards.

**Add a bot command.** Add the handler in `Backend/pyrofork/plugins/`, add the
menu entry in `setup_bot_commands` (`helper/pyro.py`), document it in
`docs/usage.md`.

**Add a metadata provider.** Add a module under `metadata/providers/`, wire it
into `resolvers.py` and the order in `entry.py`, and add tests with mocked
responses.

**Check for dependency updates.** Change one pin at a time in
`requirements.txt` and `pyproject.toml`, run
`uv lock --upgrade-package <name>==<version>` so that `uv.lock` follows, run
the tests and start the server once. `pip-audit -r requirements.txt` lists
published advisories for the pinned versions; the test workflow runs it on
every push and once a week.
