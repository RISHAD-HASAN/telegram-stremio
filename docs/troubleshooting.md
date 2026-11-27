# Troubleshooting

The log is the first place to look. It is printed to the terminal, written to
`log.txt` in the folder the server was started from (emptied at each start
when the updater runs first, as in Docker and `start.sh`) and shown under
**Settings → System & Maintenance → Logs**. With Docker:
`docker compose logs -f`.

## The server does not start

**`Missing Python packages: ...`**
The packages are not installed in the Python environment that runs the
server. Activate the virtual environment and run
`pip install -r requirements.txt`.

**`Telegram Stremio needs Python 3.11 or newer`**
Install Python 3.11, 3.12 or 3.13 and recreate the virtual environment.

**pip fails while building a package**
This usually means Python 3.14 or newer, for which a pinned dependency has no
prebuilt package. Use 3.11 to 3.13.

**`The configuration is incomplete`**
Each line under the message names a value that is missing in `config.env`.
`API_ID` must be a number. `DATABASE` needs two URIs separated by a comma, in
one line, without spaces inside the URIs.

**`MongoDB is not available`**

- *no answer from the server*: MongoDB is not running, the host or port is
  wrong, or a firewall is in the way. For Atlas, add the server's IP address
  under Network Access. Inside Docker, `localhost` is the container, not the
  host.
- *the server rejected the user name or password*: check the credentials in
  the URI. Special characters in a password must be percent-encoded (`@`
  becomes `%40`).
- *the URI is not valid*: the URI is misspelled; it must start with
  `mongodb://` or `mongodb+srv://`.

**The log stops around the Telegram bot start, or shows `ACCESS_TOKEN_INVALID`
or `API_ID_INVALID`**
`BOT_TOKEN`, `API_ID` or `API_HASH` is wrong. Copy them again from BotFather
and my.telegram.org. A revoked token must be replaced.

**`FLOOD_WAIT`**
Telegram is rate-limiting the bot after too many logins or requests. Wait the
number of seconds given and avoid restart loops.

**`Address already in use`**
Another program uses the port. Change `PORT` in `config.env`, or stop the
other program.

## The bot

**The bot does not answer `/start`**
Without the subscription system the bot only answers the owner. Check that
`OWNER_ID` is your numeric user ID, not your user name, and restart.

**A command is missing from the bot's menu**
The menu is set at start-up. Restart the server, then reopen the chat.

## Indexing

**Nothing happens when I post a file**

- The channel ID is not under AUTH_CHANNELS, or it is listed as a manual or
  skip channel.
- The bot is not an administrator of the channel.
- The file is not a video or subtitle file.

The log has a line when a file is indexed (`... updated with ID`) and a
warning when the metadata lookup for it failed. No line at all means the bot
never saw the post, or the channel is not one it indexes.

**A file went to the skip channel**
The note on the copied post says what was missing, usually a readable title
or the quality. Rename the file or add a caption, then forward it to the auth
channel again.

**A file is filed under the wrong title**
Add the IMDb ID or a TMDb link to the caption and index it again, or use
**Rescan Metadata** on the title's edit page.

**An episode has the wrong number**
Absolute numbers are mapped to seasons with the provider's season list, which
for some long-running shows differs from the numbering release groups use.
Rename the file with an explicit `SxxEyy`.

**A new upload did not replace the old one**
Replace Mode only replaces a file with one that is better at the same
resolution, or of equal quality and smaller. Otherwise the new file is not
indexed. The bot also
needs permission to delete messages in the channel.

**Older files are not in the library**
The bot only sees new posts. Use the Channel Scanner on the Tools page or
`/scan_files <first>-<last>`.

**Entries point to deleted posts**
Run `/tidy` or **Tools → Database Check & Dead Links**.

## Playback

**The addon installs but nothing plays, or plays only on the server**
**Base URL** in Settings is wrong. It must be the address the player can
reach, with `https://` when the server is on the internet.

**Stremio refuses to install the addon**
Stremio accepts `http` addons only from `127.0.0.1`. Put the server behind
HTTPS.

**Streams stall or start slowly**
Telegram limits how fast one bot can download. Add more bots under **Settings
→ Multi-Token Clients**; each must be an administrator of the channels. The
dashboard shows which client serves each stream.

**A token stopped working**
It may have expired, hit its daily or monthly limit, or been revoked. Check
it on the Access page.

**`season2mpv.py` cannot find mpv**
Install mpv, or pass its location with `--mpv "C:\path\to\mpv.exe"`.

**`season2mpv.py` cannot find the server**
Pass the addon link with `--addon`.

## The web panel

**I forgot the admin password**
See [Changing the admin password](configuration.md#changing-the-admin-password).

**The panel has no styling**
The panel loads its styles, icons and fonts from public CDNs. The browser
needs internet access.

**Restart App does not update**
An update source must be set (Upstream Repo and Branch), git must be
installed, and the folder must not be a normal git checkout. In a checkout,
run `git pull` instead. The reason is written at the top of the log after the
restart.

## Reporting a problem

Open an issue with the version (shown on the dashboard and in the first log
line), how you installed, and the relevant part of the log. Remove tokens,
database URIs, channel IDs and file names you do not want to share. For
security problems see [SECURITY.md](../SECURITY.md).
