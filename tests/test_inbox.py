import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FixtureFetcher, db_for

from jobhunt.cli import execute, parser
from jobhunt.ingest import ingest
from jobhunt.queue import queue
from jobhunt.settings import Paused
from jobhunt.workflow import mark


def observation(**changes):
    row = dict(
        source='handshake',
        external_id='h1',
        company='Feed Company',
        title='Software Intern Summer 2027',
        location='New York, NY',
        url='https://example.invalid/feed/1',
        posted_at=datetime.now(UTC).isoformat(),
        pay_text='$60 per hour',
        description='<p>Build <b>software</b>.</p>',
        seen_at=datetime.now(UTC).isoformat(),
    )
    return {**row, **changes}


def write_feed(project, rows, name='handshake.jsonl'):
    inbox = project / 'data/inbox'
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


class NoNetwork:
    def get(self, *args):
        raise AssertionError('feed import must not fetch URLs')


def test_feed_normalization_queue_pay_archive_and_append_only_updates(project):
    row = observation()
    path = write_feed(project, [row])
    result = ingest(project, source='feed', fetcher=NoNetwork())
    assert result['new_jobs'] == 1 and not result['failures']
    assert not path.exists() and (path.parent / 'processed' / path.name).exists()
    with db_for(project) as db:
        entry = queue(db)[0]
        assert entry['source'] == 'handshake' and entry['fresh']
        assert entry['location_normalized'] == 'NYC metro' and entry['pay_hourly_usd'] == 60
        assert 'Build software' in entry['description_text'] and '<p>' not in entry['description_text']
        first_seen = entry['first_seen']
    # Same filename may be reused without overwriting the archived original.
    later = (datetime.fromisoformat(row['seen_at']) + timedelta(hours=1)).isoformat()
    write_feed(project, [{**row, 'seen_at': later, 'pay_text': '$70/hr'}])
    assert ingest(project, source='feed', fetcher=NoNetwork())['new_jobs'] == 0
    assert len(list((path.parent / 'processed').glob('*.jsonl'))) == 2
    with db_for(project) as db:
        assert queue(db)[0]['pay_hourly_usd'] == 70
        assert queue(db)[0]['first_seen'] == first_seen
    write_feed(
        project,
        [
            observation(
                source='linkedin',
                external_id='l2',
                company='Other Company',
                url='https://example.invalid/feed/2',
                posted_at=None,
            )
        ],
    )
    assert ingest(project, source='feed', fetcher=NoNetwork())['new_jobs'] == 1
    with db_for(project) as db:
        assert len(queue(db)) == 2  # absent observations never close previous jobs
        assert db.execute("SELECT count(*) FROM jobs WHERE status='closed'").fetchone()[0] == 0
    mark(project, 1, 'applied')
    with db_for(project) as db:
        assert [r['source'] for r in queue(db)] == ['linkedin']


def test_feed_dedupes_boards_other_feeds_and_processed_identities(project):
    ingest(project, fetcher=FixtureFetcher())
    mark(project, 1, 'applied')
    write_feed(
        project,
        [
            observation(url='https://example.invalid/jobs/1?utm_source=handshake'),
            observation(external_id='unique'),
            observation(source='linkedin', external_id='same-identity', url='https://different.invalid/job'),
        ],
    )
    result = ingest(project, source='feed', fetcher=NoNetwork())
    assert result['duplicates'] == 2 and result['new_jobs'] == 1
    with db_for(project) as db:
        assert [r['requisition_id'] for r in queue(db)] == ['unique']


def test_invalid_feed_lines_are_reported_skipped_and_archived(project, capsys):
    changes = [
        dict(source='unsupported'),
        dict(external_id=None),
        dict(company=''),
        dict(url='javascript:alert(1)'),
        dict(posted_at='last Tuesday'),
        dict(description=12),
        dict(seen_at=None),
    ]
    path = write_feed(
        project, [observation(**change) for change in changes] + [observation(external_id='valid')]
    )
    with path.open('ab') as stream:
        stream.write(b'{bad json\n\xff\n')
    result = ingest(project, source='feed', fetcher=NoNetwork())
    assert result['new_jobs'] == 1 and not result['failures'] and len(result['warnings']) == len(changes) + 2
    output = capsys.readouterr().out
    assert 'handshake.jsonl:1: invalid feed line' in output
    assert not path.exists()
    with db_for(project) as db:
        assert queue(db)[0]['requisition_id'] == 'valid'


def test_feed_dry_run_is_read_only_and_pause_preserves_inbox(project):
    path = write_feed(project, [observation()])
    before = sorted(project.rglob('*'))
    assert ingest(project, source='feed', dry_run=True, fetcher=NoNetwork())['jobs_seen'] == 1
    assert sorted(project.rglob('*')) == before and path.exists()
    (project / 'PAUSE').touch()
    with pytest.raises(Paused):
        ingest(project, source='feed', fetcher=NoNetwork())
    assert path.exists()


def test_feed_cli_and_replay_preserve_newer_metadata_and_marks(project, capsys):
    row = observation()
    write_feed(project, [row])
    assert execute(parser().parse_args(['--root', str(project), 'ingest', '--source', 'feed'])) == 0
    mark(project, 1, 'applying')
    write_feed(project, [{**row, 'description': 'Wrong stale text'}])
    assert ingest(project, source='feed', fetcher=NoNetwork())['new_jobs'] == 0
    with db_for(project) as db:
        assert 'Wrong stale text' not in db.execute('SELECT description_text FROM jobs').fetchone()[0]
        assert queue(db) == []
        assert db.execute('SELECT status FROM applications').fetchone()[0] == 'applying'
    capsys.readouterr()


@pytest.mark.parametrize('closed', [False, True])
def test_discarded_feed_alias_stays_linked_to_applied_job_after_metadata_changes(project, closed):
    ingest(project, fetcher=FixtureFetcher())
    mark(project, 1, 'applied')
    if closed:
        with db_for(project) as db:
            db.execute("UPDATE jobs SET status='closed' WHERE id=1")
    original = observation(url='https://example.invalid/jobs/1?utm_source=handshake')
    write_feed(project, [original])
    first = ingest(project, source='feed', fetcher=NoNetwork())
    assert first['duplicates'] == 1 and first['new_jobs'] == 0
    changed = {
        **original,
        'url': 'https://different.invalid/changed',
        'company': 'Renamed Company',
        'title': 'Software Intern Summer 2027 Other Team',
        'location': 'Boston, MA',
        'seen_at': (datetime.fromisoformat(original['seen_at']) + timedelta(hours=1)).isoformat(),
    }
    write_feed(project, [changed])
    second = ingest(project, source='feed', fetcher=NoNetwork())
    assert not second['failures'] and second['new_jobs'] == 0 and second['duplicates'] == 1
    with db_for(project) as db:
        assert queue(db) == []
        assert db.execute('SELECT status FROM applications WHERE job_id=1').fetchone()[0] == 'applied'


def test_feed_external_id_is_scoped_by_source_but_not_company(project):
    row = observation()
    write_feed(project, [row])
    ingest(project, source='feed', fetcher=NoNetwork())
    mark(project, 1, 'applied')
    later = (datetime.fromisoformat(row['seen_at']) + timedelta(hours=1)).isoformat()
    changed = {
        **row,
        'company': 'Renamed Company',
        'title': 'Software Intern Summer 2027 Other Team',
        'location': 'Boston, MA',
        'url': 'https://different.invalid/changed',
        'seen_at': later,
    }
    write_feed(
        project,
        [
            changed,
            {
                **changed,
                'source': 'linkedin',
                'url': 'https://other.invalid/job',
                'title': 'Software Intern Summer 2027 Third Team',
            },
        ],
    )
    result = ingest(project, source='feed', fetcher=NoNetwork())
    assert not result['failures'] and result['new_jobs'] == 1
    with db_for(project) as db:
        assert [r['source'] for r in queue(db)] == ['linkedin']
        assert db.execute('SELECT company FROM jobs WHERE id=1').fetchone()[0] == 'Renamed Company'


def test_undated_feed_update_keeps_known_posting_date_and_queue_age(project):
    row = observation(posted_at='2026-10-01', seen_at='2026-11-06T10:00:00Z')
    write_feed(project, [row])
    ingest(project, source='feed', fetcher=NoNetwork())
    write_feed(
        project,
        [
            {
                **row,
                'posted_at': None,
                'seen_at': '2026-11-06T11:00:00Z',
                'title': 'Software Intern Summer 2027 Updated Team',
            }
        ],
    )
    ingest(project, source='feed', fetcher=NoNetwork())
    with db_for(project) as db:
        entry = queue(db, at=datetime(2026, 11, 6, 12, tzinfo=UTC))[0]
        assert entry['posted_at'] == '2026-10-01T00:00:00+00:00'
        assert not entry['fresh'] and entry['age_days'] > 30
        assert entry['title'].endswith('Updated Team')


@pytest.mark.parametrize('posted_at', [None, '2026-09-01'])
def test_older_feed_observation_merges_earliest_dates_without_replacing_newer_metadata(project, posted_at):
    newer = observation(posted_at=None, seen_at='2026-11-06T10:00:00Z')
    write_feed(project, [newer])
    ingest(project, source='feed', fetcher=NoNetwork())
    mark(project, 1, 'interested', 'keep this note')
    older = {
        **newer,
        'seen_at': '2026-10-01T10:00:00Z',
        'posted_at': posted_at,
        'description': 'Stale description',
        'pay_text': '$5/hour',
        'title': 'Software Intern Summer 2027 Stale Team',
    }
    write_feed(project, [older])
    result = ingest(project, source='feed', fetcher=NoNetwork())
    assert not result['failures'] and result['new_jobs'] == 0
    with db_for(project) as db:
        entry = queue(db, at=datetime(2026, 11, 6, 12, tzinfo=UTC))[0]
        assert not entry['fresh'] and entry['age_days'] > 30
        assert entry['first_seen'] == '2026-10-01T10:00:00+00:00'
        assert entry['last_seen'] == '2026-11-06T10:00:00+00:00'
        assert entry['posted_at'] == ('2026-09-01T00:00:00+00:00' if posted_at else None)
        assert entry['title'] == newer['title'] and entry['pay_hourly_usd'] == 60
        assert 'Stale description' not in entry['description_text']
        assert db.execute('SELECT notes FROM applications WHERE job_id=1').fetchone()[0] == 'keep this note'
    # Replay cannot make the merged dates newer again.
    write_feed(project, [newer])
    ingest(project, source='feed', fetcher=NoNetwork())
    with db_for(project) as db:
        assert db.execute('SELECT first_seen FROM jobs').fetchone()[0] == '2026-10-01T10:00:00+00:00'
