# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's
private reporting instead: on the repository page, **Security → Report a
vulnerability**. If that option is not available, open an issue that only
asks for a private contact and leave the details out.

Include the version, what an attacker can do, and the steps to reproduce.

Only the latest release is maintained. Problems that also exist in the
upstream project, [weebzone/Telegram-Stremio](https://github.com/weebzone/Telegram-Stremio),
are best reported there as well.

## Running the server safely

- **Change the admin password** before the server is reachable by anyone
  else. The default is `admin` / `admin`. The log warns at every start while
  it is unchanged.
- **Use HTTPS** when the server is on the internet. Access tokens are part of
  every addon and stream URL, so anything that can read the traffic can use
  them.
- **Treat tokens like passwords.** Give each person their own token so that
  one can be revoked. An exported M3U playlist contains a token.
- **Keep `config.env` private.** It holds the bot token and the database
  credentials. It is ignored by git; do not add it to an image you publish.
  The `*.session` files next to the code are Telegram logins and equally
  sensitive.
- **Set the update source only to a repository you trust.** The updater
  replaces the running code with that repository's code.
- **Restrict MongoDB.** Use a database user with a password and, on Atlas, an
  IP allow list.
- **Keep the installation current.** The pinned dependencies are checked
  against published advisories by the test workflow. One advisory is accepted
  on purpose: CVE-2025-67720 in pyrofork concerns downloads written to disk
  under a name taken from the message, and this project only downloads into
  memory.

## What the server exposes without a login

The sign-in form, `/status` (state and version), the web app manifest and
icon, the hand-off page `/open/...`, the public request form with its two
search lookups, and the install page of an access token. Everything else asks
for an admin session or a valid token; `tests/test_access_control.py` fails
when a new address answers an anonymous caller. The generated API pages
(`/docs`, `/redoc`, `/openapi.json`) are switched off.

The Docker image runs the server as root inside the container. Do not mount
host folders into it beyond `config.env`.

[docs/configuration.md](docs/configuration.md#external-services) lists the
third-party services the server contacts and what is sent to them.
