"""Nyaa / Torrent batch caption and tagging assistant."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from Backend.helper.metadata.parse import parse_media_name


def generate_batch_captions(
    title: str,
    tmdb_id: Optional[str | int] = None,
    imdb_id: Optional[str] = None,
    media_type: str = "tv",
    season: int = 1,
    filenames: Optional[List[str] | str] = None,
    start_ep: Optional[int] = None,
    end_ep: Optional[int] = None,
    quality_override: Optional[str] = None,
    format_style: str = "tmdb_tag",
) -> Dict[str, Any]:
    """
    Generate clean, standardized Telegram post captions and tags for a batch of torrent / Nyaa files.
    """
    clean_title = (title or "").strip()
    clean_title = re.sub(r"\[.*?\]|\(.*?\)", "", clean_title).strip()
    tag_id = ""
    if tmdb_id:
        raw_tmdb = str(tmdb_id).replace("tmdb-", "").replace("tmdb:", "").strip()
        if raw_tmdb:
            tag_id = f"{{tmdb-{raw_tmdb}}}"
    elif imdb_id:
        raw_imdb = str(imdb_id).strip()
        if raw_imdb:
            tag_id = f"{{{raw_imdb}}}"

    lines_input: List[str] = []
    if isinstance(filenames, str):
        lines_input = [l.strip() for l in filenames.splitlines() if l.strip()]
    elif isinstance(filenames, list):
        lines_input = [str(l).strip() for l in filenames if str(l).strip()]

    items: List[Dict[str, Any]] = []

    if lines_input:
        for idx, line in enumerate(lines_input, 1):
            parsed = parse_media_name(line)
            parsed_ep = parsed.get("episode")
            ep_num = parsed_ep if (parsed_ep is not None) else idx
            s_num = parsed.get("season") if (parsed.get("season") is not None) else season
            q_val = quality_override or parsed.get("quality") or "1080p"
            ep_title = parsed.get("episode_title") or ""

            # Check if line had an episode title segment e.g. "S01E01-Hang the Witch"
            if not ep_title and "-" in line:
                m = re.search(r'S\d+E\d+[-_ ]+([A-Za-z0-9 ]+)', line, re.IGNORECASE)
                if m:
                    ep_title = m.group(1).strip()

            item_res = _format_single(
                title=clean_title or parsed.get("title") or "Title",
                tag_id=tag_id,
                media_type=media_type,
                season=s_num,
                episode=ep_num,
                quality=q_val,
                episode_title=ep_title,
                original_filename=line,
                format_style=format_style,
            )
            items.append(item_res)
    else:
        # Generate sequentially from start_ep to end_ep
        s_ep = int(start_ep or 1)
        e_ep = int(end_ep or 12)
        if e_ep < s_ep:
            e_ep = s_ep

        for ep_num in range(s_ep, e_ep + 1):
            q_val = quality_override or "1080p"
            item_res = _format_single(
                title=clean_title or "Title",
                tag_id=tag_id,
                media_type=media_type,
                season=season,
                episode=ep_num,
                quality=q_val,
                episode_title="",
                original_filename="",
                format_style=format_style,
            )
            items.append(item_res)

    all_captions_text = "\n\n".join(it["caption"] for it in items)
    all_single_lines = "\n".join(it["single_line"] for it in items)

    return {
        "title": clean_title,
        "tag_id": tag_id,
        "media_type": media_type,
        "season": season,
        "count": len(items),
        "items": items,
        "all_captions_text": all_captions_text,
        "all_single_lines": all_single_lines,
    }


def _format_single(
    title: str,
    tag_id: str,
    media_type: str,
    season: int,
    episode: int,
    quality: str,
    episode_title: str,
    original_filename: str,
    format_style: str,
) -> Dict[str, Any]:
    pad_ep = f"{int(episode):02d}" if int(episode) < 100 else f"{int(episode)}"
    pad_s = f"{int(season):02d}"

    ep_tag = f"S{pad_s}E{pad_ep}" if media_type == "tv" else "Movie"
    ep_name_part = f" - {episode_title}" if episode_title else ""
    tag_prefix = f"{tag_id} " if tag_id else ""

    # 1. Single-line guaranteed indexing caption
    single_line = f"{tag_prefix}{title} {ep_tag}{ep_name_part} [{quality}]".strip()

    # 2. Clean standard filename
    clean_title_dots = re.sub(r"[\s_]+", ".", title)
    clean_filename = f"{clean_title_dots}.{ep_tag}.{quality}.mkv"

    # 3. Rich Markdown Telegram Post
    hashtag = "#" + re.sub(r"[^A-Za-z0-9]", "", title)
    if media_type == "tv":
        markdown_caption = (
            f"🎬 **{title}**\n"
            f"📺 **Season {season} - Episode {pad_ep}**{ep_name_part}\n"
            f"🏷️ `{tag_id or '{tmdb}'}` · `{quality}`\n\n"
            f"{hashtag} #Anime #Stremio"
        ).strip()
    else:
        markdown_caption = (
            f"🎬 **{title}**\n"
            f"🎥 **Movie**\n"
            f"🏷️ `{tag_id or '{tmdb}'}` · `{quality}`\n\n"
            f"{hashtag} #Anime #Stremio #Movie"
        ).strip()

    caption = single_line
    if format_style == "telegram_markdown":
        caption = markdown_caption
    elif format_style == "standard":
        caption = f"{title} - {ep_tag}{ep_name_part} [{quality}]"
    elif format_style == "anime_clean":
        caption = f"{title} - {pad_ep} [{quality}]"

    return {
        "season": season,
        "episode": episode,
        "quality": quality,
        "title": title,
        "episode_title": episode_title,
        "original_filename": original_filename,
        "caption": caption,
        "single_line": single_line,
        "clean_filename": clean_filename,
        "markdown_caption": markdown_caption,
    }
