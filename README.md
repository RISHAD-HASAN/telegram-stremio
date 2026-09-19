# Telegram Stremio

A self-hosted media server built with **FastAPI**, **MongoDB**, and **PyroFork** that indexes media files from your Telegram channels and serves them directly as a **Stremio Addon** and **MPV media source**.

Streams media directly from Telegram without downloading files to local disk. Supports seeking, multi-part files, anime version tags, and whole-season playback queues.

---

## Features

- **Direct Telegram Streaming**: High-speed chunked streaming with multi-connection striping and DC-aware routing.
- **Whole-Season MPV Queue (`season2mpv.py`)**: Queue entire TV/anime seasons or movies directly into your own `mpv` player with subtitle track attachments.
- **Anime & Multi-Format Parsing**:
  - Handles versioned releases (`S01E01v2`, `001.v4`, `01v2`) without misrouting to Season 0.
  - Cleans franchise/studio prefixes (`Studio Ghibli - Movie 04 -`, `Makoto Shinkai -`).
  - Supports absolute episode numbers (`Bleach 024`, `One Piece 1172`), batch ranges (`01-24`), and SxE notations (`1x05`, `4x2`).
  - Filters hashtags (`#anime #1080p`) and foreign audio tags (`Audio Latino`, `Dual`, `Castellano`, `Subtitulado`).
  - Recognizes `.mkv`, `.mp4`, `.avi`, `.ts`, `.m2ts`, `.webm`, `.flv`, `.vob`, `.m4v`, `.mov`, `.wmv`.
- **Quality Hierarchy System**: Compares source tiers (`BluRay > WEB-DL > WEBRip > HDTV > CAM`) to prevent lower-quality re-uploads from replacing higher-quality versions.
- **Multi-Part / Split File Support**: Seamless virtual streaming for split files (`.part001.mkv`, `.cd1.avi`, `.mkv.001`, `.rar`).
- **Admin Web Dashboard**: Modern web interface for managing media, catalogs, API tokens, user requests, range scanning, and database maintenance.
- **Targeted Range Scanner & DB Pruner**: Scan specific message ID ranges (`/scan_files 100-250`) and prune dead streams or orphan records (`/tidy`).

---

## Quick Start (Windows)

### 1. Requirements
- Python 3.10 – 3.13
- MongoDB (Local instance at `mongodb://localhost:27017` or MongoDB Atlas)
- Telegram `API_ID` & `API_HASH` (from [my.telegram.org](https://my.telegram.org))
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Optional: [mpv.net](https://github.com/mpvnet-player/mpv.net) or [mpv](https://mpv.io) for whole-season playback

### 2. Configuration
Copy `sample_config.env` to `config.env` in the project root:

```ini
API_ID="1234567"
API_HASH="your_telegram_api_hash"
BOT_TOKEN="1234567890:AAF..."
OWNER_ID="123456789"
DATABASE="mongodb://localhost:27017/tracking,mongodb://localhost:27017/storage1"
AUTH_CHANNEL="1234567890"
PORT="8000"
BASE_URL="http://127.0.0.1:8000"
```

### 3. Installation & Run
- Double-click **`install.bat`** (or run `pip install -r requirements.txt`).
- Double-click **`start.bat`** (or run `python -m Backend`).
- Open `http://localhost:8000` in your browser.

---

## Whole-Season Playback with MPV (`season2mpv.py`)

Run `season2mpv.py` to queue full seasons or movies into your configured `mpv` player:

```bash
# Play all of Season 1 (auto-discovers local server token from MongoDB)
python season2mpv.py "Bleach" --season 1
python season2mpv.py "Bleach" 1

# Binge-watch all seasons and specials in sequence
python season2mpv.py "Bleach" --season all

# Play a movie with automatic subtitle attachment
python season2mpv.py "5 Centimeters per Second"

# Resume playback from episode 6 to the end of the season
python season2mpv.py "Bleach" --season 1 --start 6

# List all available seasons and episode counts
python season2mpv.py "Bleach" --list
```

---

## Bot Commands

- `/start` — Start bot, check server status, and get Stremio addon install link.
- `/set` — Manually assign IMDb/TMDb metadata to any forwarded file or message ID.
- `/scan_files <start>-<end>` — Targeted range scan (e.g. `/scan_files 1829-1855`).
- `/tidy` — Prune dead stream links and clean up orphaned database records.

---

## Docker Deployment (Optional)

```bash
docker compose up -d --build
```

---

## Tests

Run the test suite with:

```bash
python -m pytest
```

---

## License

GNU General Public License v3.0 (GPL-3.0).
