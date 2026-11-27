# Usage

## Channels

The bot indexes files posted in the channels listed under **Settings →
AUTH_CHANNELS**. Add the bot to each of them as an administrator with
permission to post, edit and delete messages. Delete rights are only used by
Replace Mode and the duplicate tools.

A channel ID looks like `-1001234567890`. Enter it in this full form.

### Channel roles

A channel can have one role besides being an indexed channel.

| Role | Where to set | Effect |
| --- | --- | --- |
| Anime | Tick *Anime* next to an auth channel | Files resolve through Kitsu first, then TVDB, TMDb, Cinemeta |
| Manual | Manual Add Channels | Files are not indexed automatically. Add them from Media Management by pasting a message link. |
| Skip | Skip Channel | Files that cannot be indexed are copied here with a note saying what to fix. Files posted in this channel are never indexed. |
| Announcement | New Content Announcements | A post is made here when a new title is added |
| Global Search | Global Search | Limits the user session search to these channels |

## Naming files

The bot reads the caption of a post if there is one, otherwise the file name.
It needs a title and, for episodes, a season and episode. A resolution
(`720p`, `1080p`) should be present; it becomes the label of the stream.

```text
The Long Road (2019) 1080p BluRay x264.mkv         movie: title and year
Harbor Lights S02E03 720p WEB-DL.mkv               series: SxxEyy
Harbor Lights 2x03 720p.mkv                        series: 2x03 notation
[Group] Harbor Lights - 27 [1080p][HEVC].mkv       anime: absolute episode number
Harbor Lights S01E05v2 1080p.mkv                   a corrected release (v2)
Harbor Lights S00E01 1080p.mkv                     special
Harbor Lights OVA 2 1080p.mkv                      second OVA
```

Details that are useful to know:

- **Absolute numbers.** `Show - 27` on a show whose first season has 24
  episodes is filed as season 2 episode 3, following the season list of the
  metadata provider.
- **Specials.** `S00Exx` is always kept in season 0. A numbered special
  (`SP1`, `OVA 2`) is matched to the n-th full-length special of the show; a
  special with a title in its name is matched by that title.
- **Extras.** Creditless openings and endings are skipped: any name
  containing `NCOP` or `NCED`, and files named only `OP 01` or `ED 02`. A
  name such as `Harbor Lights OP 01.mkv` is not recognised as an extra and is
  read as episode 1 of "Harbor Lights OP"; rename or remove such files.
- **Split files.** Parts named `.001`, `.002` and so on are joined into one
  stream. Split zip archives (`.zip.001`) work when the archive stores the
  video without compression.
- **Subtitles.** A subtitle file (`.srt`, `.vtt`, `.ass`, `.ssa`, `.sub`)
  named like its video is attached to it. A language at the end of the name
  (`... .en.srt`) is used as the label. Subtitles can also be added by hand
  from **Media Management → Edit**.

### When the match is wrong

Put the IMDb ID (`tt1234567`), an IMDb link or a TMDb link in the caption of
the post. The bot then uses that title instead of searching by name. For
files already indexed, use **Rescan Metadata** on the title's edit page, or
correct the entry there.

Files that could not be matched go to the skip channel, if one is set.

## Bot commands

All commands are sent to the bot in a private chat. Except for `/start` and
`/status`, they answer only the owner (`OWNER_ID`).

| Command | What it does |
| --- | --- |
| `/start` | Replies with your personal addon link. With the subscription system on, it shows the plans or your subscription instead. Without subscriptions only the owner gets an answer. |
| `/status` | Shows your subscription status (subscription system) |
| `/scan_files 100-250` | Indexes messages 100 to 250 of the first auth channel. Also accepts a channel ID before the range, a single message link, or a start and an end link. |
| `/resolve_nyaa 3200-3227 https://nyaa.si/view/<id>` | Matches a range of batch-release files against the file list of a nyaa.si torrent, then indexes them |
| `/tidy` | Checks every indexed stream against Telegram, removes dead ones and cleans up empty entries |
| `/missing` | Lists the episodes missing from indexed shows. `/missing find` searches the chats of your user session for them. `/missing fill` copies the best match of each into the channel. `/missing fill sorted` also reposts the later episodes so the channel stays in order. Add a title to limit the run to one show. `/missing help` prints this. |
| `/sync_all` | Runs the AniList sync and the Jellyfin export now |

`/missing find` and `/missing fill` need a Telegram user session (Settings →
Telegram User Session).

## The web panel

| Page | Purpose |
| --- | --- |
| Dashboard | Library overview, active streams, setup checklist, update notice |
| Media Management | Browse, search, edit and delete titles; add subtitles; export M3U playlists; rescan metadata |
| Catalogs | Custom catalogs: which titles they contain, whether they are visible, exclusive or searchable |
| Access | Tokens and users: create, limit, expire, revoke; per-user statistics |
| Subscriptions | Plans and pending approvals |
| Requests | Titles requested through the public request page (`/request`) |
| Tools | Manual Upload Session, Channel Scanner, Bot Admin Manager, Database Check & Dead Links, Duplicate Check & Cleanup, Batch Topic & Range Resolver, Gap Reconciliation, AniList sync, addon links, Jellyfin sync |
| Settings | See [configuration.md](configuration.md#the-settings-page) |

`/status` is a public endpoint that returns the server version, for uptime checks.

**Manual Upload Session** (Tools) is for files that have no usable name:
pick the title first, optionally a season, then upload or forward the files.
They are filed under that title in the order they arrive.

## Players

### Stremio

Send `/start` to the bot, or open **Tools → Nuvio & Stremio Addon
Integration**, and copy the addon link. It has the form
`<Base URL>/stremio/<token>/manifest.json`. Paste it into the addon search box
of Stremio and install it. Each token is a separate login; make one per person
on the Access page.

Stremio needs an `https` address unless the server runs on the same machine.

### Nuvio

Use the same manifest link under **Settings → Add-ons** in Nuvio. The
configure page (`/stremio/<token>/configure`) also offers collection files for
Nuvio.

### mpv

`season2mpv.py` builds a playlist for a season or a movie, attaches subtitles
and starts mpv. It only uses the Python standard library, so it can be copied
to another computer on its own.

```bash
python season2mpv.py "Harbor Lights" --season 2          # one season
python season2mpv.py "Harbor Lights" --season all        # every season
python season2mpv.py "Harbor Lights" -s 1 --start 6      # from episode 6
python season2mpv.py "Harbor Lights" -s 1 -e 1-3,7       # selected episodes
python season2mpv.py "The Long Road"                     # a movie
python season2mpv.py "Harbor Lights" --list              # what is available
python season2mpv.py "Harbor Lights" --no-play -o hl.m3u # write the playlist only
```

When run on the server itself it finds the address and a token on its own.
Elsewhere, pass the addon link:

```bash
python season2mpv.py "Harbor Lights" --addon "https://example.org/stremio/<token>/manifest.json"
```

`python season2mpv.py --help` lists every option, including
`--min-quality`, `--sub-lang` and `--mpv` for a custom mpv path.

### M3U playlists

On a title's page in Media Management, the playlist buttons download an
`.m3u8` file for the movie, a season or a single episode. Any player that
opens M3U playlists can use it. The links inside contain an access token, so
treat the file like a password.

### WebDAV

The library is available as a read-only WebDAV share at
`<Base URL>/webdav/<token>/`. Set a user name and password under **Settings →
WebDAV**; if both are empty, the token alone grants access.

### Jellyfin, Emby, Kodi

**Tools → Automated Sync & Virtual Library** writes a folder of `.strm` and
`.nfo` files (default `./jellyfin_library`) that these media servers can add
as a library. With automatic sync on, the folder follows the index.

## Subscriptions

With **Enable Subscription System** on, `/start` offers the plans you defined
on the Subscriptions page. A user picks a plan, pays as described in your
payment instructions and sends proof; an approver confirms it, and the bot
gives the user a token that expires with the plan. Limits per day and month
can be set per token.

## Users' own bots

A user can stream through bots of their own, so Telegram's rate limits fall on
those bots and not on yours.

1. Owner: create one or more helper bots, add them as administrators to all
   auth channels and paste their tokens under **Settings → Helper Bots**.
2. User: open `/stremio/<token>/configure`, create a private channel, add
   their own bots (up to ten) and the helper bots shown on the page as
   administrators, enter the channel ID and bot tokens, then save.
3. When the user plays something, a helper bot copies the file into the
   user's channel and the user's bots stream it. The copy is deleted a day
   after it was last watched. If the user's bots fail, the stream falls back
   to your bots.

## Backups

**Settings → Backup & Restore** exports settings, catalogs, plans and tokens
to a file and restores them. The admin password and session secret are left
out of the export. The media index itself lives in MongoDB; back it up with
MongoDB's own tools (`mongodump`).
