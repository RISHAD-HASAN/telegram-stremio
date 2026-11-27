from os import getenv, path

from dotenv import load_dotenv

load_dotenv(path.join(path.dirname(path.dirname(__file__)), "config.env"))

# Home of this project: default source for the version check and the updater
PROJECT_REPO = "https://github.com/RISHAD-HASAN/telegram-stremio"
PROJECT_BRANCH = "master"


def _int_env(key: str, default: int = 0) -> int:
    try:
        return int((getenv(key) or "").strip())
    except ValueError:
        return default


# Environment-backed configuration
class Telegram:
    # Required: Telegram clients
    API_ID              = _int_env("API_ID")
    API_HASH            = getenv("API_HASH", "")
    BOT_TOKEN           = getenv("BOT_TOKEN", "")

    # Required: Database URIs
    DATABASE = [db.strip() for db in (getenv("DATABASE") or "").split(",") if db.strip()]

    # Required: Server
    PORT     = _int_env("PORT", 8000)
    OWNER_ID = _int_env("OWNER_ID")

    # Read/Write via SettingsManager
    REPLACE_MODE                  = getenv("REPLACE_MODE", "false").lower() == "true"
    DUPLICATE_PROTECTION          = getenv("DUPLICATE_PROTECTION", "true").lower() == "true"
    HIDE_CATALOG                  = getenv("HIDE_CATALOG", "false").lower() == "true"
    AUTH_CHANNEL                  = [c.strip() for c in (getenv("AUTH_CHANNEL") or "").split(",") if c.strip()]
    TMDB_API                      = getenv("TMDB_API", "")
    TVDB_API                       = getenv("TVDB_API", "")
    BASE_URL                      = getenv("BASE_URL", "").rstrip("/")
    UPSTREAM_REPO                 = getenv("UPSTREAM_REPO", "")
    UPSTREAM_BRANCH               = getenv("UPSTREAM_BRANCH", "")
    ADMIN_USERNAME                = getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD                = getenv("ADMIN_PASSWORD", "admin")
    SUBSCRIPTION                  = getenv("SUBSCRIPTION", "false").lower() == "true"
    SUBSCRIPTION_GROUP_ID         = _int_env("SUBSCRIPTION_GROUP_ID")
    APPROVER_IDS                  = [int(x.strip()) for x in (getenv("APPROVER_IDS") or "").split(",") if x.strip().isdigit()]
    HTTP_PROXY_URL                = getenv("HTTP_Proxy_URL", "")
    SHOW_PROXY_AND_NON_PROXY_BOTH = getenv("SHOW_ProxyAndNonProxyBoth", "false").lower() == "true"

    # WebDAV (optional env fallback; prefer Settings page)
    WEBDAV_USER     = getenv("WEBDAV_USER", "")
    WEBDAV_PASSWORD = getenv("WEBDAV_PASSWORD", "")


# Required settings that are missing or unusable, as messages for the person setting up
def config_problems() -> list[str]:
    problems = []
    if not Telegram.API_ID:
        problems.append("API_ID is missing or not a number. Get it from https://my.telegram.org.")
    if not Telegram.API_HASH:
        problems.append("API_HASH is missing. Get it from https://my.telegram.org.")
    if not Telegram.BOT_TOKEN:
        problems.append("BOT_TOKEN is missing. Create a bot with @BotFather and paste its token.")
    if len(Telegram.DATABASE) < 2:
        problems.append(
            "DATABASE needs two MongoDB URIs separated by a comma: the tracking database first, then a storage database."
        )
    return problems


# Why a configured MongoDB URI cannot be used, in words that never include the URI
def _database_failure(exc: Exception) -> str:
    name = type(exc).__name__
    if name in ("InvalidURI", "ConfigurationError"):
        return "the URI is not valid"
    if name == "OperationFailure":
        return "the server rejected the user name or password"
    if name == "ServerSelectionTimeoutError":
        return "no answer from the server"
    return name


# Ping the tracking database and the first storage database before anything else starts
async def database_problems(timeout_ms: int = 8000) -> list[str]:
    from motor.motor_asyncio import AsyncIOMotorClient

    problems = []
    for index, uri in enumerate(Telegram.DATABASE[:2]):
        label = "tracking database (first URI)" if index == 0 else "storage database (second URI)"
        client = None
        try:
            client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=timeout_ms)
            await client.admin.command("ping")
        except Exception as exc:
            problems.append(f"The {label} cannot be reached: {_database_failure(exc)}.")
        finally:
            if client is not None:
                client.close()
    return problems
