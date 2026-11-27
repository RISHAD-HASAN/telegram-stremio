# Configuration

There are two layers of configuration.

- **`config.env`** (or environment variables) holds what the server needs to
  start: Telegram credentials and the database. It is read at every start.
- **Runtime settings** are everything else. They live in MongoDB and are
  edited on the Settings page of the web panel.

A third group of `config.env` keys sits between the two: first-start values.
They are copied into the database the first time the server starts with an
empty database. After that, editing them in `config.env` has no effect; use
the Settings page.

Environment variables take precedence over `config.env`.

## Required

| Key | Description |
| --- | --- |
| `API_ID` | Numeric API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | API hash from the same page |
| `BOT_TOKEN` | Token of the main bot, from [@BotFather](https://t.me/BotFather) |
| `DATABASE` | MongoDB URIs separated by commas. The first is the tracking database, the second a storage database. Further URIs are added as extra storage databases on first start. |

The server checks these before it starts anything. A missing value is named
in the log and the process exits with code 1. It then checks that the first
two databases answer, and stops with `MongoDB is not available` when they do
not.

`DATABASE` examples:

```ini
# one local MongoDB server
DATABASE="mongodb://localhost:27017/tracking,mongodb://localhost:27017/storage1"

# MongoDB Atlas, two clusters
DATABASE="mongodb+srv://USER:PASSWORD@first.example.mongodb.net/,mongodb+srv://USER:PASSWORD@second.example.mongodb.net/"
```

The server always works in a database named `dbFyvio` on each URI; the path
part of the URI does not choose the database. Tracking and storage use
different collections, so both URIs may point at the same MongoDB server.
Separate servers or clusters are still worth having for a large library,
because each free Atlas cluster has its own size limit.

## Read at every start

| Key | Default | Description |
| --- | --- | --- |
| `OWNER_ID` | none | Your numeric Telegram user ID. The maintenance commands of the bot answer only this user. Without it the server starts with a warning and nobody can use those commands. |
| `PORT` | `8000` | Port of the web server |
| `TIMEZONE` | `Asia/Kolkata` | IANA time zone name for log lines and bot messages, for example `Europe/Berlin`. An unknown name is reported in the log and the default is used. |

## First-start values

| Key | Default | Setting it seeds |
| --- | --- | --- |
| `BASE_URL` | empty | Base URL: the address written into addon and stream links |
| `ADMIN_USERNAME` | `admin` | Admin Username |
| `ADMIN_PASSWORD` | `admin` | Admin Password (stored hashed) |
| `AUTH_CHANNEL` | empty | AUTH_CHANNELS: comma-separated channel IDs the bot indexes |
| `TMDB_API` | empty | TMDB API Key |
| `TVDB_API` | empty | TVDB API Key |
| `REPLACE_MODE` | `false` | Replace Mode |
| `DUPLICATE_PROTECTION` | `true` | Duplicate Protection |
| `HIDE_CATALOG` | `false` | Hide Catalog |
| `UPSTREAM_REPO` | empty | Upstream Repo: update source |
| `UPSTREAM_BRANCH` | empty | Upstream Branch |
| `SUBSCRIPTION` | `false` | Enable Subscription System |
| `SUBSCRIPTION_GROUP_ID` | `0` | Subscription Group ID |
| `APPROVER_IDS` | empty | User IDs allowed to approve subscriptions |
| `HTTP_Proxy_URL` | empty | HTTP Proxy URL |
| `SHOW_ProxyAndNonProxyBoth` | `false` | Show Proxy & Non-Proxy Streams |
| `WEBDAV_USER`, `WEBDAV_PASSWORD` | empty | WebDAV login; used as a fallback when none is set in Settings |

`UPSTREAM_REPO` and `UPSTREAM_BRANCH` are also read directly by `update.py`
when the database has no value for them.

Older templates listed `USER_SESSION_STRING`. The server does not read it. A
Telegram user session is set up under **Settings → Telegram User Session**.

## The Settings page

The page is grouped into cards. The most important ones:

**General.** *Replace Mode*: when a new file arrives for a title and
resolution that is already indexed, the newer file replaces the older one only
if it is better (or equal and smaller), and the old post is deleted from the
channel, which needs delete rights for the bot. A file that is not better is
not indexed. Files in a different resolution are kept side by side. With
Replace Mode off, all files are kept and *Duplicate Protection* skips exact
duplicates. *Hide Catalog* removes the automatic catalogs from the addon.

**Admin Authentication.** Username, password and session secret of the web
panel. The password is stored as a PBKDF2 hash.

**Channels.** *AUTH_CHANNELS* are the channels the bot indexes. Tick *Anime*
on a channel to resolve its files through Kitsu first, then TVDB, TMDb and
Cinemeta. The other channel roles (manual, skip, announcement, global search)
are described in [usage.md](usage.md#channel-roles).

**Media & Content.** TMDb and TVDB keys, Base URL, and the update source
(Upstream Repo and Branch).

**Automated Sync.** AniList metadata sync and the `.strm` / `.nfo` export for
Jellyfin, Emby and Kodi, with the destination directory (default
`./jellyfin_library`).

**Poster Provider.** Optional poster services: BetterPoster, RPDB,
SpatialPosters, Fanart.tv.

**Stream Formatter.** How stream entries are named in the player, from a
preset or your own name and description templates, with a live preview.

**Subscription.** The subscription system, the group users must join, payment
instructions and the approvers.

**Telegram User Session.** Signs in a user account by phone number and login
code. A user session is needed for Global Search and for `/missing find` and
`/missing fill`, and can be chosen as the default streaming client.

**Proxy Settings**, **Cloudflare Streaming**, **WebDAV**, **Databases**
(extra storage databases), **Multi-Token Clients** (more bots that share the
streaming load), **Helper Bots** (for users' own bots) and **Backup &
Restore** (settings, catalogs, plans and tokens as a file).

The first card, **System & Maintenance**, shows database statistics and the
log, and has the **Restart App** button.

## Changing the admin password

Use **Settings → Admin Authentication**. The server logs a warning at every
start while the password is still `admin`, and the setup checklist on the
dashboard shows the same.

If you are locked out, set a temporary password directly in MongoDB and change
it on the Settings page afterwards:

```javascript
// mongosh "<your tracking database URI>"
use dbFyvio
db.settings.updateOne(
  { _id: "app_settings" },
  { $set: { admin_username: "admin", admin_password: "temporary-password" } }
)
```

Restart the server after the change. A plain value like this is accepted for
login. Then set a new password under **Settings → Admin Authentication**;
that one is stored hashed.

Do not paste your database URI into third-party password reset websites.

## External services

The server contacts these services. None of them receives your Telegram
credentials, bot tokens or database URI.

| Service | Used for | When |
| --- | --- | --- |
| Telegram | Bot, file transfer | Always |
| MongoDB | Storage | Always |
| Cinemeta (`v3-cinemeta.strem.io`) | Movie and series metadata | Always |
| Kitsu, ani.zip | Anime metadata and episode maps | Anime titles |
| TMDb, TVDB | Metadata | When an API key is set |
| AniList, Jikan (MyAnimeList) | Anime ratings, relations, filler flags | AniList sync, watch order, the offline indexer |
| GitHub | Version check; the contributors list on the Settings page | Dashboard, Settings page |
| nyaa.si | Release names for the Nyaa resolver | `/resolve_nyaa` and the Nyaa tools |
| RPDB, Fanart.tv, BetterPoster, SpatialPosters | Poster artwork | When enabled |
| ip-api.com | Country, city and provider of a viewer's IP address for the access statistics. The request goes over plain HTTP and contains the viewer's IP address. | When a stream starts; results are cached per address |
| Imgur, tmpfiles.org | Hosting of video thumbnails: for a file added through a Manual Upload Session that carries a Telegram thumbnail, the thumbnail image is uploaded there and used as episode artwork. If both uploads fail, the server serves the thumbnail itself from `/thumb/...`. | Manual Upload Session |
| wsrv.nl | Resized Fanart.tv images | Fanart.tv with "Faster artwork" |
| gradient-cover-api.vercel.app | Generated cover images | Titles without a poster |
| jsDelivr, raw.githubusercontent.com | Artwork for the Nuvio collections and a placeholder episode image, served from the upstream repository and two other public repositories | Nuvio collections |
| bit.ly, i.postimg.cc | Short notice videos shown when a token has hit its limit or expired, and the addon logo; both are links set by the upstream project | In the player |
| Tailwind CDN, cdnjs (Font Awesome), Google Fonts | Styles, icons and fonts of the web panel | Web panel, in the browser |

Some of this depends on resources operated by the upstream project, not by
you and not by this fork:

- **Cloudflare Streaming.** The button on that card links to the upstream
  project's page for obtaining a streaming Worker. Using it routes video
  through that Worker. It is off unless you set it up.
- **Nuvio collection artwork, the limit notice videos and the addon logo**,
  as listed above.
- **Thumbnail uploads to Imgur** use the application ID that the upstream
  code ships with.

If these resources go away, the features degrade (missing images) but
indexing and streaming keep working.
