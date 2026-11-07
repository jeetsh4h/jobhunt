"""Fresh postings first, then posting day, USD hourly pay, preferred location and role fit."""

import re
from datetime import UTC, datetime

from .lists import identity_groups
from .postings import ats_type, hourly_pay
from .shortlist import since_time

ACCOUNT_ATS = {'workday', 'icims', 'avature', 'taleo', 'successfactors'}


def posting_date(row):
    value = row['posted_at'] or row['first_seen']
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def queue(db, limit=25, since=None, *, at=None, include_accounts=False, preferred=()):
    if limit < 1:
        raise ValueError('limit must be positive')
    at = at or datetime.now(UTC)
    cutoff = since_time(since) if since else None
    done = {r[0] for r in db.execute("SELECT job_id FROM applications WHERE status!='interested'")}
    results = []
    # Include closed/applied aliases in identities so a duplicate cannot re-enter the queue.
    for group in identity_groups(db.execute('SELECT * FROM jobs')):
        if any(r['id'] in done for r in group):
            continue
        candidates = [
            r
            for r in group
            if r['status'] == 'open'
            and r['role_family'] == 'software_internship'
            and r['matches_target']
            and r['country'] != 'non-US'
            and r['degree_note'] is None
            and (cutoff is None or r['first_seen'] >= cutoff)
        ]
        if not candidates:
            continue
        row = dict(min(candidates, key=lambda r: (r['duplicate_of'] is not None, r['first_seen'], r['id'])))
        # A list alias may have the actual release date or salary absent from the board.
        dates = [posting_date(r) for r in group if r['posted_at']]
        released = min(dates) if dates else posting_date(row)
        pays = [
            r['pay_hourly_usd'] if r['pay_hourly_usd'] is not None else hourly_pay(r['description_text'])
            for r in group
        ]
        row['pay_hourly_usd'] = max((p for p in pays if p is not None), default=None)
        row['resolved_url'] = next(
            (
                r['resolved_url']
                for r in group
                if r['resolved_url'] and 'go.vansh.nyc/' not in r['resolved_url']
            ),
            row['url'],
        )
        age_seconds = (at - released).total_seconds()
        row['age_days'] = max(0, age_seconds / 86400)
        row['fresh'] = 0 <= age_seconds <= 48 * 3600
        row['ats'] = ats_type(row['resolved_url'])
        row['needs_account'] = (
            'yes' if row['ats'] in ACCOUNT_ATS else 'unknown' if row['ats'] == 'unknown' else 'no'
        )
        if any(ats_type(r['resolved_url'] or r['url']) in ACCOUNT_ATS for r in group):
            row['needs_account'] = 'yes'
        if row['needs_account'] == 'yes' and not include_accounts:
            continue
        fit = (
            1
            if re.search(r'software|backend|frontend|full.stack|forward.deployed', row['title'], re.I)
            else 0
        )
        row['_rank'] = (
            -min(released.date(), at.date()).toordinal(),
            -(row['pay_hourly_usd'] if row['pay_hourly_usd'] is not None else -1),
            0 if preferred_location(row['location_normalized'], preferred) else 1,
            -fit,
            -row['id'],
        )
        row['_rank'] = (0, -released.timestamp(), *row['_rank']) if row['fresh'] else (1, 0, *row['_rank'])
        results.append(row)
    return sorted(results, key=lambda row: row['_rank'])[:limit]


def preferred_location(normalized, preferred):
    return any(place.casefold() in normalized.casefold() for place in preferred)


def queue_line(row):
    pay = f'${row["pay_hourly_usd"]:.2f}/h' if row['pay_hourly_usd'] is not None else '?'
    return (
        f'{row["id"]} | {row["company"]} | {row["title"]} | {row["location_raw"]} | '
        f'{row["age_days"]:.1f}d | {pay} | {row["resolved_url"]} | '
        f'{row["ats"]} | account={row["needs_account"]}'
    )


def queue_sections(rows):
    lines = []
    for fresh, title in ((True, 'Fresh (posted in the last 48h)'), (False, 'Other queue items')):
        if lines:
            lines.append('')
        lines.append(title)
        lines.extend([queue_line(row) for row in rows if row['fresh'] == fresh] or ['- none'])
    return lines
