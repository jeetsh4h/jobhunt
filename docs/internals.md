# jobhunt internals

Reference notes for people changing the code. The [README](../README.md) covers everyday use.

## Launcher

`bin/jobhunt` runs `.venv/bin/python -B -m jobhunt.cli --root <checkout>` with `PYTHONDONTWRITEBYTECODE=1`, so runs leave no bytecode in the tree. `--root /path` selects another configuration directory. A dry-run ingest creates no files, opens no database and takes no lock.

## Network boundary

All requests go through `fetch.py`:

- Only GETs to fixed public Greenhouse, Lever and Ashby JSON endpoints, the three raw GitHub list files, and `go.vansh.nyc` short links (used by one list).
- Tokens are validated; redirects and proxies are disabled; TLS is verified.
- Responses are capped at 64 MiB (gzip accepted and bounded). Socket timeouts are bounded and attempts capped at three. Truncated bodies and resets retry. `Retry-After` accepts seconds or HTTP dates, capped at 30 seconds.
- A 60-second total deadline covers throttling, headers, body and retries. It uses `SIGALRM`, so the fetcher must run on the main thread.
- Requests to one host are spaced by `per_host_interval_seconds` (default 1.1).
- A short-link lookup reads the first `Location` header without following it. Results, including failures, are cached in `url_resolutions`.

## Ingest and snapshot holds

Each board's snapshot is atomic. All nested feed text, IDs, URLs and timestamps are validated before anything is written; a malformed source records an error and the next company continues. Recognized error envelopes are always rejected. Initially empty boards are valid.

A board can never go from some open jobs to an empty snapshot in one step, and a source with at least five open jobs cannot close more than half its postings on its first degraded snapshot. The first drop is held: incoming jobs update, missing jobs stay open, `sources.warning` and an audit event record the hold, and `status` shows it. The same missing set on the next successful run confirms the closures. To accept a drop immediately:

```sh
bin/jobhunt ingest --accept-drop greenhouse/example   # one board; a bare source name accepts all its boards
```

The global heartbeat only advances when no registered source has a persisted error or held drop.

GitHub lists behave like one source each. Only active, visible entries are stored. Inbox files are observations, not snapshots, so they never close jobs.

## Duplicates

A posting is grouped with an existing one when it has the same canonical URL (tracking parameters and trailing slash ignored; Workday locale and path-case variants collapse by host, tenant and requisition ID) or the same normalized company, title, overlapping location and season. The existing row keeps its earlier `first_seen`; `duplicate_of` points at the open representative. A non-`interested` application on any alias removes the whole group from the queue. Closed originals never hide an open alias.

## Classification

`classify.py` is deterministic and rule-based. Keywords come from `filters.yaml`.

- **Role family:** `software_internship` needs an intern keyword and a software keyword. Hardware, analytics, data science, security analyst, solutions/sales and ML research are separate families. "Forward Deployed Engineer" counts as software. Part-time titles are flagged with `part_time`.
- **Season:** `Summer 2027`, `summer of 2027`, or a May–August range near 2027. A Fall/Spring 2027 or Summer 2026 title conflicts even if the description mentions Summer 2027. A Summer 2027 mention only in the description does not match if the description names another 2027 term for the posting.
- **Degree:** PhD-only and high-school-only titles are excluded; roles mentioning an MS alternative stay.
- **Country:** explicit country, state and province tokens. Unknown locations stay visible for review; known non-US jobs are hidden by default. When the location field names no place, "Available Locations …" and "in office/hybrid in <city>" in the description decide.
- **Restriction notes:** null, or a short description of detected wording (no sponsorship, sponsorship available, US citizenship required, clearance mentioned, export-control/US-person language, CPT/OPT/F-1 welcome). Negations win over positive wording. "Beneficial but not required" clearance produces no note. Notes never exclude or reorder a job and are not legal advice.

## Pay and enrichment

`enrich` fetches the full public posting for open target internships that only have list metadata, newest first, capped per run. Greenhouse requests pay transparency, Lever fetches one posting, Ashby fetches the board with compensation once per organization per run. Other ATSs and custom sites are not enriched.

USD base-pay ranges use their midpoint. Week ÷ 40, month ÷ 173, year ÷ 2080. Foreign currency, bonuses and equity are ignored; amounts without a period stay unknown. With several bands, the highest midpoint wins. This is a ranking hint, not an offer.

## Queue order

1. Postings from the last 48 hours, newest posting time first.
2. Then by posting day (newest first), hourly pay (unknown last), `preferred_locations`, software title fit.

`posted_at` gives the age; `first_seen` is the fallback. `updated_at` never makes an old posting fresh. Sites that require an account are hidden unless `--include-accounts`.

## Pause

`pause` (or a `PAUSE` file in the root) blocks domain writes: ingest, mark, apply-pack, registry writes. Read commands open SQLite read-only (WAL sidecars may still appear). Exports, backups and explicit digest output are still allowed. Work already inside a transaction can finish; pause is checked again before the next one.

## Locking

Writers take `flock` on `<realpath of the database>.lock`, so two roots or symlinks pointing at one database cannot overlap. Kernel locks release on exit, including SIGKILL.

## Database, backup and restore

A new database is created from the current schema in `db.py`. There is no automatic schema upgrade path. Before changing the schema, back up your data and plan how to copy it into the new schema.

```sh
bin/jobhunt backup --out ~/jobhunt-backup.sqlite3
```

To restore: disable the timer and stop every process using the database, move the current database and its `-wal`/`-shm` files aside, copy the backup to the configured path, then open it with jobhunt and check `PRAGMA integrity_check` returns `ok`. Never combine a restored database with old sidecar files, and never back up by copying a live database file. Backups contain your data; there is no automatic backup schedule.

The audit log records ingest summaries, source warnings, application marks and pause changes. The digest uses ingest summaries to count closures.
