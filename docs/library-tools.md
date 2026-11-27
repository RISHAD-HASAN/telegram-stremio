# Library tools

The scripts in `tools/` work on the MongoDB library directly, without the bot
and without Telegram. They are for checking a library in bulk. Day-to-day
indexing does not need them.

All of them read `DATABASE` from the environment or from `config.env` in the
repository root, and need the same Python environment as the server. They can
be started from any directory; reports are written to the current one. The
indexer-based tools load the server's parser and therefore also write a
`log.txt` there.

What the tools write to the database:

| Tool | Writes |
| --- | --- |
| `dump_all_filenames.py`, `diagnose_library.py`, `indexer_gap.py` | Nothing |
| `indexer.py`, `repair_library.py`, `reindex_full_pipeline.py` | Nothing. Their `--apply` option is switched off, see [Repair](#repair). |
| `nyaa_resolve.py`, `nyaa_repair.py` | With `--apply`: the quality label of existing streams. The affected documents are saved to `nyaa_fix_backup_<time>_<id>.json` before the first change. |

Stop the server, or at least do not index new files, while applying changes.

## The export

Most tools start from an export of what is indexed:

```bash
python tools/dump_all_filenames.py
```

This writes `filenames_report.txt` (tab-separated, one stream per line) and
`filenames_report.json`. The database is only read.

Columns of the text file: media type, storage database, title, season and
episode, quality label, size, split flag, file name.

## Preview how names would be indexed

```bash
python tools/indexer.py --dump filenames_report.txt --parse-only
```

Writes `indexer_report_parse.tsv`, showing how each file name is read (title,
season, episode, quality) without contacting any provider or database. Useful
for trying naming schemes.

`--scan <folder>` reads names from a local folder instead of an export.

## Find files filed in the wrong place

```bash
python tools/diagnose_library.py --dump filenames_report.txt
```

Parses every file name again and compares the result with where the file
sits in the library. Results go to `diagnose_output/`:

- `diagnose_report.csv` and `.json`: every finding (wrong media type, wrong
  season or episode, misplaced special, title mismatch, quality mismatch)
- `repair_plan.tsv`: the affected files, as input for the next step

The tool stops with an error when a storage database cannot be read. A
partial comparison would report every file stored there as missing.

## Repair

```bash
python tools/repair_library.py --plan diagnose_output/repair_plan.tsv
```

Runs the files in the plan through `indexer.py`: a fresh parse and a provider
lookup. The report (`indexer_report.txt` and `.json`, or the name given with
`--out`) lists the title, season and episode each file belongs to, grouped by
confidence.

`reindex_full_pipeline.py` runs diagnose and this step in sequence and writes
a log of every step:

```bash
python tools/reindex_full_pipeline.py --dump filenames_report.txt
```

These tools report; they do not change the library. `--apply` exists on all
three but is switched off: the write path behind it stores streams under an
identifier the server cannot decode, leaves the wrongly filed entry in place
and takes its backup after the change. `python tools/indexer.py --apply`
prints the details. Apply the corrections on the server instead, as described
under [When the match is wrong](usage.md#when-the-match-is-wrong).

## Reconcile an export with the database

```bash
python tools/indexer_gap.py --dump filenames_report.txt
```

Compares an export (for instance an older one, or one from another
installation) with the current database and reports which files are missing
or filed differently. `gaps_only.txt` can be fed into
`indexer.py --dump gaps_only.txt` to see where those files belong.

## Quality labels from nyaa.si

For libraries built from nyaa releases, two tools compare stored quality
labels with the release names on nyaa.si:

```bash
python tools/nyaa_resolve.py        # reads filenames_report.json
python tools/nyaa_repair.py         # reads filenames_report.txt
```

Both query nyaa.si with a delay between requests and cache the answers in
`nyaa_cache.json`, so an interrupted run can be resumed. They write a report
with the proposed label for each stream and the evidence for it. A file much
smaller than the release on nyaa is flagged as a probable re-encode and left
alone. `--apply` writes only the high-confidence changes, after saving the
affected documents to a backup file.

## Options shared by the indexer-based tools

| Option | Meaning |
| --- | --- |
| `--anime-channel` | Resolve through Kitsu first |
| `--concurrency N`, `--delay S` | Provider request rate |
| `--out PATH` | Where reports are written |
| `--resume` (`indexer.py`) | Skip records already resolved in an earlier report |

`python tools/<name>.py --help` prints the full list for each tool.

## Standalone checks

`tests/manual/` holds three scripts that are not part of the pytest suite:

```bash
python tests/manual/check_indexer.py       # parser checks for the offline indexer
python tests/manual/check_watch_order.py   # watch-order grouping
python tests/manual/check_filler.py        # filler flags; calls the public Jikan API
```

If a `filenames_report.txt` is present in the repository root (or its path is
in `FILENAMES_REPORT`), the scripts also parse every file name in it and
report names that fail.
