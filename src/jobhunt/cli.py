import argparse
import csv
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .db import audit, backup, connect
from .enrich import enrich
from .ingest import ingest
from .queue import queue, queue_sections
from .settings import Paused, config
from .shortlist import shortlist, since_time
from .workflow import APPLICATION_STATES, answer, apply_pack, digest, mark


def parser():
    result = argparse.ArgumentParser(prog='jobhunt')
    result.add_argument('--root', type=Path, default=Path.cwd(), help='directory containing config.yaml')
    commands = result.add_subparsers(dest='command', required=True)
    fetch = commands.add_parser('ingest')
    fetch.add_argument('--dry-run', action='store_true')
    fetch.add_argument(
        '--accept-drop', help='source, board token, or source/token whose drop is explicitly accepted'
    )
    fetch.add_argument(
        '--source', choices=['greenhouse', 'lever', 'ashby', 'simplify', 'vanshb03', 'speedyapply', 'feed']
    )
    enrichment = commands.add_parser('enrich')
    enrichment.add_argument('--limit', type=int, default=80)
    ranked = commands.add_parser('queue')
    ranked.add_argument('--limit', type=int, default=25)
    ranked.add_argument('--include-accounts', action='store_true', help='include sites requiring an account')
    for command in ('list', 'export-csv'):
        show = commands.add_parser(command)
        show.add_argument('--status', choices=['open', 'closed'])
        show.add_argument('--include-non-us', action='store_true')
        show.add_argument('--role-family')
        show.add_argument('--since', help='ISO date/time; filters last_seen, not posting date')
        show.add_argument('--matches-target', action='store_true')
    short = commands.add_parser('shortlist')
    short.add_argument('--limit', type=int, default=25)
    short.add_argument('--since', help='duration such as 7d, or ISO date; filters first_seen')
    track = commands.add_parser('mark')
    track.add_argument('id', type=int)
    track.add_argument('status', choices=APPLICATION_STATES)
    track.add_argument('--note')
    pack = commands.add_parser('apply-pack')
    pack.add_argument('id', type=int)
    single_answer = commands.add_parser('answer', help='print one answers.yaml text value')
    single_answer.add_argument('key', help='dotted key, for example contact.phone')
    daily = commands.add_parser('digest')
    daily.add_argument('--since', default='24h')
    daily.add_argument('--include-accounts', action='store_true', help='include sites requiring an account')
    daily.add_argument('--out', type=Path, nargs='?', const=Path('digest.md'))
    copy = commands.add_parser('backup')
    copy.add_argument('--out', type=Path)
    detail = commands.add_parser('show')
    detail.add_argument('id', type=int)
    for command in ('status', 'pause', 'resume', 'heartbeat-check'):
        commands.add_parser(command)
    return result


def query(db, args):
    conditions, values = [], []
    for option in ('status', 'role_family'):
        if value := getattr(args, option):
            conditions.append(f'{option}=?')
            values.append(value)
    if not args.include_non_us:
        conditions.append("country!='non-US'")
    if args.since:
        conditions.append('last_seen>=?')
        values.append(since_time(args.since))
    if args.matches_target:
        conditions.append('matches_target=1')
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    return db.execute('SELECT * FROM jobs' + where + ' ORDER BY company,title,id', values)


def csv_value(value):
    return (
        "'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value
    )


def print_shortlist(rows):
    keys = ('id', 'company', 'title', 'location_raw', 'season_evidence', 'restriction_note', 'url')
    writer = csv.writer(sys.stdout)
    writer.writerow(('id', 'company', 'title', 'location', *keys[4:]))
    writer.writerows([csv_value(row[key]) for key in keys] for row in rows)


def toggle_pause(root, pause):
    settings = config(root)
    # These two safety-control commands must be able to run while paused.
    # Write the safety flag first: a DB error leaves pause effective.
    settings['paused'] = pause
    temp = root / 'config.yaml.tmp'
    temp.write_text(json.dumps(settings, indent=2) + '\n')
    temp.replace(root / 'config.yaml')
    if not pause and (root / 'PAUSE').exists():
        (root / 'PAUSE').unlink()
    with connect(root / settings['database']) as db:
        audit(db, 'pause' if pause else 'resume', 'config')
    print('paused' if pause else 'resumed')


def execute(args):
    root = args.root.resolve()
    if args.command == 'answer':
        print(answer(root, args.key))
        return 0
    settings = config(root)
    if args.command == 'ingest':
        result = ingest(root, dry_run=args.dry_run, source=args.source, accept_drop=args.accept_drop)
        print(json.dumps(result, indent=2))
        return 1 if result.get('failures') else 0
    if args.command == 'enrich':
        result = enrich(root, limit=args.limit)
        print(json.dumps(result, indent=2))
        return 1 if result.get('failures') else 0
    if args.command in ('pause', 'resume'):
        toggle_pause(root, args.command == 'pause')
        return 0
    path = root / settings['database']
    if args.command == 'mark':
        mark(root, args.id, args.status, args.note)
        print(f'job {args.id}: {args.status}')
        return 0
    if args.command == 'apply-pack':
        print(apply_pack(root, args.id))
        return 0
    if not path.exists():
        if args.command == 'status':
            print(
                json.dumps(
                    {
                        'jobs': 0,
                        'last_heartbeat': None,
                        'paused': settings['paused'] or (root / 'PAUSE').exists(),
                    }
                )
            )
            return 0
        raise ValueError('database missing: run ingest first')
    db = connect(path, readonly=True)
    try:
        if args.command == 'digest':
            result = digest(db, root, args.since, include_accounts=args.include_accounts)
            print(result, end='')
            if args.out:
                args.out.write_text(result)
        elif args.command == 'backup':
            out = args.out or path.with_name(
                path.name + '.' + datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ') + '.bak'
            )
            print(backup(db, out))
        elif args.command == 'status':
            counts = [
                dict(row)
                for row in db.execute(
                    'SELECT status,country,role_family,count(*) AS count FROM jobs GROUP BY 1,2,3'
                )
            ]
            heartbeat = db.execute('SELECT * FROM heartbeat ORDER BY last_ok_at DESC LIMIT 1').fetchone()
            print(
                json.dumps(
                    {
                        'counts': counts,
                        'last_heartbeat': dict(heartbeat) if heartbeat else None,
                        'paused': settings['paused'] or (root / 'PAUSE').exists(),
                        'warnings': [
                            r[0] for r in db.execute('SELECT warning FROM sources WHERE warning IS NOT NULL')
                        ],
                        'wal_bytes': Path(str(path) + '-wal').stat().st_size
                        if Path(str(path) + '-wal').exists()
                        else 0,
                        'sources': [
                            dict(r)
                            for r in db.execute('SELECT * FROM sources WHERE last_attempt_at IS NOT NULL')
                        ],
                    },
                    indent=2,
                )
            )
        elif args.command == 'heartbeat-check':
            row = db.execute('SELECT max(last_ok_at) FROM heartbeat').fetchone()
            if not row[0] or datetime.now(UTC) - datetime.fromisoformat(row[0]) > timedelta(hours=26):
                print('heartbeat missing or older than 26h', file=sys.stderr)
                return 1
            print('heartbeat fresh')
        elif args.command == 'queue':
            print('id | company | title | location | age | pay USD | resolved URL | ATS | needs account')
            print(
                '\n'.join(
                    queue_sections(
                        queue(
                            db,
                            args.limit,
                            include_accounts=args.include_accounts,
                            preferred=settings.get('preferred_locations', []),
                        )
                    )
                )
            )
        elif args.command == 'shortlist':
            print_shortlist(
                shortlist(db, args.limit, args.since, preferred=settings.get('preferred_locations', []))
            )
        elif args.command == 'show':
            row = db.execute('SELECT * FROM jobs WHERE id=?', (args.id,)).fetchone()
            if not row:
                raise ValueError('job ID not found')
            print(json.dumps(dict(row), indent=2))
        else:
            rows = query(db, args)
            if args.command == 'export-csv':
                writer = csv.writer(sys.stdout)
                writer.writerow([column[0] for column in rows.description])
                for row in rows:
                    # Job text is untrusted, including spreadsheet formula prefixes.
                    writer.writerow(csv_value(value) for value in row)
            else:
                for row in rows:
                    print(
                        json.dumps(
                            {
                                key: row[key]
                                for key in (
                                    'id',
                                    'company',
                                    'title',
                                    'location_normalized',
                                    'status',
                                    'restriction_note',
                                    'country',
                                    'role_family',
                                    'matches_target',
                                )
                            }
                        )
                    )
    finally:
        db.close()
    return 0


def main():
    try:
        return execute(parser().parse_args())
    except (Paused, ValueError, OSError, sqlite3.Error) as exc:
        print(f'jobhunt: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
