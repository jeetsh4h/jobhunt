"""Local Handshake/LinkedIn observations. No scraping or network requests."""

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .classify import classify, strip_html
from .db import audit
from .lists import Seen, canonical_url, refresh_duplicates, term_key
from .postings import hourly_pay
from .settings import ensure_active


def timestamp(value, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError('expected ISO date/time')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC).isoformat()


def parse_observation(row, rules):
    if not isinstance(row, dict) or row.get('source') not in ('handshake', 'linkedin'):
        raise ValueError('source must be handshake or linkedin')
    for key in ('external_id', 'company', 'title', 'location', 'url'):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError(f'{key} must be nonempty text')
    url = row['url'].strip()
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('invalid job URL')
    for key in ('description', 'pay_text'):
        if not isinstance(row.get(key, ''), str):
            raise ValueError(f'{key} must be text when supplied')
    description = strip_html(' '.join((row.get('description', ''), row.get('pay_text', ''))))
    title, company, location = (strip_html(row[key]) for key in ('title', 'company', 'location'))
    if not title or not company or not location:
        raise ValueError('title, company and location must contain text')
    seen = timestamp(row['seen_at'])
    return dict(
        source=row['source'],
        requisition_id=row['external_id'].strip(),
        company=company,
        title=title,
        location_raw=location,
        url=url,
        resolved_url=url,
        posted_at=timestamp(row['posted_at'], nullable=True),
        updated_at=None,
        first_seen=seen,
        last_seen=seen,
        status='open',
        description_text=description,
        listing_description=description,
        pay_hourly_usd=hourly_pay(description),
        **classify(title, description, rules, location),
    )


def import_inbox(root, db, rules, summary):
    """Commit one observation at a time; files are append-only observations, not snapshots."""
    for path in sorted((root / 'data/inbox').glob('*.jsonl')):
        ensure_active(root)
        try:
            before = path.stat()
            index = (
                Seen(
                    db.execute("""SELECT * FROM jobs WHERE status='open' OR id IN
                (SELECT job_id FROM applications WHERE status!='interested')""")
                )
                if db is not None
                else Seen()
            )
            with path.open('rb') as incoming:
                for number, line in enumerate(incoming, 1):
                    ensure_active(root)
                    try:
                        job = parse_observation(json.loads(line), rules)
                    except (ValueError, KeyError, UnicodeError) as exc:
                        warning = f'{path.name}:{number}: invalid feed line: {exc}'
                        summary['warnings'].append(warning)
                        print(warning, flush=True)
                        continue
                    summary['jobs_seen'] += 1
                    summary['role_family'][job['role_family']] += 1
                    old = (
                        db.execute(
                            """SELECT jobs.* FROM inbox_aliases JOIN jobs ON jobs.id=inbox_aliases.job_id
                        WHERE inbox_aliases.source=? AND external_id=?""",
                            (job['source'], job['requisition_id']),
                        ).fetchone()
                        if db is not None
                        else None
                    )
                    fields = (
                        job['url'],
                        job['company'],
                        job['title'],
                        job['location_normalized'],
                        term_key(job),
                    )
                    if db is None:
                        if index.has(*fields):
                            summary['duplicates'] += 1
                        index.add(*fields)
                        continue
                    if old is None:
                        matched_id = next(index.matches(*fields), None)
                        if matched_id is not None:
                            old = db.execute('SELECT * FROM jobs WHERE id=?', (matched_id,)).fetchone()
                    own_row = old is not None and (old['source'], old['requisition_id']) == (
                        job['source'],
                        job['requisition_id'],
                    )
                    newer = old is None or timestamp(job['last_seen']) > timestamp(old['last_seen'])
                    if old:
                        # Date evidence is independent of which observation owns the
                        # newest descriptive metadata. Null dates never erase evidence.
                        job['first_seen'] = min(timestamp(old['first_seen']), job['first_seen'])
                        dates = [timestamp(value) for value in (old['posted_at'], job['posted_at']) if value]
                        job['posted_at'] = min(dates) if dates else None
                        if (
                            own_row
                            and newer
                            and old['posting_description']
                            and canonical_url(old['url']) == canonical_url(job['url'])
                        ):
                            job['description_text'] += ' ' + old['posting_description']
                            job['pay_hourly_usd'] = old['pay_hourly_usd']
                            job.update(
                                classify(job['title'], job['description_text'], rules, job['location_raw'])
                            )
                        elif own_row and newer and canonical_url(old['url']) != canonical_url(job['url']):
                            job['posting_description'] = None
                    ensure_active(root)
                    with db:
                        if old is None or (own_row and newer):
                            db.execute(
                                """INSERT INTO sources(source,company,token,last_success_at,last_attempt_at)
                                VALUES(?,?,?,?,?) ON CONFLICT(source,company) DO UPDATE SET
                                last_success_at=excluded.last_success_at,last_attempt_at=excluded.last_attempt_at""",
                                (
                                    job['source'],
                                    job['company'],
                                    job['company'],
                                    job['last_seen'],
                                    job['last_seen'],
                                ),
                            )
                            if old is None:
                                columns = list(job)
                                job_id = db.execute(
                                    f"""INSERT INTO jobs({','.join(columns)})
                                    VALUES({','.join('?' for _ in columns)})""",
                                    list(job.values()),
                                ).lastrowid
                            else:
                                job_id = old['id']
                                columns = [c for c in job if c not in ('source', 'requisition_id')]
                                db.execute(
                                    'UPDATE jobs SET ' + ','.join(f'{c}=?' for c in columns) + ' WHERE id=?',
                                    [*(job[c] for c in columns), job_id],
                                )
                        else:
                            job_id = old['id']
                            db.execute(
                                """UPDATE jobs SET first_seen=?,posted_at=? WHERE id=?
                                AND (first_seen IS NOT ? OR posted_at IS NOT ?)""",
                                (
                                    job['first_seen'],
                                    job['posted_at'],
                                    job_id,
                                    job['first_seen'],
                                    job['posted_at'],
                                ),
                            )
                        # Commit alias and job together before archiving, including when
                        # this observation only identifies a job held by another source.
                        db.execute(
                            """INSERT INTO inbox_aliases(source,external_id,job_id) VALUES(?,?,?)
                            ON CONFLICT(source,external_id) DO NOTHING""",
                            (job['source'], job['requisition_id'], job_id),
                        )
                    if old is None or (own_row and newer):
                        index.add(*fields, job_id)
                    summary['new_jobs'] += int(old is None)
                    summary['duplicates'] += int(old is not None and not own_row)
            ensure_active(root)
            if db is not None:
                with db:
                    refresh_duplicates(db)
                    audit(db, 'feed_import', 'inbox', json.dumps({'file': path.name}))
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError('file changed during import; retained for replay')
                processed = path.parent / 'processed'
                processed.mkdir(mode=0o700, exist_ok=True)
                destination = processed / path.name
                if destination.exists():
                    destination = processed / f'{path.stem}-{uuid.uuid4().hex}.jsonl'
                path.rename(destination)
            summary['companies_ok'] += 1
            print(f'feed {path.name}: processed' + (' (dry-run)' if db is None else ''), flush=True)
        except (OSError, ValueError, sqlite3.Error) as exc:
            error = f'feed {path.name}: {type(exc).__name__}: {exc}'
            summary['failures'].append(error)
            print(error, flush=True)
