import io
import os
import re
import zipfile
from typing import Dict, List, Optional, Tuple

from Backend import db
from Backend.helper.encrypt import encode_string
from Backend.helper.nfo_generator import episode_nfo, movie_nfo, season_nfo, tvshow_nfo
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


def _sanitize_path_name(name: str) -> str:
    """Sanitize string for Windows and Unix filesystem folder/file names."""
    if not name:
        return "Unknown"
    # Replace illegal filesystem characters: \ / : * ? " < > |
    s = re.sub(r'[\\/*?:"<>|]', " - ", str(name))
    # Remove leading/trailing dots, spaces, and dashes
    s = re.sub(r"\s+", " ", s).strip(". -")
    return s or "Unknown"


async def _resolve_api_token(token: Optional[str] = None) -> str:
    """Ensure a valid API token is used for stream URLs."""
    if token and str(token).strip():
        return str(token).strip()
    try:
        tokens = await db.get_all_api_tokens()
        if tokens:
            return tokens[0].get("token") or "default"
    except Exception:
        pass
    return "default"


async def generate_jellyfin_tree(token: Optional[str] = None, include_nfos: bool = True) -> List[Tuple[str, str]]:
    """
    Generate all (relative_filepath, file_content) pairs for Jellyfin.
    Produces:
      - Movies/<Movie (Year)>/movie.nfo
      - Movies/<Movie (Year)>/<Movie (Year)> - Part 1 [<quality>].strm
      - TV-Shows/<Show (Year)>/tvshow.nfo
      - TV-Shows/<Show (Year)>/Season 01/season.nfo
      - TV-Shows/<Show (Year)>/Season 01/<Show - S01E01 - Title> [<quality>].strm
      - TV-Shows/<Show (Year)>/Season 01/<Show - S01E01 - Title>.nfo
    """
    settings = SettingsManager.current()
    base_url = (settings.base_url or "http://127.0.0.1:8000").rstrip("/")
    active_token = await _resolve_api_token(token)
    items: List[Tuple[str, str]] = []

    storage_keys = sorted(
        [k for k in getattr(db, "dbs", {}).keys() if k.startswith("storage_")],
        key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0,
    )

    for db_key in storage_keys:
        storage = db.dbs[db_key]

        # 1. Process Movies (check "movie" and fallback "movies")
        movie_collections = [col for col in ["movie", "movies"] if col in storage] if hasattr(storage, "__contains__") else ["movie"]
        for col_name in movie_collections:
            try:
                cursor = storage[col_name].find({})
                async for m in cursor:
                    title = m.get("title_english") or m.get("title") or "Unknown Movie"
                    year = m.get("release_year")
                    year_str = f" ({year})" if year else ""
                    folder_name = _sanitize_path_name(f"{title}{year_str}")
                    movie_dir = f"Movies/{folder_name}"

                    # Movie NFO
                    if include_nfos:
                        try:
                            nfo_text = movie_nfo(m)
                            items.append((f"{movie_dir}/movie.nfo", nfo_text))
                        except Exception as e:
                            LOGGER.debug(f"[JellyfinSync] Error creating movie nfo for {title}: {e}")

                    # Qualities / streams
                    qualities = m.get("telegram") or m.get("qualities") or []
                    for q in qualities:
                        quality_label = q.get("quality") or "1080p"
                        parts = q.get("parts") or []
                        stream_id = q.get("id")

                        # If multi-part split file -> export individual part streams for Plezy / Jellyfin versioning
                        if parts and len(parts) > 1:
                            for idx, p in enumerate(sorted(parts, key=lambda x: x.get("part_number", 0)), start=1):
                                part_num = p.get("part_number") or idx
                                part_enc = await encode_string({"chat_id": int(p["chat_id"]), "msg_id": int(p["msg_id"])})
                                stream_url = f"{base_url}/dl/{active_token}/{part_enc}/video.mkv"
                                strm_name = _sanitize_path_name(f"{title}{year_str} - Part {part_num} [{quality_label}]")
                                items.append((f"{movie_dir}/{strm_name}.strm", stream_url))
                        else:
                            # Single stream
                            if not stream_id:
                                if parts and len(parts) == 1:
                                    stream_id = await encode_string({"chat_id": int(parts[0]["chat_id"]), "msg_id": int(parts[0]["msg_id"])})
                                elif q.get("chat_id") and q.get("msg_id"):
                                    stream_id = await encode_string({"chat_id": int(q["chat_id"]), "msg_id": int(q["msg_id"])})

                            if stream_id:
                                stream_url = f"{base_url}/dl/{active_token}/{stream_id}/video.mkv"
                                strm_name = _sanitize_path_name(f"{title}{year_str} [{quality_label}]")
                                items.append((f"{movie_dir}/{strm_name}.strm", stream_url))
            except Exception as e:
                LOGGER.error(f"[JellyfinSync] Error processing movies in {db_key}: {e}")

        # 2. Process TV Shows / Series (check "tv" and fallback "series")
        tv_collections = [col for col in ["tv", "series"] if col in storage] if hasattr(storage, "__contains__") else ["tv"]
        for col_name in tv_collections:
            try:
                cursor = storage[col_name].find({})
                async for s in cursor:
                    title = s.get("title_english") or s.get("title") or "Unknown Series"
                    year = s.get("release_year")
                    year_str = f" ({year})" if year else ""
                    show_folder = _sanitize_path_name(f"{title}{year_str}")
                    show_dir = f"TV-Shows/{show_folder}"

                    # Series NFO
                    if include_nfos:
                        try:
                            s_nfo = tvshow_nfo(s)
                            items.append((f"{show_dir}/tvshow.nfo", s_nfo))
                        except Exception as e:
                            LOGGER.debug(f"[JellyfinSync] Error creating series nfo for {title}: {e}")

                    seasons = s.get("seasons") or []
                    for season_doc in seasons:
                        season_num = int(season_doc.get("season_number") if season_doc.get("season_number") is not None else 1)
                        season_dir = f"{show_dir}/Season {season_num:02d}"

                        if include_nfos:
                            try:
                                seas_nfo = season_nfo(s, season_num)
                                items.append((f"{season_dir}/season.nfo", seas_nfo))
                            except Exception as e:
                                LOGGER.debug(f"[JellyfinSync] Error creating season nfo: {e}")

                        episodes = season_doc.get("episodes") or []
                        # Sort episodes with floating-point support for decimal specials like 11.5
                        episodes.sort(key=lambda ep_d: float(ep_d.get("episode_number") or 0.0))

                        for ep in episodes:
                            ep_num = ep.get("episode_number", 1)
                            try:
                                f_ep = float(ep_num)
                                ep_tag = f"{int(f_ep):02d}.{str(f_ep).split('.')[1]}" if f_ep % 1 != 0 else f"{int(f_ep):02d}"
                            except Exception:
                                ep_tag = f"{ep_num}"

                            ep_title = ep.get("title") or f"Episode {ep_tag}"
                            clean_ep_title = _sanitize_path_name(ep_title)
                            ep_base_name = f"{title} - S{season_num:02d}E{ep_tag} - {clean_ep_title}"

                            if include_nfos:
                                try:
                                    ep_nfo_text = episode_nfo(s, season_num, ep)
                                    items.append((f"{season_dir}/{ep_base_name}.nfo", ep_nfo_text))
                                except Exception as e:
                                    LOGGER.debug(f"[JellyfinSync] Error creating episode nfo: {e}")

                            qualities = ep.get("telegram") or ep.get("qualities") or []
                            for q in qualities:
                                quality_label = q.get("quality") or "1080p"
                                parts = q.get("parts") or []
                                stream_id = q.get("id")

                                # If multi-part split file -> export individual part streams
                                if parts and len(parts) > 1:
                                    for idx, p in enumerate(sorted(parts, key=lambda x: x.get("part_number", 0)), start=1):
                                        part_num = p.get("part_number") or idx
                                        part_enc = await encode_string({"chat_id": int(p["chat_id"]), "msg_id": int(p["msg_id"])})
                                        stream_url = f"{base_url}/dl/{active_token}/{part_enc}/video.mkv"
                                        strm_file = f"{season_dir}/{ep_base_name} - Part {part_num} [{quality_label}].strm"
                                        items.append((strm_file, stream_url))
                                else:
                                    if not stream_id:
                                        if parts and len(parts) == 1:
                                            stream_id = await encode_string({"chat_id": int(parts[0]["chat_id"]), "msg_id": int(parts[0]["msg_id"])})
                                        elif q.get("chat_id") and q.get("msg_id"):
                                            stream_id = await encode_string({"chat_id": int(q["chat_id"]), "msg_id": int(q["msg_id"])})

                                    if stream_id:
                                        stream_url = f"{base_url}/dl/{active_token}/{stream_id}/video.mkv"
                                        strm_file = f"{season_dir}/{ep_base_name} [{quality_label}].strm"
                                        items.append((strm_file, stream_url))
            except Exception as e:
                LOGGER.error(f"[JellyfinSync] Error processing TV shows in {db_key}: {e}")

    return items


async def sync_jellyfin_to_disk(target_dir: str, token: Optional[str] = None, include_nfos: bool = True) -> Dict[str, int]:
    """Sync the Jellyfin library files directly to a directory on disk."""
    items = await generate_jellyfin_tree(token, include_nfos)
    target_dir = os.path.abspath(target_dir)
    os.makedirs(target_dir, exist_ok=True)

    synced_files = 0
    synced_strm = 0
    synced_nfo = 0

    for rel_path, content in items:
        full_path = os.path.join(target_dir, rel_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        should_write = True
        if os.path.exists(full_path):
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    if f.read() == content:
                        should_write = False
            except Exception:
                should_write = True

        if should_write:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)

        synced_files += 1
        if rel_path.endswith(".strm"):
            synced_strm += 1
        elif rel_path.endswith(".nfo"):
            synced_nfo += 1

    LOGGER.info(f"[JellyfinSync] Synced {synced_files} files ({synced_strm} strm, {synced_nfo} nfo) to {target_dir}")
    return {
        "total_files": synced_files,
        "strm_files": synced_strm,
        "nfo_files": synced_nfo,
        "target_dir": target_dir,
    }


async def build_jellyfin_zip_buffer(token: Optional[str] = None, include_nfos: bool = True) -> io.BytesIO:
    """Build an in-memory zip archive containing the full Jellyfin library."""
    items = await generate_jellyfin_tree(token, include_nfos)
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel_path, content in items:
            zf.writestr(rel_path, content)

    buf.seek(0)
    return buf
