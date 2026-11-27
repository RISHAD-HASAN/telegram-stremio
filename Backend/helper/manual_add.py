import re
from typing import Optional, Tuple, Union

from Backend.helper.metadata import caption_with_id, parse_media_name
from Backend.helper.pyro import clean_filename, finalize_media_name, get_readable_file_size, is_media, message_has_thumb
from Backend.helper.split_files import parse_split_info, strip_part_suffix
from Backend.logger import LOGGER
from Backend.helper.task_manager import edit_message

_PRIVATE_LINK = re.compile(r"t\.me/c/(\d+)(?:/\d+)*/(\d+)")
_PUBLIC_LINK = re.compile(r"t\.me/([A-Za-z][\w]{3,})(?:/\d+)*/(\d+)")
_TG_DEEP_PRIVATE = re.compile(r"tg://privatepost\?channel=(\d+)&post=(\d+)")
_TG_DEEP_PUBLIC = re.compile(r"tg://resolve\?domain=([A-Za-z][\w]{3,})&post=(\d+)")


# Parse a Telegram post link into (chat_ref, msg_id).
# chat_ref is a full -100 int for private channels or a @username string for public ones.
def parse_telegram_link(url: str) -> Tuple[Optional[object], Optional[int]]:
    url = (url or "").strip()
    if not url:
        return None, None
    # Strip URL fragments or query params like ?single or #hash
    clean_url = re.split(r'[\?\#]', url)[0].rstrip('/')
    
    private = _PRIVATE_LINK.search(clean_url) or _PRIVATE_LINK.search(url)
    if private:
        return int(f"-100{private.group(1)}"), int(private.group(2))
    
    public = _PUBLIC_LINK.search(clean_url) or _PUBLIC_LINK.search(url)
    if public:
        return public.group(1), int(public.group(2))
        
    deep_priv = _TG_DEEP_PRIVATE.search(url)
    if deep_priv:
        return int(f"-100{deep_priv.group(1)}"), int(deep_priv.group(2))
        
    deep_pub = _TG_DEEP_PUBLIC.search(url)
    if deep_pub:
        return deep_pub.group(1), int(deep_pub.group(2))

    return None, None


# Extract the final message ID from an int, numeric string, or Telegram message URL
def extract_msg_id(val: Union[int, str, None], default: Optional[int] = None) -> Optional[int]:
    if val is None:
        return default
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if not s:
        return default
    if s.isdigit():
        return int(s)

    # Try standard Telegram link resolution first
    _, msg_id = parse_telegram_link(s)
    if msg_id is not None:
        return msg_id

    # Fallback: strip query/anchor and check trailing slash-delimited segments
    clean = re.split(r'[\?\#]', s)[0].rstrip('/')
    segments = [p for p in clean.split('/') if p.isdigit()]
    if segments:
        return int(segments[-1])

    # Fallback: last contiguous integer in string
    nums = re.findall(r'\d+', s)
    if nums:
        return int(nums[-1])

    return default


# Extract both channel reference and message ID from an input value or URL
def extract_tg_channel_and_msg(val: Union[int, str, None]) -> Tuple[Optional[str | int], Optional[int]]:
    if val is None:
        return None, None
    if isinstance(val, int):
        return None, val
    s = str(val).strip()
    if not s:
        return None, None
    if s.isdigit():
        return None, int(s)

    chat_ref, msg_id = parse_telegram_link(s)
    if chat_ref is not None or msg_id is not None:
        return (str(chat_ref) if chat_ref is not None else None), msg_id

    return None, extract_msg_id(s)


# Parse versatile range specifications (e.g. "100-200", "3363 3472", "https://t.me/c/.../3363 https://t.me/c/.../3472")
def parse_tg_range_spec(text: Union[str, int, None], default_channel: Optional[str | int] = None) -> Tuple[Optional[str | int], Optional[int], Optional[int]]:
    if text is None:
        return default_channel, None, None
    text = str(text).strip()
    if not text:
        return default_channel, None, None
    channel = default_channel

    # Check if there are multiple HTTP/TG URLs in the string (e.g. start link and end link)
    urls = re.split(r'[-–—\s]\s*(?=https?://|t\.me/|tg://)', text)
    if len(urls) >= 2:
        ch1, msg1 = extract_tg_channel_and_msg(urls[0])
        ch2, msg2 = extract_tg_channel_and_msg(urls[1])
        ch = ch1 or ch2 or channel
        if msg1 is not None and msg2 is not None:
            return ch, min(msg1, msg2), max(msg1, msg2)

    # Check if a single URL ends with a range (e.g. .../3363-3472 or .../3363_3472)
    m_url_range = re.search(r't\.me/(?:c/)?(\d+|[A-Za-z][\w]{3,})(?:/\d+)*[/:](?:(\d+)\s*[-_:]\s*(\d+))', text)
    if m_url_range:
        ch_raw = m_url_range.group(1)
        ch = f"-100{ch_raw}" if ch_raw.isdigit() else ch_raw
        s_id, e_id = int(m_url_range.group(2)), int(m_url_range.group(3))
        return ch, min(s_id, e_id), max(s_id, e_id)

    parts = text.split()
    if len(parts) >= 3 and (parts[0].startswith('-100') or (parts[0].startswith('-') and parts[0][1:].isdigit())):
        channel = parts[0]
        s_id = extract_msg_id(parts[1])
        e_id = extract_msg_id(parts[2])
        if s_id is not None and e_id is not None:
            return channel, min(s_id, e_id), max(s_id, e_id)

    if len(parts) == 2:
        if parts[0].startswith('-100') or (parts[0].startswith('-') and parts[0][1:].isdigit()):
            channel = parts[0]
            return parse_tg_range_spec(parts[1], channel)
        ch1, msg1 = extract_tg_channel_and_msg(parts[0])
        ch2, msg2 = extract_tg_channel_and_msg(parts[1])
        ch = ch1 or ch2 or channel
        if msg1 is not None and msg2 is not None:
            return ch, min(msg1, msg2), max(msg1, msg2)

    # Check hyphen/colon/dots between two numbers or expressions
    m_range = re.search(r'^(.*?)\s*(?:[-–—:]|\.\.)\s*(.*?)$', text)
    if m_range and m_range.group(1) != text:
        part1 = m_range.group(1).strip()
        part2 = m_range.group(2).strip()
        ch1, msg1 = extract_tg_channel_and_msg(part1)
        ch2, msg2 = extract_tg_channel_and_msg(part2)
        ch = ch1 or ch2 or channel
        if msg1 is not None and msg2 is not None:
            return ch, min(msg1, msg2), max(msg1, msg2)

    ch, msg = extract_tg_channel_and_msg(text)
    if ch:
        channel = ch
    if msg is not None:
        return channel, msg, msg

    return channel, None, None


# Map a video pixel height to a standard quality label
def quality_from_height(height: int) -> str:
    if not height:
        return ""
    for threshold, label in ((1800, "2160p"), (1200, "1440p"), (900, "1080p"),
                             (620, "720p"), (400, "480p"), (260, "360p")):
        if height >= threshold:
            return label
    return "240p"


# Fetch a message and return the stream fields the manual-add flow needs.
async def resolve_telegram_message(client, url: str = None, chat_id=None, msg_id=None) -> dict:
    if url:
        chat_ref, msg_id = parse_telegram_link(url)
        if chat_ref is None:
            raise ValueError("Could not read that Telegram link. Use a t.me/c/... or t.me/<channel>/... message link.")
    elif chat_id and msg_id:
        # Auto-extract msg_id if a full link was passed into msg_id
        if isinstance(msg_id, str) and ("t.me/" in msg_id or "/" in msg_id):
            link_ch, link_msg = extract_tg_channel_and_msg(msg_id)
            if link_ch:
                chat_id = link_ch
            msg_id = link_msg
        chat_ref = int(f"-100{str(chat_id).replace('-100', '')}")
        msg_id = int(msg_id)
    elif msg_id and ("t.me/" in str(msg_id) or "tg://" in str(msg_id)):
        chat_ref, msg_id = parse_telegram_link(str(msg_id))
        if chat_ref is None:
            raise ValueError("Could not read that Telegram link. Use a t.me/c/... or t.me/<channel>/... message link.")
    else:
        raise ValueError("Provide a Telegram message link, or a chat id and message id.")

    message = await client.get_messages(chat_ref, msg_id)
    if not message or getattr(message, "empty", False):
        raise ValueError("That message was not found. Make sure the bot is in the channel.")

    media = is_media(message)
    if not media:
        raise ValueError("That message has no downloadable file.")

    # Prefer the caption over the raw file name, then normalise it to the exact
    # filename receiver.py stores (clean, split-suffix stripped, video extension).
    caption = (getattr(message, "caption", None) or "").strip()
    raw_name = caption or getattr(media, "file_name", None) or "video"
    cleaned = clean_filename(raw_name)
    split_info = parse_split_info(cleaned)
    raw_size = getattr(media, "file_size", 0) or 0
    parsed = parse_media_name(strip_part_suffix(cleaned) if split_info else cleaned)
    file_name = finalize_media_name(raw_name, bool(split_info))

    # Real video dimensions beat the filename; documents fall back to the name
    height = getattr(media, "height", 0) or 0
    quality = quality_from_height(height) or parsed.get("quality") or ""

    # Original upload date (forward source if forwarded, else this message's date)
    original_date = getattr(message, "forward_date", None) or getattr(message, "date", None)
    upload_year = original_date.year if original_date else 0

    return {
        "chat_id": str(message.chat.id).replace("-100", ""),
        "msg_id": message.id,
        "name": file_name,
        "raw_size": raw_size,
        "size": get_readable_file_size(raw_size),
        "quality": quality,
        "season": parsed.get("season"),
        "episode": parsed.get("episode"),
        "width": getattr(media, "width", 0) or 0,
        "height": height,
        "has_thumb": message_has_thumb(message),
        "upload_year": upload_year,
        "split_key": split_info[0] if split_info else None,
        "part_number": split_info[1] if split_info else None,
    }


async def stamp_caption_with_id(message, metadata_info: dict) -> bool:
    try:
        media = message.video or message.document
        base = (getattr(message, "caption", None) or getattr(media, "file_name", None) or "")
        new_caption = caption_with_id(base, metadata_info)
        if not new_caption:
            return False
        
        await edit_message(message.chat.id, message.id, new_caption)
        return True
    except Exception as e:
        LOGGER.warning(f"[Caption] Could not stamp id on message {getattr(message, 'id', '?')}: {e}")
        return False


async def stamp_caption_by_ref(client, chat_id, msg_id, metadata_info: dict) -> bool:
    try:
        chat_ref = int(f"-100{str(chat_id).replace('-100', '')}")
        message = await client.get_messages(chat_ref, int(msg_id))
        if not message or getattr(message, "empty", False):
            return False
        return await stamp_caption_with_id(message, metadata_info)
    except Exception as e:
        LOGGER.warning(f"[Caption] Could not stamp id on message {msg_id}: {e}")
        return False
