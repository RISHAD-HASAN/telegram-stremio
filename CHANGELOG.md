# Changelog

Notable changes to this fork. Version numbers follow the upstream project,
[weebzone/Telegram-Stremio](https://github.com/weebzone/Telegram-Stremio); a
release here contains the upstream release of the same number plus the changes
listed.

## 5.3.5

The previous version published in this repository was 5.0.5.

### Upstream

- Brought up to upstream release 5.3.5. This includes, among other things,
  Cloudflare Worker streaming, users' own bots with helper bots, the stream
  formatter and the Render blueprint. See the upstream repository for its
  release notes.

### Added

- `/missing`: lists the episodes missing from indexed shows; `find` searches
  the chats of the user session for them, `fill` copies them into the channel,
  `fill sorted` keeps the channel in episode order.
- `/resolve_nyaa` and the Batch Topic & Range Resolver: index a range of
  batch-release files using the file list of a nyaa.si torrent.
- `/sync_all`: AniList metadata sync and `.strm` / `.nfo` export for Jellyfin,
  Emby and Kodi, also available as automatic sync.
- Episode lookup by episode title for uploads whose file name has no show
  title.
- Watch-order grouping and filler flags for anime (AniList, Jikan).
- Offline library tools in `tools/`, including `dump_all_filenames.py`, which
  the other tools expected but which was missing from the tree.
- `TIMEZONE` setting for log lines and bot messages.
- Start-up checks: missing Python packages, an unsupported Python version,
  missing required settings and an unreachable MongoDB are reported in plain
  words and the server exits with code 1.
- Documentation under `docs/`, `CONTRIBUTING.md`, `SECURITY.md`, a test
  workflow for GitHub Actions and issue and pull request templates.

### Changed

- File names are cleaned and planned in dedicated modules
  (`helper/name_cleaning.py`, `helper/metadata/plan.py`) before the provider
  lookup.
- Absolute episode numbers are mapped into later seasons using the provider's
  season list instead of being filed under season 1.
- Specials: numbered specials (`SP1`, `OVA 2`) map to the n-th full-length
  special; specials with a title in the name are matched by that title;
  creditless openings and endings are skipped.
- Duplicate handling identifies a release by more than its title
  (`helper/release_identity.py`), so different resolutions of the same
  episode are kept side by side.
- `update.py` does nothing unless an update source is configured and never
  modifies an ordinary git checkout. Before, it deleted the `.git` directory
  of the folder it was run in.
- The Restart App button works without `uv` by falling back to the running
  Python interpreter.
- The default update source and the User-Agent sent to the anime APIs name
  this repository.
- The library scripts moved from the repository root to `tools/`, and the
  three standalone check scripts to `tests/manual/`. Commands change from
  `python indexer.py` to `python tools/indexer.py`. They can be started from
  any directory and write their reports to the current one.
- `--apply` of `tools/indexer.py`, `tools/repair_library.py` and
  `tools/reindex_full_pipeline.py` is switched off. These tools now only
  report. The write path stored streams under an identifier the server cannot
  decode, kept the wrongly filed entry, wrote to every storage database and
  took its backup after the change. `--apply` of the two nyaa tools is not
  affected.
- Path arguments of the library tools accept absolute Windows paths and `~`.
- `sample_config.env` lists every setting that is read, with the required
  ones first.
- `SETUP.md` was replaced by `docs/installation.md`.

### Security

- aiohttp, FastAPI, Starlette and python-multipart raised to the releases
  that close the advisories published for the earlier pins (request, Range
  and form parsing). `uv.lock`, which the Docker image installs from, had
  fallen behind `requirements.txt` and is aligned with it again; a test keeps
  the two in step.
- The generated API pages `/docs`, `/redoc` and `/openapi.json`, which listed
  every admin route to anyone, are no longer served.
- Cross-origin answers no longer carry `Access-Control-Allow-Credentials`.
  Players and Stremio Web still reach the add-on from other origins.
- The self-update has no source until one is entered. Before, a settings
  record without one fell back to this repository.
- The Docker build installs exactly what `uv.lock` records instead of
  resolving the dependencies again during the build.
- The test workflow pins its actions to commits, checks the dependencies for
  advisories on every push and weekly, and has read-only permissions.
  Dependabot keeps the action pins current.

### Fixed

- The page shown after too many failed sign-in attempts was built with a call
  form that newer Starlette releases no longer accept.
- The M3U playlist export answered without a login and, when called without a
  token, wrote the first access token in the database into the playlist. It
  now requires an admin session or a valid token.
- `/stream/stats` listed the files currently being streamed without a login.
- `/open/<app>/<type>/<id>` inserted its path parameter into the page without
  checking it.
- With MongoDB unreachable, the server used to log a successful connection and
  start with default settings, including the default admin login.
- `tools/diagnose_library.py` called a database method that does not exist and
  could not run. It now also stops when a storage database cannot be read,
  where a partial comparison would have reported every file as missing.
- `tools/reindex_full_pipeline.py` started its second step with an option that
  step does not accept, so a dry run always ended in an error.
- The bot menu listed `/set`, which has no handler, and missed `/sync_all`.
- `bump-version.py --help` ended in a traceback.
- `.dockerignore` misspelled the season2mpv configuration file.

### Removed

- `USER_SESSION_STRING` from the configuration template. It was not read; the
  user session is set up on the Settings page.
- The committed episode map cache under
  `Backend/helper/metadata/.episode_maps_cache/` (14 MB). It is downloaded on
  first use and is now ignored by git.
- `Backend/helper/custom_filter.py`, which nothing imported, and
  `Backend/tests/test_quality_standalone.py`.
