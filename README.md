# Telegram Stremio

Telegram Stremio is a self-hosted media server that uses Telegram channels as
storage. You forward video files to a channel, a bot indexes them, and the
server plays them in Stremio, Nuvio, mpv or any player that opens an M3U
playlist or a WebDAV share. Files are streamed straight from Telegram; nothing
is downloaded to the server's disk.

This repository is a fork of
[weebzone/Telegram-Stremio](https://github.com/weebzone/Telegram-Stremio),
kept in step with upstream release 5.3.5. It adds stricter file name matching
for anime and batch releases, a missing-episode finder, mpv season playback
and a set of offline library tools. See [Credits](#credits-and-licence).

## Why it exists

A Telegram channel can hold a large media collection, but Telegram offers no
library view: no posters, no seasons, no "next episode". Telegram Stremio
builds that view. It reads each file name, works out which movie or episode it
is, fetches the metadata and serves the result through the Stremio addon
protocol, so the collection behaves like any other streaming catalog.

## How it works

1. You add the bot as an administrator to one or more channels and list those
   channels in the settings.
2. Every video posted there is parsed (title, season, episode, quality) and
   matched against TMDb, TVDB, Cinemeta, Kitsu or AniList.
3. The match is stored in MongoDB together with the Telegram message it
   points to.
4. A player asks the server for a catalog, a title or a stream. For a stream
   the server fetches the file from Telegram in chunks and passes them on,
   with seeking.

The server is a single Python process: a FastAPI web application, a Telegram
bot (PyroFork) and a few background tasks. [docs/architecture.md](docs/architecture.md)
describes the parts.

## Features

- Stremio addon with catalogs, search, metadata, streams and subtitles, plus
  collection files for Nuvio.
- Web panel for the library, catalogs, access tokens, requests, settings and
  maintenance tools.
- Indexing of movies, series and anime, including absolute episode numbers,
  season packs, specials and OVAs, versioned releases (`v2`) and split files.
- Quality-aware duplicate handling: optionally replace a file only when the
  new one is better.
- Subtitle files (`.srt`, `.vtt`, `.ass`, `.ssa`, `.sub`) attached to the
  matching video.
- M3U playlist export, a read-only WebDAV share, and `.strm` / `.nfo` export
  for Jellyfin, Emby and Kodi.
- `season2mpv.py`, which plays a whole season or a movie in mpv from the
  command line.
- Optional subscription system with plans, approvals and per-token limits.
- Optional extras: more bot tokens for parallel streaming, a Telegram user
  session, users' own bots, an HTTP or MediaFlow proxy, Cloudflare Worker
  streaming.
- Bot commands for scanning a message range, cleaning dead links and finding
  missing episodes.

## Requirements

- Python 3.11, 3.12 or 3.13. Python 3.14 is not supported yet because a
  pinned dependency has no prebuilt package for it.
- MongoDB, either a local server or MongoDB Atlas. Two connection URIs are
  configured, one for tracking and one for the library; both may point at
  the same server.
- A Telegram account, `API_ID` and `API_HASH` from
  [my.telegram.org](https://my.telegram.org), and a bot token from
  [@BotFather](https://t.me/BotFather).
- Linux, macOS or Windows. Docker is supported as an alternative to a local
  Python installation.
- Optional: [mpv](https://mpv.io) for `season2mpv.py`, git for self-updates.

## Installation

```bash
git clone https://github.com/RISHAD-HASAN/telegram-stremio.git
cd telegram-stremio
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp sample_config.env config.env  # Windows: copy sample_config.env config.env
```

On Windows you can instead double-click `install.bat` and then `start.bat`.
With Docker, create `config.env` as below and run `docker compose up -d --build`.

[docs/installation.md](docs/installation.md) covers each route in detail,
including MongoDB, Docker, hosting platforms and updating.

## Initial configuration

Open `config.env` and fill in the required block:

```ini
API_ID="1234567"
API_HASH="your_api_hash"
BOT_TOKEN="1234567890:your_bot_token"
DATABASE="mongodb://localhost:27017/tracking,mongodb://localhost:27017/storage1"
OWNER_ID="123456789"
```

`DATABASE` takes two URIs separated by a comma: the tracking database first,
then a storage database. `OWNER_ID` is your numeric Telegram user ID; the
maintenance commands of the bot answer only the owner.

If something is missing, the server says what and stops:

```text
[ERROR] - The configuration is incomplete:
  - BOT_TOKEN is missing. Create a bot with @BotFather and paste its token.
```

## Starting and first use

```bash
python -m Backend
```

Run the command from the repository root. Then:

1. Open `http://localhost:8000` and sign in. The default login is `admin` /
   `admin`; change it under **Settings → Admin Authentication** straight away.
2. Create a private channel, add the bot as an administrator with permission
   to post, edit and delete messages, and add the channel ID under
   **Settings** (AUTH_CHANNELS).
3. Forward or upload a video to the channel. It appears in the dashboard once
   it is indexed.
4. Send `/start` to the bot. It replies with your addon link; open it in
   Stremio and choose Install.

Files that were in the channel before the bot joined can be indexed with the
Channel Scanner on the Tools page or with `/scan_files`.

## Configuration options

`config.env` holds what the server needs before it can reach the database.
Everything else is a runtime setting, edited on the Settings page and stored
in MongoDB.

| Setting | Required | Default | Purpose |
| --- | --- | --- | --- |
| `API_ID`, `API_HASH` | yes | | Telegram API credentials |
| `BOT_TOKEN` | yes | | Token of the main bot |
| `DATABASE` | yes | | Tracking URI, then one or more storage URIs |
| `OWNER_ID` | recommended | | Telegram user ID allowed to use the bot commands |
| `PORT` | no | `8000` | Port of the web server |
| `TIMEZONE` | no | `Asia/Kolkata` | Time zone of log lines and bot messages |

The remaining keys in `sample_config.env` (`BASE_URL`, `ADMIN_USERNAME`,
`ADMIN_PASSWORD`, `AUTH_CHANNEL`, `TMDB_API` and others) are first-start
values: they are copied into the database once, and the Settings page is
authoritative afterwards. [docs/configuration.md](docs/configuration.md) lists
all of them.

## Usage examples

Name files so the title and episode can be read from them:

```text
The Long Road (2019) 1080p BluRay.mkv            movie
Harbor Lights S02E03 720p WEB-DL.mkv             episode
[Group] Harbor Lights - 27 [1080p].mkv           anime, absolute episode number
Harbor Lights S00E01 1080p.mkv                   special
```

Index an older part of a channel, by message IDs:

```text
/scan_files 100-250
```

List the episodes missing from the indexed shows:

```text
/missing
```

Play a season in mpv:

```bash
python season2mpv.py "Harbor Lights" --season 2
```

The edit page of a title in Media Management has buttons that download an M3U
playlist of the movie, a season or one episode.

[docs/usage.md](docs/usage.md) explains file naming, the channel roles, every
bot command and the players.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `The configuration is incomplete` | A required value in `config.env` is empty |
| `MongoDB is not available` | MongoDB is not running, or `DATABASE` is wrong |
| `Missing Python packages` | `pip install -r requirements.txt` was not run in this environment |
| The bot does not answer `/start` | `OWNER_ID` is not your user ID |
| A file is not indexed | The channel is not listed in Settings, or the file name has no readable title |
| Streams do not start outside your network | `BASE_URL` still points at `127.0.0.1` |

[docs/troubleshooting.md](docs/troubleshooting.md) has the longer list.

## Documentation

- [Installation](docs/installation.md)
- [Configuration](docs/configuration.md)
- [Usage](docs/usage.md)
- [Library tools](docs/library-tools.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture and maintenance](docs/architecture.md)
- [Contributing](CONTRIBUTING.md), [Security](SECURITY.md), [Changelog](CHANGELOG.md)

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests need neither MongoDB nor Telegram.

## Credits and licence

Telegram Stremio is based on
[weebzone/Telegram-Stremio](https://github.com/weebzone/Telegram-Stremio) and
its contributors. Some optional features rely on services run by the upstream
project; [docs/configuration.md](docs/configuration.md#external-services) lists
them.

Licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE).

Use the software only with content you have the right to store and stream.
