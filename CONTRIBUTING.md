# Contributing

Fixes and new sources are welcome. A few ground rules keep this tool safe to run:

- **No automation of signed-in sites.** No LinkedIn, Handshake or portal scraping, no form filling, no submitting. Pull requests that add these will be closed.
- **Be polite to job boards.** New network code goes through `fetch.py` and keeps its pacing, size and time limits.
- **No runtime dependencies.** The tool runs on the standard library. Ask in an issue before proposing one.
- **No personal data in the repo.** Fixtures come from public postings only. Never commit `answers.yaml`, `config.yaml`, `data/` or `packs/`.

Before opening a pull request:

```sh
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
uv run ty check
git diff --check
```

Use `uv run ruff format` to format changes. Runtime code must stay on the standard library;
pytest, Ruff and ty are development dependencies only.

Keep tests focused on behavior a user can observe through CLI commands, feed imports,
queue/status output and saved jobs or application marks. Add a regression case for each
behavior change, ideally from a small recorded fixture in `tests/fixtures/`. Classification
fixes should include the public title or sentence that was misread. Combine repeated cases
into a table and avoid tests of private helpers, internal call order or schema internals.
Tests must use temporary configuration and databases, never your personal files.

`db.py` defines one current schema without automatic upgrades. A schema change needs an
explicit plan to preserve existing users' jobs and application marks.
