#!/usr/bin/env python3
"""
Play whole seasons or movies from a Telegram Stremio server in mpv.

Builds an ordered mpv playlist from the server's addon API and launches mpv,
so episodes play one after another without touching anything. Movies work too
and get their subtitle track attached automatically.

Standard library only, so no pip installs are needed (Python 3.10+).

Examples:
  # play all of Season 2 in mpv (auto-detects local server & token)
  python season2mpv.py "Fire Force" --season 2
  python season2mpv.py "Fire Force" 2

  # play all seasons (binge mode)
  python season2mpv.py "Fire Force" --season all

  # play a movie (e.g. Only Yesterday, Kiki's Delivery Service, Oppenheimer)
  python season2mpv.py "Kiki's Delivery Service"

  # play a specific episode range
  python season2mpv.py "One Piece" --episodes 1000-1050

  # play with explicit addon manifest URL (auto-saved for future runs)
  python season2mpv.py "Bleach" --season 1 --addon "http://127.0.0.1:8000/stremio/TOKEN/manifest.json"

  # list all available seasons on your server
  python season2mpv.py "Fire Force" --list

  # resume: start at episode 6 and play to the end of the season
  python season2mpv.py "Fire Force" --season 1 --start 6

  # skip filler episodes (requires is_filler data from indexer)
  python season2mpv.py "Bleach" --season all --no-filler

  # play in recommended watch order (requires watch_order_group/index from indexer)
  python season2mpv.py "Monogatari" --season all --watch-order

  # both: filler-free, watch-order sorted
  python season2mpv.py "Bleach" --season all --no-filler --watch-order

The .m3u playlist file stays on disk afterwards, so you can also run
`mpv "Fire Force S02.m3u"` yourself any time.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

USER_AGENT = "season2mpv/1.3"
TIMEOUT = 30
RETRIES = 2

COMBINED_SEASON = 0
COMBINED_EPISODE_BASE = 1000


def log(msg: str) -> None:
    print(msg, flush=True)


def get_config_file() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, ".season2mpv_config.json")


def load_cached_addon() -> str | None:
    cfg = get_config_file()
    if os.path.exists(cfg):
        try:
            with open(cfg, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("addon_url")
        except Exception:
            pass
    return None


def save_cached_addon(addon_url: str) -> None:
    try:
        cfg = get_config_file()
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"addon_url": addon_url}, f, indent=2)
    except Exception:
        pass


def auto_detect_addon(passed_addon: str | None = None, passed_token: str | None = None) -> str:
    if passed_token:
        url = f"http://127.0.0.1:8000/stremio/{passed_token.strip()}/manifest.json"
        save_cached_addon(url)
        return url

    if passed_addon:
        clean = passed_addon.strip()
        if not clean.startswith("http") and "/" not in clean:
            # Bare token passed to --addon
            clean = f"http://127.0.0.1:8000/stremio/{clean}/manifest.json"
        save_cached_addon(clean)
        return clean

    # 1. Environment variable
    env_url = os.environ.get("STREMIO_ADDON_URL")
    if env_url:
        return env_url.strip()

    # 2. Cached config from previous runs
    cached = load_cached_addon()
    if cached:
        return cached

    # 3. Query local MongoDB if pymongo is installed / available
    try:
        import pymongo
        client = pymongo.MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=1200)
        db_names = client.list_database_names()
        for db_name in db_names:
            if db_name in ("admin", "local", "config"):
                continue
            db = client[db_name]
            cols = db.list_collection_names()
            for col_name in ("api_tokens", "tokens"):
                if col_name in cols:
                    tok_doc = db[col_name].find_one({"token": {"$exists": True}})
                    if tok_doc and tok_doc.get("token"):
                        addon_url = f"http://127.0.0.1:8000/stremio/{tok_doc['token']}/manifest.json"
                        save_cached_addon(addon_url)
                        log(f"💡 Auto-discovered active token from local MongoDB: {tok_doc['token'][:8]}...")
                        return addon_url
    except Exception:
        pass

    # 4. Try default manifest on port 8000
    try:
        req = Request("http://127.0.0.1:8000/stremio/manifest.json", headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                addon_url = "http://127.0.0.1:8000/stremio/manifest.json"
                save_cached_addon(addon_url)
                return addon_url
    except Exception:
        pass

    raise SystemExit(
        "❌ Missing Addon URL!\n\n"
        "To get your Addon Manifest URL:\n"
        "1. Open your Telegram Stremio web UI (http://localhost:8000)\n"
        "2. In the 'Home' or 'Tokens' tab, click 'Copy Addon URL'\n"
        "   (e.g. http://127.0.0.1:8000/stremio/YOUR_TOKEN/manifest.json)\n"
        "3. Run with --addon ONCE:\n"
        "   python season2mpv.py \"Fire Force\" --season 2 --addon \"http://127.0.0.1:8000/stremio/YOUR_TOKEN/manifest.json\"\n\n"
        "💡 Note: The URL is saved automatically, so you will never have to pass --addon again!"
    )


def http_json(url: str):
    """GET a URL and parse JSON, with retries. Returns dict or raises SystemExit."""
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except HTTPError as e:
            if e.code in (404, 400):
                raise SystemExit(f"Server returned HTTP {e.code} for:\n  {url}")
            last_err = e
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
        if attempt < RETRIES:
            time.sleep(1.0 + attempt)
    raise SystemExit(f"Failed to fetch {url}: {last_err}")


def parse_addon(addon_url: str):
    """Extract (base_url, token) from a manifest URL like
    http://host:8000/stremio/TOKEN/manifest.json"""
    p = urlparse(addon_url.strip())
    if not p.scheme or not p.netloc:
        raise SystemExit("Invalid addon URL - expected e.g. http://127.0.0.1:8000/stremio/TOKEN/manifest.json")
    parts = [x for x in p.path.split("/") if x]
    if len(parts) >= 3 and parts[0] == "stremio" and parts[1] and parts[-1] == "manifest.json":
        return f"{p.scheme}://{p.netloc}", parts[1]
    if len(parts) >= 2 and parts[0] == "stremio" and parts[-1] == "manifest.json":
        return f"{p.scheme}://{p.netloc}", ""
    raise SystemExit(
        "Could not find the token in that URL.\n"
        "Use the exact addon/manifest URL from your Telegram Stremio web UI (Tokens or Home tab)."
    )


def _score_title(name: str, query: str) -> int:
    name_low = name.lower().strip()
    q_low = query.lower().strip()
    if name_low == q_low:
        return 0
    if name_low.startswith(q_low):
        return 1
    if q_low in name_low:
        return 2
    overlap = len(set(name_low.split()) & set(q_low.split()))
    return 3 + (0 if overlap else 1)


def search_media(base: str, token: str, query: str):
    """Find matching series or movie via the addon catalog search."""
    prefix = f"/stremio/{token}" if token else "/stremio"
    # 1. Search series catalogs
    for catalog in ("top_series", "latest_series"):
        url = f"{base}{prefix}/catalog/series/{catalog}/search={quote(query)}.json"
        try:
            data = http_json(url)
            metas = [m for m in (data.get("metas") or []) if m.get("id")]
            if metas:
                metas.sort(key=lambda m: _score_title(m.get("name") or "", query))
                return "series", metas[0]
        except Exception:
            continue

    # 2. Search movie catalogs
    for catalog in ("top_movies", "latest_movies"):
        url = f"{base}{prefix}/catalog/movie/{catalog}/search={quote(query)}.json"
        try:
            data = http_json(url)
            metas = [m for m in (data.get("metas") or []) if m.get("id")]
            if metas:
                metas.sort(key=lambda m: _score_title(m.get("name") or "", query))
                return "movie", metas[0]
        except Exception:
            continue

    raise SystemExit(
        f"No media matched {query!r}.\n"
        "Check the exact title in your Telegram Stremio library."
    )


def fetch_meta_videos(base: str, token: str, meta_id: str):
    prefix = f"/stremio/{token}" if token else "/stremio"
    url = f"{base}{prefix}/meta/series/{meta_id}.json"
    data = http_json(url)
    meta = data.get("meta") or {}
    videos = meta.get("videos") or []
    if not videos:
        raise SystemExit("The server returned this title but with no episodes - indexing may have failed. "
                         "Check the Web UI Media Management tab.")
    return meta, videos


def ep_key(v):
    s = v.get("season")
    e = v.get("episode")
    try:
        s = int(s) if s is not None else 0
    except (TypeError, ValueError):
        s = 0
    try:
        e = float(e) if e is not None else 0.0
    except (TypeError, ValueError):
        e = 0.0
    return (s, e)


def is_filler(v) -> bool:
    """Return True if the video is flagged as filler by the indexer."""
    return bool(v.get("is_filler", False))


def filler_type(v) -> str:
    """Return the filler type string ('filler', 'recap', 'canon', None)."""
    return v.get("filler_type")


def watch_order_key(v):
    """Sort key for watch-order playback.

    Uses (watch_order_group, watch_order_index) when available, falling
    back to (season, episode) for entries without watch-order data.
    Returns (group_sort, index_sort, season, episode) so that entries
    with watch-order data sort before those without, and within each
    group entries are ordered by their watch-order index.
    """
    group = v.get("watch_order_group")
    index = v.get("watch_order_index")
    s, e = ep_key(v)

    # Entries with watch-order data get group_sort=0 (sorted first),
    # entries without get group_sort=1 (sorted after).
    if group is not None and index is not None:
        try:
            idx = int(index)
        except (TypeError, ValueError):
            idx = 0
        return (0, str(group), idx, s, e)
    else:
        return (1, "", 0, s, e)


def is_combined(v) -> bool:
    s, e = ep_key(v)
    return s == COMBINED_SEASON and e >= COMBINED_EPISODE_BASE


def fmt_ep(v) -> str:
    s, e = ep_key(v)
    if is_combined(v):
        return f"S00E{int(e):03d}[combined]"
    if isinstance(e, float) and e % 1 != 0:
        int_part = int(e)
        dec_part = str(e).split(".")[1]
        return f"S{max(s, 0):02d}E{int_part:02d}.{dec_part}"
    if s == 0:
        return f"S00E{int(e):02d}"
    return f"S{max(s, 0):02d}E{int(e):02d}"


def label(v) -> str:
    t = (v.get("title") or "").strip()
    return t if t else f"E{ep_key(v)[1]:02d}"


def pick_stream(streams):
    """Pick the best stream: skip donation/notice entries and proxy duplicates."""
    all_parts = pick_all_part_streams(streams)
    return all_parts[0] if all_parts else None


def pick_all_part_streams(streams):
    """If a movie/episode has multiple parts (e.g. Part 1/4, Part 2/4...), returns all parts in order. Otherwise returns [best_stream]."""
    good = []
    for s in streams or []:
        name = s.get("name") or ""
        url = s.get("url") or ""
        if not url:
            continue
        low = name.lower()
        if any(k in low for k in ("donation", "limit reached", "plan expired", "join required", "expired")):
            continue
        if not ("/dl/" in url or "/sub/" in url or "tg://" in url or "/stream/" in url or "http" in url):
            continue
        if "(proxy)" in low:
            continue
        good.append(s)

    if not good:
        return []

    # Check if there are explicit "Part X/Y" entries
    part_streams = [s for s in good if re.search(r'part\s*\d+/\d+', s.get("name", "") + s.get("title", ""), re.IGNORECASE)]
    if part_streams and len(part_streams) > 1:
        def get_part_num(s):
            m = re.search(r'part\s*(\d+)/\d+', s.get("name", "") + s.get("title", ""), re.IGNORECASE)
            return int(m.group(1)) if m else 0
        part_streams.sort(key=get_part_num)
        return part_streams

    return [good[0]]


def fetch_episode_url(base: str, token: str, video_id: str):
    prefix = f"/stremio/{token}" if token else "/stremio"
    url = f"{base}{prefix}/stream/series/{video_id}.json"
    data = http_json(url)
    return pick_stream(data.get("streams"))


def fetch_movie_streams(base: str, token: str, meta_id: str):
    prefix = f"/stremio/{token}" if token else "/stremio"
    url = f"{base}{prefix}/stream/movie/{meta_id}.json"
    data = http_json(url)
    return pick_all_part_streams(data.get("streams"))


def fetch_movie_url(base: str, token: str, meta_id: str):
    prefix = f"/stremio/{token}" if token else "/stremio"
    url = f"{base}{prefix}/stream/movie/{meta_id}.json"
    data = http_json(url)
    return pick_stream(data.get("streams"))


def parse_episode_spec(spec: str):
    """'1-3,7,10-12' -> [1,2,3,7,10,11,12]"""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out.extend(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            out.append(int(part))
        else:
            raise SystemExit(f"Bad --episodes entry: {part!r} (use e.g. 1-3,7,10-12)")
    return sorted(set(out))


def write_m3u(path: str, title: str, entries):
    """entries: list of (display_title, url)"""
    lines = ["#EXTM3U"]
    for t, u in entries:
        t = t.replace("\n", " ")
        lines.append(f"#EXTINF:-1,{t}")
        lines.append(u)
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def find_mpv(explicit: str = None) -> str:
    if explicit:
        p = os.path.expandvars(os.path.expanduser(explicit))
        if os.path.exists(p) or shutil.which(p):
            return p
        raise SystemExit(f"--mpv path not found: {p}")
    for cand in ("mpv", "mpvnet", "mpv.exe", "mpvnet.exe"):
        p = shutil.which(cand)
        if p:
            return p
    if os.name == "nt":
        env = os.environ
        cands = [
            os.path.join(env.get("ProgramFiles", r"C:\Program Files"), "mpv", "mpv.exe"),
            os.path.join(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "mpv", "mpv.exe"),
            os.path.join(env.get("ProgramFiles", r"C:\Program Files"), "mpv.net", "mpvnet.exe"),
            os.path.join(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "mpv.net", "mpvnet.exe"),
            os.path.join(env.get("LOCALAPPDATA", ""), "Programs", "mpv.net", "mpvnet.exe"),
            os.path.join(env.get("LOCALAPPDATA", ""), "mpv", "mpv.exe"),
            os.path.join(env.get("USERPROFILE", ""), r"scoop\apps\mpv\current\mpv.exe"),
            os.path.join(env.get("USERPROFILE", ""), r"scoop\apps\mpv.net\current\mpvnet.exe"),
            r"C:\mpv\mpv.exe",
            r"D:\mpv\mpv.exe",
        ]
        for c in cands:
            if c and os.path.exists(c):
                return c
    for c in ("/usr/bin/mpv", "/usr/local/bin/mpv", "/opt/homebrew/bin/mpv"):
        if os.path.exists(c):
            return c
    raise SystemExit(
        "mpv not found. Install mpv (Windows: mpv.net, https://mpv.net) and put it on PATH, "
        "or pass --mpv \"C:\\path\\to\\mpvnet.exe\"."
    )


def launch_mpv(mpv: str, args) -> None:
    if os.name == "nt":
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        proc = subprocess.Popen(
            [mpv] + args,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    else:
        proc = subprocess.Popen([mpv] + args, close_fds=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(f"🎬 Launched mpv (pid {proc.pid}).")


def safe_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n]+', " ", s).strip()
    return re.sub(r"\s+", " ", s) or "playlist"


def main():
    ap = argparse.ArgumentParser(
        description="Build an mpv season playlist or play movies from a Telegram Stremio server directly in mpv.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples", 1)[1] if "Examples" in __doc__ else None,
    )
    ap.add_argument("title_pos", nargs="?", default=None, metavar="TITLE",
                    help="Title of anime, series, or movie to search for (e.g. 'Fire Force', 'Code Geass')")
    ap.add_argument("season_pos", nargs="?", default=None, metavar="SEASON",
                    help="Optional season number (e.g. 1, 2, 'all')")
    ap.add_argument("--title", "-t", default=None, help="Title to search for (optional flag alternative to positional TITLE)")
    ap.add_argument("--addon", "-a", default=None,
                    help="Addon manifest URL: http://127.0.0.1:8000/stremio/TOKEN/manifest.json (Auto-discovered if omitted)")
    ap.add_argument("--token", default=None,
                    help="Your Stremio Token (alternative to full --addon URL)")
    ap.add_argument("--season", "-s", default=None,
                    help="Season number (e.g. 1), 0 for OVA, or 'all'. Default: 1 (or the only season).")
    ap.add_argument("--start", type=int, default=None,
                    help="Within the season, start at this episode number (plays to the end)")
    ap.add_argument("--episodes", "-e", default=None,
                    help="Comma list / ranges of episodes to play, e.g. 1-3,7,10-12")
    ap.add_argument("--only", type=int, default=None, metavar="N",
                    help="Play a single episode (auto-attaches subtitles when available)")
    ap.add_argument("--sub-lang", default=None,
                    help='Preferred subtitle language (e.g. "en", "hindi", "arabic")')
    ap.add_argument("--out", "-o", default=None, help="Playlist file path (default: <Show> Sxx.m3u in current dir)")
    ap.add_argument("--no-play", action="store_true", help="Write the playlist but do not launch mpv")
    ap.add_argument("--list", "-l", action="store_true", help="List available seasons and exit")
    ap.add_argument("--min-quality", type=int, default=0, choices=(0, 360, 480, 720, 1080, 2160),
                    help="Skip streams below this resolution (default: 0 = keep best of everything)")
    ap.add_argument("--no-filler", action="store_true",
                    help="Skip filler/recap episodes (requires is_filler data from indexer)")
    ap.add_argument("--watch-order", action="store_true",
                    help="Sort by recommended watch order (requires watch_order_group/index from indexer)")
    ap.add_argument("--mpv", default=None, help="Path to mpv/mpvnet executable")
    ap.add_argument("--mpv-args", default="", help='Extra mpv args, e.g. "--cache-secs=60"')
    args = ap.parse_args()

    title = args.title or args.title_pos
    if not title:
        ap.print_help()
        sys.exit(1)

    season_arg = args.season or args.season_pos

    addon_url = auto_detect_addon(args.addon, args.token)
    base, token = parse_addon(addon_url)

    log(f"🔎 Searching for {title!r} …")
    media_type, meta = search_media(base, token, title)
    media_name = meta.get("name") or title
    meta_id = meta["id"]
    log(f"   matched {media_type}: {media_name} ({meta_id})")

    prefix = f"/stremio/{token}" if token else "/stremio"

    # MOVIE HANDLING
    if media_type == "movie":
        streams = fetch_movie_streams(base, token, meta_id)
        if not streams:
            raise SystemExit(f"No stream found for movie {media_name!r}. Check Web UI Media tab.")
        cmd_args = []
        sub_args = []
        try:
            sub_data = http_json(f"{base}{prefix}/subtitles/movie/{meta_id}.json")
            subs = [s for s in (sub_data.get("subtitles") or []) if s.get("url")]
            if subs:
                def sub_pref(s):
                    lang = (s.get("lang") or "").lower()
                    if args.sub_lang and args.sub_lang.lower() in lang:
                        return 0
                    if lang.startswith("eng") or lang.startswith("en") or lang == "english":
                        return 1
                    return 2
                subs.sort(key=sub_pref)
                chosen = subs[0]
                sub_args = [f"--sub-file={chosen['url']}"]
                log(f"   subtitles: {chosen['lang']}")
        except SystemExit:
            pass

        if len(streams) > 1:
            log(f"🎬 Movie has {len(streams)} parts - building playlist …")
            entries = [(f"{media_name} (Part {idx}/{len(streams)})", st["url"]) for idx, st in enumerate(streams, 1)]
            out = args.out or os.path.join(os.getcwd(), f"{safe_filename(media_name)}.m3u")
            write_m3u(out, media_name, entries)
            log(f"✅ Playlist written: {out} ({len(entries)} parts)")
            if args.no_play:
                return
            mpv = find_mpv(args.mpv)
            extra = args.mpv_args.split() if args.mpv_args else []
            launch_mpv(mpv, extra + sub_args + [out])
            return
        else:
            stream = streams[0]
            if args.mpv_args:
                cmd_args += args.mpv_args.split()
            cmd_args += sub_args + [stream["url"]]
            if args.no_play:
                log(f"🎬 Would play:\n   {stream['url']}  {' '.join(sub_args)}")
                return
            mpv = find_mpv(args.mpv)
            launch_mpv(mpv, cmd_args)
            return

    # SERIES HANDLING
    log("📚 Loading episodes …")
    _, videos = fetch_meta_videos(base, token, meta_id)

    seasons = {}
    for v in videos:
        s, _ = ep_key(v)
        seasons.setdefault(s, []).append(v)

    if args.list:
        log(f"\n{media_name} - seasons on your server:")
        for s in sorted(seasons, key=lambda x: (x,)):
            eps = seasons[s]
            label_s = "OVA/Combined" if s == 0 else f"Season {s}"
            nums = [ep_key(e)[1] for e in eps]
            filler_count = sum(1 for e in eps if is_filler(e))
            filler_str = f"  [{filler_count} filler]" if filler_count else ""
            log(f"   {label_s:15s}  {len(eps):3d} episodes  "
                f"(ep {min(nums)}-{max(nums)}){filler_str}")
        log(f'\nUse:  python season2mpv.py "{media_name}" --season <N>      (or --season all)')
        return

    # choose the working set of episodes
    use_watch_order = args.watch_order
    if season_arg == "all":
        selected = sorted(videos, key=watch_order_key if use_watch_order else ep_key)
        season_label = "ALL"
    else:
        if season_arg is None:
            if 1 in seasons:
                season = 1
            elif len(seasons) == 1:
                season = next(iter(seasons))
            else:
                avail = ", ".join(f"S{s}" for s in sorted(seasons))
                raise SystemExit(f"Season 1 not found. Available: {avail} - "
                                 "pass --season <N> or --season all.")
        else:
            try:
                season = int(season_arg)
            except ValueError:
                raise SystemExit(f"Invalid season '{season_arg}'. Must be a number or 'all'.")
            if season not in seasons:
                avail = ", ".join(f"S{s}" for s in sorted(seasons))
                raise SystemExit(f"Season {season} not found for {media_name}. Available: {avail}")
        selected = sorted(seasons[season], key=watch_order_key if use_watch_order else ep_key)
        season_label = "all" if season == 0 else str(season)

    # filters
    if args.only is not None:
        selected = [v for v in selected if ep_key(v)[1] == args.only]
    elif args.episodes:
        keep = set(parse_episode_spec(args.episodes))
        selected = [v for v in selected if ep_key(v)[1] in keep]
    elif args.start is not None:
        selected = [v for v in selected if ep_key(v)[1] >= args.start]

    # --no-filler: skip filler/recap episodes
    if args.no_filler:
        before = len(selected)
        selected = [v for v in selected if not is_filler(v)]
        skipped = before - len(selected)
        if skipped:
            log(f"   ⏭️  Skipped {skipped} filler/recap episode(s) (--no-filler)")

    selected = [v for v in selected if v.get("id")]
    if not selected:
        raise SystemExit("No episodes matched the given filters.")

    n = len(selected)
    log(f"🎞  {media_name} - {n} entr{'y' if n == 1 else 'ies'} to play")

    # single episode mode (with subtitles)
    if args.only is not None and n == 1:
        v = selected[0]
        stream = fetch_episode_url(base, token, v["id"])
        if not stream:
            raise SystemExit(f"No stream found for {fmt_ep(v)} (check the Web UI Media tab).")
        cmd_args = []
        sub_args = []
        try:
            sub_data = http_json(f"{base}{prefix}/subtitles/series/{v['id']}.json")
            subs = [s for s in (sub_data.get("subtitles") or []) if s.get("url")]
            if subs:
                def sub_pref(s):
                    lang = (s.get("lang") or "").lower()
                    if args.sub_lang and args.sub_lang.lower() in lang:
                        return 0
                    if lang.startswith("eng") or lang.startswith("en") or lang == "english":
                        return 1
                    return 2
                subs.sort(key=sub_pref)
                chosen = subs[0]
                sub_args = [f"--sub-file={chosen['url']}"]
                log(f"   subtitles: {chosen['lang']}")
        except SystemExit:
            pass
        if args.mpv_args:
            cmd_args += args.mpv_args.split()
        cmd_args += sub_args + [stream["url"]]
        if args.no_play:
            log(f"🎬 Would play:\n   {stream['url']}  {' '.join(sub_args)}")
            return
        mpv = find_mpv(args.mpv)
        launch_mpv(mpv, cmd_args)
        return

    # season playlist mode
    log(f"⬇️  Resolving stream URLs ({n} requests) …")
    results = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(fetch_episode_url, base, token, v["id"]): v for v in selected}
        done = 0
        for fut in as_completed(futs):
            v = futs[fut]
            try:
                results[v["id"]] = fut.result()
            except SystemExit:
                results[v["id"]] = None
            done += 1
            if done % 10 == 0 or done == n:
                log(f"   {done}/{n} ({time.time() - t0:.0f}s)")

    missing = [v for v in selected if not results.get(v["id"])]
    if missing:
        for v in missing:
            log(f"   ⚠️  no stream for {fmt_ep(v)} - skipped (check the Web UI Media tab)")

    if args.min_quality:
        def ok(s):
            name = (s.get("name") or "").lower()
            for low, num in (("360p", 360), ("480p", 480), ("720p", 720), ("1080p", 1080),
                             ("2160p", 2160), ("4k", 2160)):
                if low in name and num >= args.min_quality:
                    return True
            return False
        for vid in list(results):
            st = results[vid]
            results[vid] = st if (st and ok(st)) else (None if st else st)

    entries = []
    for v in selected:
        st = results.get(v["id"])
        if not st:
            continue
        disp = f"{media_name} {fmt_ep(v)} - {label(v)}"
        entries.append((disp, st["url"]))

    if not entries:
        raise SystemExit("Could not build a playlist - no streams were resolved.")

    out = args.out or os.path.join(
        os.getcwd(), f"{safe_filename(media_name)} S{season_label.zfill(2) if season_label.isdigit() else season_label}.m3u"
    )
    write_m3u(out, media_name, entries)
    log(f"✅ Playlist written: {out}  ({len(entries)} entries)")

    if args.no_play:
        log("   (--no-play set, not launching mpv)")
        return

    mpv = find_mpv(args.mpv)
    extra = args.mpv_args.split() if args.mpv_args else []
    launch_mpv(mpv, extra + [out])
    log(f"   Season plays in order; close the mpv window when done. Playlist kept at: {out}")


if __name__ == "__main__":
    main()
