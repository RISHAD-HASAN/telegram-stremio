import asyncio
import random
import traceback

import aiohttp
from pyrogram import raw

from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import multi_clients
import Backend.pyrofork.bot as botmod


# Periodically send MTProto keep-alive pings across all client media sessions (prevents cold-start idle disconnects)
async def telegram_mtproto_keepalive():
    sleep_time = 300  # 5 minutes
    await asyncio.sleep(60)  # Wait 1 minute after boot before starting keepalive loop

    while True:
        try:
            clients_to_check = list(multi_clients.values())
            if botmod.Userbot is not None and getattr(botmod.Userbot, "is_connected", False):
                clients_to_check.append(botmod.Userbot)

            for client in clients_to_check:
                if not client or not getattr(client, "is_connected", False):
                    continue
                media_sessions = getattr(client, "media_sessions", {})
                for dc, session in list(media_sessions.items()):
                    if session:
                        try:
                            ping_id = random.randint(1, 2**31 - 1)
                            await asyncio.wait_for(
                                session.send(raw.functions.Ping(ping_id=ping_id)),
                                timeout=5.0
                            )
                        except Exception as e:
                            LOGGER.debug("Keepalive ping for client %s DC %s: %s", getattr(client, "name", "bot"), dc, e)
        except Exception:
            LOGGER.debug("Keepalive loop exception:\n" + traceback.format_exc())

        await asyncio.sleep(sleep_time)


# Periodically self-ping the public status page to keep the instance awake and keep MTProto media sessions warm
async def ping():
    asyncio.create_task(telegram_mtproto_keepalive())

    sleep_time = 1200

    while True:
        await asyncio.sleep(sleep_time)
        try:
            base = (SettingsManager.current().base_url or "").rstrip("/")
            if not base:
                LOGGER.warning("Ping skipped: base_url is not configured")
                continue

            ping_url = f"{base}/status"

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(ping_url, allow_redirects=True) as resp:
                    if 200 <= resp.status < 400:
                        LOGGER.info("Pinged keep-alive URL %s — Status: %s", ping_url, resp.status)
                    else:
                        LOGGER.warning(
                            "Keep-alive ping to %s returned %s (check BASE_URL / reverse proxy)",
                            ping_url,
                            resp.status,
                        )
        except asyncio.TimeoutError:
            LOGGER.warning("Timeout: Could not connect to keep-alive URL.")
        except Exception:
            LOGGER.error("Ping failed:\n" + traceback.format_exc())
