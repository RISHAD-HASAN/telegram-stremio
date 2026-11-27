from pyrogram.file_id import FileId
from typing import Optional
from Backend.logger import LOGGER
from Backend import __version__, now, timezone
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.exceptions import FileNotFound
from aiofiles import open as aiopen
from aiofiles.os import path as aiopath, remove as aioremove
from pyrogram import Client
from Backend.pyrofork.bot import StreamBot
from pyrogram.types import BotCommand
from pyrogram import enums


# Re-exported from name_cleaning for existing importers.
from Backend.helper.name_cleaning import (  # noqa: E402,F401
    _CHANNEL_TAG_PATTERN,
    _CODEC_WORD_PATTERN,
    _DECORATION_PATTERN,
    _DOMAIN_TAG_PATTERN,
    _EMOJI_PATTERN,
    clean_filename,
    finalize_media_name,
    remove_urls,
)


def is_media(message):
    return next(
        (
            getattr(message, attr)
            for attr in [
                "document", "photo", "video", "audio",
                "voice", "video_note", "sticker", "animation",
            ]
            if getattr(message, attr)
        ),
        None,
    )


def get_video_cover(message):
    media = getattr(message, "video", None) or getattr(message, "document", None)
    if not media:
        return None
    return getattr(media, "cover", None) or getattr(media, "video_cover", None) or getattr(message, "video_cover", None)


def message_has_thumb(message) -> bool:
    media = getattr(message, "video", None) or getattr(message, "document", None)
    if not media:
        return False
    if get_video_cover(message):
        return True
    return bool(getattr(media, "thumbs", None))


def get_thumb_download_target(message):
    cover = get_video_cover(message)
    if cover is not None:
        return cover
    media = getattr(message, "video", None) or getattr(message, "document", None)
    thumbs = getattr(media, "thumbs", None) if media else None
    if thumbs:
        return thumbs[-1]
    return None


_IMGUR_CLIENT_ID = "546c25a59c58ad7"


async def upload_bytes_to_host(data: bytes, filename: str = "thumb.jpg") -> Optional[str]:
    if not data:
        return None
    import httpx
    timeout = httpx.Timeout(25.0, connect=10.0)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as http:
        try:
            resp = await http.post(
                "https://api.imgur.com/3/image",
                headers={**headers, "Authorization": f"Client-ID {_IMGUR_CLIENT_ID}"},
                files={"image": (filename, data, "image/jpeg")},
            )
            if resp.status_code == 200:
                link = (((resp.json() or {}).get("data") or {}).get("link") or "").strip()
                if link.startswith("http"):
                    return link
        except Exception:
            pass
        try:
            resp = await http.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": (filename, data, "image/jpeg")},
            )
            if resp.status_code == 200:
                url = ((((resp.json() or {}).get("data") or {}).get("url")) or "").strip()
                if url.startswith("http"):
                    if "tmpfiles.org/" in url and "/dl/" not in url:
                        url = url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
                    return url
        except Exception:
            pass
    return None


async def upload_message_thumb_to_host(client, message) -> Optional[str]:
    target = get_thumb_download_target(message)
    if not target or not client:
        return None
    try:
        file_id = getattr(target, "file_id", None) or target
        buf = await client.download_media(file_id, in_memory=True)
        data = buf.getvalue() if hasattr(buf, "getvalue") else bytes(buf)
        return await upload_bytes_to_host(data)
    except Exception as e:
        LOGGER.warning(f"[THUMB] host upload failed: {e}")
        return None


async def resolve_video_thumb_url(client, message, encoded: str) -> str:
    if not message_has_thumb(message):
        return ""
    fallback = f"/thumb/{encoded}"
    if client:
        try:
            url = await upload_message_thumb_to_host(client, message)
            if url:
                return url
        except Exception as e:
            LOGGER.warning(f"[THUMB] hybrid resolve failed: {e}")
    return fallback


async def apply_video_thumb_to_metadata(metadata_info: dict, message, encoded: str, client=None) -> None:
    needs = False
    if metadata_info.get("media_type") == "tv":
        needs = not metadata_info.get("episode_backdrop")
    else:
        needs = not metadata_info.get("backdrop")
    if not needs:
        return
    thumb_url = await resolve_video_thumb_url(client, message, encoded)
    if not thumb_url:
        return
    if metadata_info.get("media_type") == "tv":
        metadata_info["episode_backdrop"] = thumb_url
    else:
        metadata_info["backdrop"] = thumb_url


async def get_file_ids(client: Client, chat_id: int, message_id: int) -> Optional[FileId]:
    try:
        message = await client.get_messages(chat_id, message_id)
        if message.empty:
            raise FileNotFound("Message not found or empty")

        if media := is_media(message):
            file_id_obj = FileId.decode(media.file_id)
            file_unique_id = media.file_unique_id

            file_id_obj.file_name = getattr(media, 'file_name', '')
            file_id_obj.file_size = getattr(media, 'file_size', 0)
            file_id_obj.mime_type = getattr(media, 'mime_type', '')
            file_id_obj.unique_id = file_unique_id

            return file_id_obj
        else:
            raise FileNotFound("No supported media found in message")
    except Exception as e:
        LOGGER.error(f"Error getting file IDs: {e}")
        raise


def get_readable_file_size(size_in_bytes):
    size_in_bytes = int(size_in_bytes) if str(size_in_bytes).isdigit() else 0
    if not size_in_bytes:
        return '0B'

    index, SIZE_UNITS = 0, ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    while size_in_bytes >= 1024 and index < len(SIZE_UNITS) - 1:
        size_in_bytes /= 1024
        index += 1

    return f'{size_in_bytes:.2f}{SIZE_UNITS[index]}' if index > 0 else f'{size_in_bytes:.0f}B'


def get_readable_time(seconds: int) -> str:
    count = 0
    readable_time = ""
    time_list = []
    time_suffix_list = ["s", "m", "h", " days"]

    while count < 4:
        count += 1
        if count < 3:
            remainder, result = divmod(seconds, 60)
        else:
            remainder, result = divmod(seconds, 24)

        if seconds == 0 and remainder == 0:
            break

        time_list.append(int(result))
        seconds = int(remainder)

    for x in range(len(time_list)):
        time_list[x] = str(time_list[x]) + time_suffix_list[x]

    if len(time_list) == 4:
        readable_time += time_list.pop() + ", "

    time_list.reverse()
    readable_time += ": ".join(time_list)

    return readable_time


async def restart_notification():
    chat_id, msg_id = 0, 0
    try:
        if await aiopath.exists(".restartmsg"):
            async with aiopen(".restartmsg", "r") as f:
                data = await f.readlines()
                chat_id, msg_id = map(int, data)

            try:
                repo = SettingsManager.current().upstream_repo.split('/')
                UPSTREAM_REPO = f"https://github.com/{repo[-2]}/{repo[-1]}"
                await StreamBot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=(
                        f"... ♻️ Restart Successfully...! \n\n"
                        f"Date: {now.strftime('%d/%m/%y')}\n"
                        f"Time: {now.strftime('%I:%M:%S %p')}\n"
                        f"TimeZone: {timezone.zone}\n\n"
                        f"Repo: {UPSTREAM_REPO}\n"
                        f"Branch: {SettingsManager.current().upstream_branch}\n"
                        f"Version: {__version__}"
                    ),
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception as e:
                LOGGER.error(f"Failed to edit restart message: {e}")

            await aioremove(".restartmsg")

    except Exception as e:
        LOGGER.error(f"Error in restart_notification: {e}")


VIDEO_EXTENSIONS = (
    '.mkv', '.mp4', '.avi', '.ts', '.m4v', '.mov', '.wmv', '.webm',
    '.flv', '.m2ts', '.mpg', '.mpeg', '.3gp', '.ogv', '.divx', '.vob',
)

def has_video_extension(filename: str) -> bool:
    if not filename:
        return False
    fn_lower = filename.lower()
    return any(fn_lower.endswith(ext) for ext in VIDEO_EXTENSIONS)


# Bot commands (stats, log and restart moved to the web app)
commands = [
    BotCommand("start", "Get your addon link"),
    BotCommand("scan_files", "Index a message range, e.g. /scan_files 100-200"),
    BotCommand("resolve_nyaa", "Index batch-release files using a nyaa.si link"),
    BotCommand("tidy", "Remove dead streams and empty entries"),
    BotCommand("missing", "List missing episodes; find or fill searches your Telegram"),
    BotCommand("sync_all", "Sync AniList metadata and the Jellyfin library"),
]


async def setup_bot_commands(bot: Client):
    try:
        current_commands = await bot.get_bot_commands()
        if current_commands:
            LOGGER.info(f"Found {len(current_commands)} existing commands. Deleting them...")
            await bot.set_bot_commands([])

        await bot.set_bot_commands(commands)
        LOGGER.info("Bot commands updated successfully.")
    except Exception as e:
        LOGGER.error(f"Error setting up bot commands: {e}")
