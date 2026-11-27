"""
Background sync for AniList metadata and Jellyfin/Emby/Kodi libraries.

A sync is scheduled (with debouncing) after any of these events:
  - the channel scanner finishes a scan or a batch
  - a live file is received and indexed
  - the Nyaa batch resolver finishes a batch
  - a manual upload session finishes
  - the user clicks 'Sync Server (AniList + Jellyfin)' in the web UI
    or sends /sync_all to the bot
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from Backend import db
from Backend.helper.jellyfin_sync import sync_jellyfin_to_disk
from Backend.helper.metadata.providers.anilist import search_anilist
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

_sync_task: Optional[asyncio.Task] = None
_sync_lock = asyncio.Lock()
_pending_sync_deadline: float = 0.0
_is_syncing: bool = False

_LAST_SYNC_STATS: Dict[str, Any] = {
    "status": "idle",
    "last_sync_time": 0.0,
    "jellyfin": {},
    "anilist": {},
}


async def sync_anilist_metadata(limit: int = 200) -> Dict[str, Any]:
    """
    Enrich all anime entries in MongoDB with AniList metadata (AniList ID,
    MAL ID, Romaji/Native titles, studio, score, status, genres).
    """
    settings = SettingsManager.current()
    if not settings.anilist_auto_sync:
        return {"status": "skipped", "reason": "anilist_auto_sync is disabled"}

    updated_count = 0
    scanned_count = 0

    storage_keys = sorted(
        [k for k in getattr(db, "dbs", {}).keys() if k.startswith("storage_")],
        key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0,
    )

    for db_key in storage_keys:
        storage = db.dbs[db_key]

        # 1. TV Series / Anime
        tv_collections = [col for col in ["tv", "series"] if col in storage] if hasattr(storage, "__contains__") else ["tv"]
        for col_name in tv_collections:
            try:
                cursor = storage[col_name].find({})
                async for doc in cursor:
                    scanned_count += 1
                    # Skip if already has anilist_id and status
                    if doc.get("anilist_id") and doc.get("status"):
                        continue

                    title = doc.get("title_english") or doc.get("title") or ""
                    year = doc.get("release_year")
                    is_anime = bool(doc.get("is_anime") or doc.get("kitsu_id"))

                    if not is_anime and not any(g in ["Animation", "Anime"] for g in doc.get("genres", [])):
                        # Quick check if it's anime
                        pass

                    hits = await search_anilist(title, year)
                    if hits:
                        hit = hits[0]
                        update_fields: Dict[str, Any] = {
                            "is_anime": True,
                            "anilist_id": hit.get("anilist_id"),
                            "mal_id": hit.get("mal_id"),
                            "romaji_title": hit.get("romaji_title"),
                            "native_title": hit.get("native_title"),
                            "studio": hit.get("studio"),
                            "status": hit.get("status"),
                            "anilist_score": hit.get("anilist_score"),
                            "site_url": hit.get("site_url"),
                        }
                        if not doc.get("poster") and hit.get("poster"):
                            update_fields["poster"] = hit.get("poster")
                        if not doc.get("backdrop") and hit.get("backdrop"):
                            update_fields["backdrop"] = hit.get("backdrop")

                        await storage[col_name].update_one({"_id": doc["_id"]}, {"$set": update_fields})
                        updated_count += 1
            except Exception as e:
                LOGGER.error(f"[AutoSync:AniList] Error scanning {col_name} in {db_key}: {e}")

        # 2. Anime Movies
        movie_collections = [col for col in ["movie", "movies"] if col in storage] if hasattr(storage, "__contains__") else ["movie"]
        for col_name in movie_collections:
            try:
                cursor = storage[col_name].find({"$or": [{"is_anime": True}, {"kitsu_id": {"$exists": True}}]})
                async for doc in cursor:
                    scanned_count += 1
                    if doc.get("anilist_id") and doc.get("status"):
                        continue

                    title = doc.get("title_english") or doc.get("title") or ""
                    year = doc.get("release_year")

                    hits = await search_anilist(title, year)
                    if hits:
                        hit = hits[0]
                        update_fields = {
                            "is_anime": True,
                            "anilist_id": hit.get("anilist_id"),
                            "mal_id": hit.get("mal_id"),
                            "romaji_title": hit.get("romaji_title"),
                            "native_title": hit.get("native_title"),
                            "studio": hit.get("studio"),
                            "status": hit.get("status"),
                            "anilist_score": hit.get("anilist_score"),
                            "site_url": hit.get("site_url"),
                        }
                        await storage[col_name].update_one({"_id": doc["_id"]}, {"$set": update_fields})
                        updated_count += 1
            except Exception as e:
                LOGGER.error(f"[AutoSync:AniList] Error scanning movies in {db_key}: {e}")

    LOGGER.info(f"[AutoSync:AniList] Processed {scanned_count} titles, updated {updated_count} with AniList metadata.")
    return {
        "status": "success",
        "scanned_titles": scanned_count,
        "updated_titles": updated_count,
    }


async def sync_all_server(
    target_dir: Optional[str] = None,
    token: Optional[str] = None,
    sync_anilist: bool = True,
    sync_jellyfin: bool = True,
) -> Dict[str, Any]:
    """
    Execute a full synchronization cycle:
      1. AniList Metadata & Status Sync
      2. Jellyfin/Emby/Kodi Virtual STRM Library & NFO Sync
    """
    global _is_syncing
    settings = SettingsManager.current()

    async with _sync_lock:
        _is_syncing = True
        _LAST_SYNC_STATS["status"] = "running"
        t0 = time.time()
        results: Dict[str, Any] = {"status": "success"}

        # 1. AniList Sync
        if sync_anilist and settings.anilist_auto_sync:
            try:
                al_res = await sync_anilist_metadata()
                results["anilist"] = al_res
            except Exception as e:
                LOGGER.error(f"[AutoSync] AniList sync failed: {e}")
                results["anilist"] = {"status": "error", "error": str(e)}

        # 2. Jellyfin Sync
        if sync_jellyfin and settings.jellyfin_auto_sync:
            j_dir = target_dir or settings.jellyfin_sync_dir or "./jellyfin_library"
            try:
                jf_res = await sync_jellyfin_to_disk(target_dir=j_dir, token=token, include_nfos=True)
                results["jellyfin"] = jf_res
            except Exception as e:
                LOGGER.error(f"[AutoSync] Jellyfin disk sync failed: {e}")
                results["jellyfin"] = {"status": "error", "error": str(e)}

        elapsed = round(time.time() - t0, 2)
        results["elapsed_seconds"] = elapsed
        _LAST_SYNC_STATS.update({
            "status": "completed",
            "last_sync_time": time.time(),
            "results": results,
        })
        _is_syncing = False
        LOGGER.info(f"[AutoSync] Completed full server sync in {elapsed}s.")
        return results


async def _debounced_worker():
    global _pending_sync_deadline
    while True:
        now = time.time()
        if _pending_sync_deadline > 0:
            if now >= _pending_sync_deadline:
                _pending_sync_deadline = 0.0
                try:
                    await sync_all_server()
                except Exception as e:
                    LOGGER.error(f"[AutoSync:Debounce] Background sync error: {e}")
            else:
                await asyncio.sleep(min(1.0, _pending_sync_deadline - now))
        else:
            await asyncio.sleep(1.0)


def schedule_auto_sync(delay_seconds: float = 3.0) -> None:
    """
    Schedule an automated background server sync with debouncing.
    Repeated calls within `delay_seconds` will coalesce into a single sync.
    """
    global _pending_sync_deadline, _sync_task
    settings = SettingsManager.current()
    if not settings.jellyfin_auto_sync and not settings.anilist_auto_sync:
        return

    _pending_sync_deadline = time.time() + max(1.0, float(delay_seconds))

    try:
        loop = asyncio.get_running_loop()
        if loop and loop.is_running():
            if _sync_task is None or _sync_task.done():
                _sync_task = loop.create_task(_debounced_worker())
    except (RuntimeError, Exception):
        pass


def get_auto_sync_status() -> Dict[str, Any]:
    """Get the current automated sync status and statistics."""
    return {
        "is_syncing": _is_syncing,
        "pending": _pending_sync_deadline > time.time(),
        "stats": _LAST_SYNC_STATS,
    }
