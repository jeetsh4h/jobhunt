"""Capped public-ATS enrichment. No application or account endpoints."""

import json
from http.client import IncompleteRead

from .classify import classify
from .db import audit, connect
from .fetch import Fetcher
from .ingest import now, runner_lock
from .lists import refresh_duplicates, resolve_url
from .postings import ats_posting, posting_content
from .settings import config, ensure_active, filters


def enrich(root, *, limit=80, fetcher=None):
    if limit < 1:
        raise ValueError('limit must be positive')
    ensure_active(root)
    settings, rules = config(root), filters(root)
    fetcher = fetcher or Fetcher(settings)
    with runner_lock(root / settings['database']) as acquired:
        if not acquired:
            return {'skipped': 'overlapping run'}
        db = connect(root / settings['database'])
        summary = {'attempted': 0, 'enriched': 0, 'failures': []}
        try:
            rows = db.execute("""SELECT * FROM jobs WHERE status='open' AND matches_target=1
                AND role_family='software_internship' AND country!='non-US' AND degree_note IS NULL
                AND posting_description IS NULL
                AND (listing_description IS NOT NULL OR trim(description_text)='')
                ORDER BY date(coalesce(posted_at,first_seen)) DESC,id DESC""").fetchall()
            payloads = {}
            for row in rows:
                ensure_active(root)
                with db:
                    resolved = resolve_url(db, fetcher, row['url'], now())
                    db.execute(
                        'UPDATE jobs SET resolved_url=? WHERE id=? AND resolved_url IS NOT ?',
                        (resolved, row['id'], resolved),
                    )
                posting = ats_posting(resolved)
                if posting is None:
                    continue
                if summary['attempted'] >= limit:
                    break
                summary['attempted'] += 1
                source, token, identifier = posting
                try:
                    # Ashby returns a board: reuse it for other postings in this run.
                    key = (source, token, None if source == 'ashby' else identifier)
                    if key not in payloads:
                        payloads[key] = fetcher.posting(source, token, identifier)
                    description, pay = posting_content(source, payloads[key], identifier)
                    if not description:
                        raise ValueError('posting has no description')
                    ensure_active(root)
                    with db:
                        # Keep list cohort evidence separate so future list snapshots do not erase enrichment.
                        combined = ' '.join(filter(None, (row['listing_description'], description)))
                        hints = classify(row['title'], combined, rules, row['location_raw'])
                        values = {
                            'posting_description': description,
                            'description_text': combined,
                            'pay_hourly_usd': pay,
                            **hints,
                        }
                        db.execute(
                            'UPDATE jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                            [*values.values(), row['id']],
                        )
                    summary['enriched'] += 1
                    print(f'enriched {row["id"]}: {row["company"]} ({source})', flush=True)
                except (OSError, ValueError, TypeError, KeyError, IncompleteRead) as exc:
                    error = f'job {row["id"]}: {type(exc).__name__}: {exc}'
                    summary['failures'].append(error)
                    print(error, flush=True)
            ensure_active(root)
            with db:
                refresh_duplicates(db)
                audit(db, 'enrich_complete', 'jobs', json.dumps(summary))
            return summary
        finally:
            db.close()
