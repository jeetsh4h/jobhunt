# jobhunt

A local tool for finding **US software engineering internships for Summer 2027** and getting ready to apply to them by hand.

It pulls postings from public company job boards (Greenhouse, Lever, Ashby) and three community-maintained GitHub internship lists, stores them in SQLite, and ranks the ones that fit: newest first, then by hourly pay. For each job you pick, it writes an "apply pack": a Markdown page with the link, the date and season evidence, any sponsorship or citizenship wording it found, your resume path and your prepared form answers. You read the posting and submit the application yourself.

It runs on your machine, needs no accounts or API keys, and has no runtime dependencies beyond Python.

## What it does not do

These limits are deliberate:

- **It never submits an application, fills a form, or sends email.** Every application goes through you.
- **It does not log in anywhere or scrape LinkedIn, Handshake or any other signed-in site.** Their terms forbid automation and accounts get banned. You can import rows you copied yourself (see [Importing your own rows](#importing-your-own-rows)).
- **It does not call an AI model** or write cover letters.
- **It does not judge your eligibility.** Sponsorship and citizenship notes quote what the posting says; they never hide or reorder a job. Only you can answer authorization questions.
- **It is gentle with job boards:** only public JSON endpoints, about one request per 1.1 seconds per host, at most three attempts, and a 60-second limit per request.

## Requirements

- Linux. It uses `SIGALRM` and `flock`; macOS might work but is untested. Windows is not supported (WSL2 is fine).
- Python 3.14 and [uv](https://docs.astral.sh/uv/).
- Optional: systemd (for the daily timer), git (for the resume freshness check).

## Quickstart

The commands work in bash, zsh and fish.

```sh
git clone <your-fork-url> jobhunt
cd jobhunt
uv sync
cp config.example.yaml config.yaml
cp answers.example.yaml answers.yaml
chmod 600 answers.yaml
```

Check that everything starts without the network:

```sh
uv run pytest -q
bin/jobhunt --help
```

Then fetch real postings:

```sh
bin/jobhunt ingest --dry-run   # fetch and parse everything, write nothing
bin/jobhunt ingest             # creates data/jobhunt.sqlite3; first run can take an hour
bin/jobhunt enrich --limit 80  # fetch full descriptions and pay for list-only postings
bin/jobhunt queue --limit 20   # what to apply to next
```

The first full ingest can take about 60 minutes when community-list short links need resolving. Later runs reuse cached link resolutions.

Each queue line shows the job ID, company, title, location, age in days, hourly pay (or `?`), link, ATS and whether the site needs an account. Jobs on Workday, iCIMS, Taleo, Avature and SuccessFactors (which make you create an account) are hidden unless you pass `--include-accounts`.

Now open `answers.yaml` and fill in what you can. It uses JSON syntax (valid YAML, no extra library needed). Leave anything you are unsure about empty: packs list empty fields as `MISSING` rather than guessing.

## Daily use

```sh
bin/jobhunt digest --out          # summary in digest.md: fresh jobs, top 15, warnings, your to-dos
bin/jobhunt apply-pack 132        # writes packs/132.md for job 132
bin/jobhunt mark 132 applying
bin/jobhunt mark 132 applied --note 'Referral from a TA'
bin/jobhunt answer contact.phone  # print one answer, e.g. to paste into a form
```

Application states are `interested`, `applying`, `applied`, `skipped`, `rejected`, `interview` and `offer`. Anything except `interested` removes the job (and its duplicates from other sources) from the queue.

Other commands:

| Command | What it does |
|---|---|
| `show <id>` | Every stored field for one job |
| `list` / `export-csv` | Filter all stored jobs (`--status open`, `--role-family software_internship`, `--matches-target`, `--include-non-us`, `--since`) |
| `shortlist --since 7d` | The queue as CSV, limited to jobs first seen recently |
| `status` | Sources, errors, held drops, database size |
| `pause` / `resume` | Block new ingest, mark and apply-pack writes; a run already in progress can finish |
| `backup --out <file>` | Consistent SQLite copy; refuses to overwrite |
| `heartbeat-check` | Exit 1 if no healthy ingest in the last 26 hours |

Packs mask your phone number as `[phone: answers.yaml contact.phone]`; use `jobhunt answer contact.phone` when a form needs it. Packs still hold your other answers, so keep them private.

## Configuration

| File | Tracked? | What to change |
|---|---|---|
| `config.yaml` | No (copy from `config.example.yaml`) | Resume paths, preferred locations, weekly target, request pacing |
| `answers.yaml` | No (copy from `answers.example.yaml`) | Your form answers. Private: never commit it |
| `companies.toml` | Yes | Company boards and GitHub lists to watch |
| `filters.yaml` | Yes | Keywords for role families and the sponsorship/citizenship wording patterns |

Both `.yaml` files use JSON syntax. Ordinary indented YAML is rejected.

Useful `config.yaml` keys:

- `preferred_locations`: substrings of the normalized location that break ties after date and pay, e.g. `["Seattle", "Boston"]`. New York City, its boroughs, Jersey City, Hoboken and Newark NJ all normalize to `NYC metro`.
- `weekly_application_target`: shown in the digest. Use `0` to hide it.
- `resume_pdfs`: resume PDF per variant (default variant `software-engineer`). Relative paths resolve against `resume_repository` if set, otherwise against this folder. `resume/` is gitignored for this.
- `resume_repository` and `resume_source` (optional): if your resume source lives in git, the pack warns when the PDF is older than the source's latest commit.

Personal files (`config.yaml`, `answers.yaml`, `data/`, `packs/`, `digest.md`, `resume/`) are gitignored. Packs are written with owner-only permissions.

## Run it every day (optional, systemd)

`scripts/fresh-watch` runs ingest, enrichment (80 jobs max), the queue and the digest, and keeps going if one step fails. The included timer runs it once a day at 09:00 local time and catches up after the laptop was asleep.

```sh
scripts/install-timer
```

This writes `jobhunt.service` (with this checkout's path filled in) and `jobhunt.timer` to `~/.config/systemd/user/`, then enables the timer. Change the time by editing `OnCalendar=` in `systemd/jobhunt.timer` and running the script again. Check runs with:

```sh
systemctl --user list-timers jobhunt.timer
journalctl --user -u jobhunt.service
```

The timer only runs while you are logged in (no lingering is configured). It sends no notifications: read `digest.md`.

## Sources

| Source | What it reads |
|---|---|
| `greenhouse`, `lever`, `ashby` | Each company's public job-board JSON, listed in `companies.toml` |
| `simplify` | `SimplifyJobs/Summer2027-Internships` listings JSON |
| `vanshb03` | `vanshb03/Summer2027-Internships` listings JSON |
| `speedyapply` | The USA internship table in `speedyapply/2027-SWE-College-Jobs` |
| `feed` | Your own JSONL files in `data/inbox/` |

The same job often shows up on several sources. Duplicates (same URL, or same company, title, overlapping location and season) are grouped and shown once.

### Add a company

Find the board token in the company's careers URL, e.g. `boards.greenhouse.io/figma` → `figma`, `jobs.lever.co/plaid` → `plaid`, `jobs.ashbyhq.com/linear` → `linear`. Then add:

```toml
[[companies]]
name = "Figma"
source = "greenhouse"
token = "figma"
```

`bin/jobhunt ingest --source greenhouse` checks it. The shipped list was generated by `scripts/verify-boards`, which only keeps boards that answered with valid JSON. To keep a company through future regenerations, add it to `CANDIDATES` in that script and rerun it.

### Importing your own rows

Put one JSON object per line in `data/inbox/<anything>.jsonl` (write the file elsewhere and move it in when complete). The next `ingest` (or `ingest --source feed`, which needs no network) imports it and moves the file to `data/inbox/processed/`.

```json
{"source":"handshake","external_id":"123","company":"Example","title":"Software Intern Summer 2027","location":"New York, NY","url":"https://example.com/jobs/123","posted_at":"2026-11-06","pay_text":"$50/hr","description":"Summer 2027 software internship","seen_at":"2026-11-06T19:00:00-05:00"}
```

`source` is `handshake` or `linkedin`; `pay_text` and `description` are optional; `posted_at` may be `null`. Copy these rows by hand or with a tool you are allowed to use. Imported rows are observations, so a job missing from a later file is never closed.

### Add a new kind of source

A board source needs an endpoint and token rule in `src/jobhunt/fetch.py` and a parser in `src/jobhunt/feeds.py` that returns the common job dict. A GitHub list needs an entry in `fetch.LISTS` and a parser in `src/jobhunt/lists.py`. Record a small real response under `tests/fixtures/` and add a test next to the existing ones.

## How it works

```
companies.toml ─┐
GitHub lists ───┼─ fetch.py ─ feeds.py / lists.py / inbox.py ─ classify.py ─ ingest.py ─ SQLite
data/inbox/ ────┘                                                                          │
                     enrich.py (full description, pay) ───────────────────────────────────┤
                     queue.py ─ cli.py: queue, digest, shortlist, apply-pack (workflow.py) ┘
```

- `fetch.py`: the only network code. Fixed hosts, TLS, no redirects, size and time limits, retries, per-host pacing.
- `classify.py`: rule-based (no AI). Role family, Summer 2027 evidence, country, normalized location, degree requirements, restriction notes. Keywords come from `filters.yaml`.
- `ingest.py`: one atomic snapshot per board. If a board suddenly loses most of its jobs, the drop is held until the next run confirms it, so one bad response cannot close everything.
- `postings.py` and `enrich.py`: pay extraction (USD base pay converted to hourly) and ATS detection.
- `queue.py`: postings from the last 48 hours first, by exact posting time; then by posting day, hourly pay, preferred location and title fit.
- `db.py`: the current SQLite schema, explicit backups and an audit log. Writers use a file lock so two runs cannot overlap.

[docs/internals.md](docs/internals.md) has the details: snapshot holds, locking, pause semantics, backup and restore, classification rules and pay parsing.

## Known limits

- The season (Summer 2027) and the three list repositories are built in. Targeting another cycle means editing the season rules in `classify.py` and `lists.py` and the list URLs in `fetch.py`.
- Classification is a finite set of rules tested against real postings, not a language model. Some jobs land in the wrong family; `filters.yaml` is where you tune it.
- Custom careers sites and ATSs other than Greenhouse, Lever and Ashby are only seen through the GitHub lists, and their pay is not enriched.

## Development

```sh
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
uv run ty check
```

Use `uv run ruff format` to format changes before committing. Ruff checks Python 3.14 code
with a 110-column line limit; ty checks the source, tests and build tools. Development
packages are separate from the dependency-free runtime.

Tests use recorded HTTP responses, local feed imports and temporary databases; they never
touch your `data/` or make network requests. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE), copyright (c) 2026 Jeet Shah.
