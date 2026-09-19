# Windows Local Setup Guide

A guide for setting up Telegram Stremio directly on Windows with local MongoDB, Python 3.10–3.13, and MPV.

---

## 1. Prerequisites

1. **Python 3.10 – 3.13** (Download from [python.org](https://www.python.org/downloads/) — check *"Add Python to PATH"* during installation).
2. **MongoDB Community Server** (Download from [mongodb.com/try/download/community](https://www.mongodb.com/try/download/community) — install as a Service at `localhost:27017`).
3. **Telegram API Credentials**:
   - `API_ID` & `API_HASH` from [my.telegram.org](https://my.telegram.org).
   - `BOT_TOKEN` from [@BotFather](https://t.me/BotFather).
   - `OWNER_ID` from [@userinfobot](https://t.me/userinfobot).
4. **MPV Player** (Optional, for whole-season streaming):
   - [mpv.net](https://github.com/mpvnet-player/mpv.net) or [mpv](https://mpv.io).

---

## 2. Channel Setup

1. Create a private Telegram channel for your media.
2. Add your bot as an **Administrator** with full permissions (Post Messages, Delete Messages, Edit Messages).
3. Obtain your channel ID (e.g. `4386493465` or `-1004386493465`).

---

## 3. Server Configuration

Create `config.env` in the project root:

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

---

## 4. Install & Run

1. Run **`install.bat`** (or `pip install -r requirements.txt`).
2. Run **`start.bat`** (or `python -m Backend`).
3. Open `http://localhost:8000` to access the Admin Web Dashboard.

---

## 5. Streaming Whole Seasons with MPV

Run `season2mpv.py` to stream directly into your MPV player:

```bash
# Stream an entire season in sequence
python season2mpv.py "My Hero Academia" --season 1

# Stream all seasons
python season2mpv.py "My Hero Academia" --season all
```
