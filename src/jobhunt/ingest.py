import fcntl
import json
import os
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from http.client import IncompleteRead
from pathlib import Path

from .classify import classify
from .db import audit, connect
from .feeds import parse
from .fetch import Fetcher
from .inbox import import_inbox
from .lists import LIST_ROW, Seen, parse_list, refresh_duplicates, resolve_url, term_key
from .postings import hourly_pay
from .settings import config, ensure_active, filters, list_registry, registry


def now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def runner_lock(database: Path):
    database = database.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(database) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(descriptor)


def ingest(root: Path, *, dry_run=False, source=None, fetcher=None, accept_drop=None) -> dict:
    ensure_active(root)
    settings = config(root)
    database = (root / settings['database']).resolve()
    with nullcontext(True) if dry_run else runner_lock(database) as acquired:
        if not acquired:
            print('ingest skipped: another runner holds the exclusive lock')
            return {'skipped': 'overlapping run'}
        ensure_active(root)
        settings = config(root)
        targets = [dict(kind='board', **c) for c in registry(root, source)] + [
            dict(kind='list', source=entry['source'], name=LIST_ROW, token=LIST_ROW)
            for entry in list_registry(root, source)
        ]
        if source in (None, 'feed'):
            targets.insert(0, dict(kind='feed'))
        if not targets:
            raise ValueError('no companies selected')
        rules = filters(root)
        fetcher = fetcher or Fetcher(settings)
        db = None if dry_run else connect(database)
        runner = str(uuid.uuid4())
        summary = {
            'dry_run': dry_run,
            'companies_ok': 0,
            'failures': [],
            'jobs_seen': 0,
            'new_jobs': 0,
            'duplicates': 0,
            'closed_jobs': 0,
            'warnings': [],
            'role_family': Counter(),
        }
        try:
            if db is not None:
                ensure_active(root)
                with db:
                    audit(db, 'ingest_start', 'runner', json.dumps({'runner_id': runner}))
            for target in targets:
                ensure_active(root)
                if target['kind'] == 'feed':
                    import_inbox(root, db, rules, summary)
                    continue
                is_list = target['kind'] == 'list'
                source_name, name, token = target['source'], target['name'], target['token']
                label = f'list {source_name}' if is_list else name
                attempted = now()
                try:
                    payload = fetcher.get(source_name, token)
                    jobs = parse_list(source_name, payload) if is_list else parse(source_name, payload)
                    for job in jobs:
                        job.update(
                            classify(job['title'], job['description_text'], rules, job['location_raw'])
                        )
                        job.setdefault('company', name)
                        job.update(
                            source=source_name, first_seen=attempted, last_seen=attempted, status='open'
                        )
                    ensure_active(root)
                    if db is not None:
                        # Resolve before the atomic snapshot: network waits must not hold a
                        # SQLite write transaction or discard already-completed cache entries.
                        for position, job in enumerate(jobs, 1):
                            ensure_active(root)
                            if source_name == 'vanshb03' and position % 100 == 0:
                                print(
                                    f'list {source_name}: resolving URLs {position}/{len(jobs)}', flush=True
                                )
                            with db:
                                job['resolved_url'] = resolve_url(db, fetcher, job['url'], attempted)
                        with db:
                            db.execute(
                                """INSERT INTO sources(
                                    source,company,token,last_attempt_at,last_success_at,last_error,job_count)
                                VALUES(?,?,?,?,?,NULL,?) ON CONFLICT(source,company) DO UPDATE SET
                                token=excluded.token,
                                last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,
                                last_error=NULL,job_count=excluded.job_count""",
                                (source_name, name, token, attempted, now(), len(jobs)),
                            )
                            # A board is scoped by company; a list's jobs span many companies.
                            scope, scope_values = (
                                ('source=?', (source_name,))
                                if is_list
                                else ('source=? AND company=?', (source_name, name))
                            )
                            current = {
                                r['requisition_id']: (r['status'], r['company'])
                                for r in db.execute(
                                    'SELECT requisition_id,status,company FROM jobs WHERE ' + scope,
                                    scope_values,
                                )
                            }
                            incoming = {job['requisition_id'] for job in jobs}
                            open_ids = {key for key, (status, _) in current.items() if status == 'open'}
                            missing_ids = open_ids - incoming
                            drop = json.dumps(sorted(missing_ids))
                            previous = db.execute(
                                'SELECT pending_drop FROM sources WHERE source=? AND company=?',
                                (source_name, name),
                            ).fetchone()[0]
                            # Any nonzero-to-zero snapshot is held, however small the board; larger
                            # boards also hold when more than half the open jobs vanish at once.
                            suspicious = bool(missing_ids) and (
                                not incoming or (len(open_ids) >= 5 and len(missing_ids) > len(open_ids) / 2)
                            )
                            accepted = accept_drop in (source_name, token, name, source_name + '/' + token)
                            hold = suspicious and previous != drop and not accepted
                            warning = (
                                (
                                    f'{label} ({source_name}/{token}): holding closure of '
                                    f'{len(missing_ids)}/{len(open_ids)} open jobs; '
                                    'repeat snapshot or use ingest --accept-drop ' + source_name + '/' + token
                                )
                                if hold
                                else None
                            )
                            db.execute(
                                'UPDATE sources SET pending_drop=?,warning=? WHERE source=? AND company=?',
                                (drop if hold else None, warning, source_name, name),
                            )
                            if hold:
                                summary['warnings'].append(warning)
                                audit(
                                    db,
                                    'source_warning',
                                    'sources',
                                    json.dumps({'source': source_name, 'company': name, 'warning': warning}),
                                )
                                print(f'source warning: {warning}', flush=True)
                            elif suspicious:
                                audit(
                                    db,
                                    'source_drop_accepted',
                                    'sources',
                                    json.dumps(
                                        {
                                            'source': source_name,
                                            'company': name,
                                            'explicit': accepted,
                                            'closed': len(missing_ids),
                                        }
                                    ),
                                )
                            for job in jobs:
                                old = db.execute(
                                    'SELECT * FROM jobs WHERE source=? AND company=? AND requisition_id=?',
                                    (source_name, job['company'], job['requisition_id']),
                                ).fetchone()
                                if is_list:
                                    job['listing_description'] = job['description_text']
                                    if (
                                        old
                                        and old['posting_description'] is not None
                                        and old['url'] == job['url']
                                    ):
                                        job['description_text'] += ' ' + old['posting_description']
                                        job['pay_hourly_usd'] = old['pay_hourly_usd']
                                    elif old and old['url'] != job['url']:
                                        job['posting_description'] = None
                                job.setdefault('pay_hourly_usd', hourly_pay(job['description_text']))
                                job.update(
                                    classify(
                                        job['title'], job['description_text'], rules, job['location_raw']
                                    )
                                )
                            written = jobs
                            if is_list:
                                # Drop postings already held from boards/other lists (same URL, or same
                                # company+title+location). The existing row keeps its earlier first_seen.
                                index = Seen(
                                    db.execute(
                                        "SELECT * FROM jobs WHERE source!=? AND status='open'", (source_name,)
                                    )
                                )
                                written = []
                                for job in jobs:
                                    fields = (
                                        job['resolved_url'],
                                        job['company'],
                                        job['title'],
                                        job['location_normalized'],
                                        term_key(job),
                                    )
                                    if job['requisition_id'] in current or not index.has(*fields):
                                        index.add(*fields)
                                        written.append(job)
                                summary['duplicates'] += len(jobs) - len(written)
                                for company in {job['company'] for job in written}:
                                    db.execute(
                                        'INSERT OR IGNORE INTO sources(source,company,token) VALUES(?,?,?)',
                                        (source_name, company, company),
                                    )
                            for job in written:
                                columns = list(job)
                                assignments = ','.join(
                                    f'{c}=excluded.{c}'
                                    for c in columns
                                    if c not in ('source', 'company', 'requisition_id', 'first_seen')
                                )
                                db.execute(
                                    f"""INSERT INTO jobs({','.join(columns)})
                                    VALUES({','.join('?' for _ in columns)})
                                    ON CONFLICT(source,company,requisition_id) DO UPDATE SET {assignments}""",
                                    list(job.values()),
                                )
                            for missing in current.keys() - incoming:
                                if current[missing][0] == 'open' and not hold:
                                    db.execute(
                                        "UPDATE jobs SET status='closed' "
                                        'WHERE source=? AND company=? AND requisition_id=?',
                                        (source_name, current[missing][1], missing),
                                    )
                                    summary['closed_jobs'] += 1
                            refresh_duplicates(db)
                            summary['new_jobs'] += len(
                                {job['requisition_id'] for job in written} - current.keys()
                            )
                except (
                    ValueError,
                    OSError,
                    TypeError,
                    KeyError,
                    AttributeError,
                    sqlite3.ProgrammingError,
                    IncompleteRead,
                ) as exc:
                    error = f'{label} ({source_name}/{token}): {type(exc).__name__}: {exc}'
                    summary['failures'].append(error)
                    print('feed failed: ' + error, flush=True)
                    if db is not None:
                        ensure_active(root)
                        with db:
                            db.execute(
                                """INSERT INTO sources(source,company,token,last_attempt_at,last_error)
                                VALUES(?,?,?,?,?) ON CONFLICT(source,company) DO UPDATE SET
                                last_attempt_at=excluded.last_attempt_at,last_error=excluded.last_error,pending_drop=NULL""",
                                (source_name, name, token, attempted, error),
                            )
                    continue
                summary['companies_ok'] += 1
                summary['jobs_seen'] += len(jobs)
                for job in jobs:
                    summary['role_family'][job['role_family']] += 1
                print(f'{label}: {len(jobs)} jobs{" (dry-run)" if dry_run else ""}', flush=True)
            if db is not None:
                ensure_active(root)
                with db:
                    # Health is global: sources not touched by this run may still have
                    # persisted errors or held drops from an earlier one.
                    registered = {(c['source'], c['name']) for c in registry(root)} | {
                        (entry['source'], LIST_ROW) for entry in list_registry(root)
                    }
                    unresolved = [
                        r
                        for r in db.execute(
                            'SELECT source,company FROM sources '
                            'WHERE last_error IS NOT NULL OR warning IS NOT NULL'
                        )
                        if (r[0], r[1]) in registered
                    ]
                    if not summary['failures'] and not summary['warnings'] and not unresolved:
                        db.execute(
                            'INSERT INTO heartbeat VALUES(?,?) ON CONFLICT(runner_id) '
                            'DO UPDATE SET last_ok_at=excluded.last_ok_at',
                            ('ingest', now()),
                        )
                    audit(db, 'ingest_complete', 'runner', json.dumps(summary))
            return summary
        finally:
            if db is not None:
                db.close()
