# Contributing

Bug reports and pull requests are welcome.

## Reporting a bug

Open an issue and include:

- the version (first line of the log, or the dashboard),
- how the server is installed (Python version and operating system, or Docker),
- what you did, what you expected and what happened,
- the relevant part of the log.

For indexing problems, the exact file name or caption matters more than
anything else. Remove bot tokens, database URIs and channel IDs before
posting. Security problems go through [SECURITY.md](SECURITY.md), not the
issue tracker.

## Changing the code

```bash
git clone https://github.com/RISHAD-HASAN/telegram-stremio.git
cd telegram-stremio
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest
```

The tests run without MongoDB, Telegram or network access.
[docs/architecture.md](docs/architecture.md) explains the layout and points
out the modules that need care.

Guidelines:

- Keep a pull request to one topic.
- Add or adjust a test when you change how file names are parsed or matched.
  Use invented titles in tests, not names from a real channel.
- Do not commit `config.env`, session files, logs, database exports or
  anything else from a running installation. `.gitignore` covers the usual
  ones.
- Existing behaviour that users rely on (settings, URLs, bot commands, the
  database layout) should keep working. Say so in the pull request when a
  change is not backwards compatible.
- Update the documentation and `CHANGELOG.md` when behaviour changes.
- Follow the style of the surrounding code. Comments describe what the code
  does, not how it came about.

## Licence

The project is licensed under GPL-3.0. Contributions are accepted under the
same licence.
