# Installation

Telegram Stremio runs anywhere Python 3.11 to 3.13 and MongoDB are available.
Pick one of the routes below. All of them end with the same two steps: fill in
`config.env` and start the server.

## What you need first

1. **Telegram API credentials.** Sign in at [my.telegram.org](https://my.telegram.org),
   open "API development tools" and create an application. Note the `api_id`
   and `api_hash`.
2. **A bot.** Talk to [@BotFather](https://t.me/BotFather), send `/newbot` and
   note the token.
3. **Your user ID.** [@userinfobot](https://t.me/userinfobot) tells you the
   number.
4. **MongoDB.** Either install MongoDB Community Server locally, or create a
   free cluster on MongoDB Atlas and copy its connection string. The server
   uses two databases: one for tracking (settings, tokens, users) and one or
   more for the library. Both can be on the same MongoDB server.

## Linux and macOS

```bash
git clone https://github.com/RISHAD-HASAN/telegram-stremio.git
cd telegram-stremio
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp sample_config.env config.env
```

Edit `config.env` (see [configuration.md](configuration.md)), then start:

```bash
python -m Backend
```

Always start from the repository root; the bot plugins and `log.txt` are
looked up relative to the current directory.

To keep the server running after you log out, use your usual process manager.
A minimal systemd unit:

```ini
[Unit]
Description=Telegram Stremio
After=network-online.target

[Service]
WorkingDirectory=/opt/telegram-stremio
ExecStart=/opt/telegram-stremio/.venv/bin/python -m Backend
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Adjust the two paths to where you cloned the repository.

## Windows

1. Install Python 3.11, 3.12 or 3.13 from [python.org](https://www.python.org/downloads/)
   and tick "Add Python to PATH" in the installer.
2. Install MongoDB Community Server as a service, or use Atlas.
3. Download or clone the repository.
4. Double-click `install.bat`. It finds Python and installs the packages from
   `requirements.txt`.
5. Double-click `start.bat`. On the first run it creates `config.env` from the
   template and stops so that you can fill it in. Run it again afterwards.

The batch files are a convenience. The manual commands work as well:

```bat
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy sample_config.env config.env
python -m Backend
```

## Docker

```bash
git clone https://github.com/RISHAD-HASAN/telegram-stremio.git
cd telegram-stremio
cp sample_config.env config.env     # then edit it
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yaml` builds the image from the `Dockerfile`, publishes port
8000 and mounts `config.env` into the container. The compose file does not
start MongoDB; point `DATABASE` at Atlas or at a MongoDB instance the
container can reach. `localhost` inside the container is the container itself,
so a database on the Docker host needs the host's address instead.

If you change `PORT`, change the port mapping in `docker-compose.yaml` to
match.

The image starts through `start.sh`, which runs the updater and then the
server. See [Updating](#updating).

## Hosting platforms

The repository includes `heroku.yml` and `render.yaml`. Both build the same
Docker image. On such platforms there is no `config.env`; set the required
values (`API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `DATABASE`) as
environment variables in the platform's dashboard, and use MongoDB Atlas for
the database. Platforms usually provide `PORT` themselves.

These two files are carried over from the upstream project. They have not been
exercised against the platforms as part of this fork's testing.

## Optional packages

Two native extensions speed things up and are safe to skip:

```bash
pip install PyroTgCrypto zstandard
```

`PyroTgCrypto` makes Telegram transfers faster; without it a pure Python
implementation is used. `zstandard` lets the anime episode maps be downloaded
compressed; without it the plain JSON feed is used.

## Making the server reachable

`http://127.0.0.1:8000` only works on the machine itself. For other devices:

- On your home network, set **Base URL** in Settings to
  `http://<address of the server>:8000`.
- On the internet, put the server behind a reverse proxy with HTTPS (Caddy,
  nginx, a tunnel) and set **Base URL** to the public `https://` address.
  Stremio on most platforms refuses plain `http` addons unless they are on
  `127.0.0.1`.

**Base URL** is what the server writes into addon and stream links, so players
fail when it is wrong.

Before exposing the server, change the default admin password.

## Updating

How you update depends on how you installed.

**Git clone:** pull and reinstall the requirements.

```bash
git pull
pip install -r requirements.txt
```

Then restart the server.

**Docker or a platform:** the container runs `update.py` at every start. When
an update source is configured (Settings → **Upstream Repo** and **Upstream
Branch**, or `UPSTREAM_REPO` / `UPSTREAM_BRANCH` in the environment), the code
inside the container is reset to the newest commit of that branch before the
server starts. The **Restart App** button on the Settings page triggers the
same thing. Without an update source the installed code is kept.

Some care is needed here:

- The update replaces every tracked file with the version from the update
  source. Local changes inside the container are lost. Files ignored by git
  (`config.env`, `log.txt`, session files) are kept.
- Point the update source only at a repository you trust; its code runs with
  access to your configuration.
- `update.py` never touches a folder that is a normal git checkout. Use
  `git pull` there.

## Checking the installation

```bash
python -m Backend
```

A good start ends with `Telegram Stremio Started Successfully!`. If the server
stops earlier, the last lines say why; see [troubleshooting.md](troubleshooting.md).
