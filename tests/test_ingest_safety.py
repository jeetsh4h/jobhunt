import json
import multiprocessing
import os
import signal
from pathlib import Path

import pytest
from conftest import FixtureFetcher, db_for, job

from jobhunt.ingest import ingest


@pytest.mark.parametrize('size', [1, 6])
def test_empty_snapshot_quarantined_then_confirmed_and_status_warns(project, capsys, size):
    ingest(project, fetcher=FixtureFetcher([job(i) for i in range(size)]))
    with db_for(project) as db:
        heartbeat = db.execute('SELECT last_ok_at FROM heartbeat').fetchone()[0]
    first = ingest(project, fetcher=FixtureFetcher([]))
    assert first['warnings'] and first['closed_jobs'] == 0
    with db_for(project) as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE status='open'").fetchone()[0] == size
        assert db.execute('SELECT last_ok_at FROM heartbeat').fetchone()[0] == heartbeat
    from jobhunt.cli import execute, parser

    execute(parser().parse_args(['--root', str(project), 'status']))
    assert 'holding closure' in capsys.readouterr().out
    assert ingest(project, fetcher=FixtureFetcher([]))['closed_jobs'] == size


def test_large_drop_requires_same_snapshot_or_explicit_acceptance(project):
    ingest(project, fetcher=FixtureFetcher([job(i) for i in range(6)]))
    assert ingest(project, fetcher=FixtureFetcher([job(1)]))['closed_jobs'] == 0
    assert ingest(project, fetcher=FixtureFetcher([job(2)]))['closed_jobs'] == 0
    assert (
        ingest(project, fetcher=FixtureFetcher([job(3)]), accept_drop='greenhouse/example')['closed_jobs']
        == 5
    )


def test_error_envelope_breaks_consecutive_drop_confirmation(project):
    ingest(project, fetcher=FixtureFetcher([job(i) for i in range(6)]))
    ingest(project, fetcher=FixtureFetcher([]))

    class Envelope:
        def get(self, source, token):
            return {'jobs': [], 'error': 'temporarily unavailable'}

    assert ingest(project, fetcher=Envelope())['failures']
    assert ingest(project, fetcher=FixtureFetcher([]))['closed_jobs'] == 0


def _blocked_ingest(root, pipe):
    class Blocked(FixtureFetcher):
        def get(self, source, token):
            pipe.send('fetching')
            pipe.recv()
            return super().get(source, token)

    ingest(Path(root), fetcher=Blocked())


def test_shared_canonical_database_lock_and_sigkill_recovery(project, tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    for name in ('filters.yaml', 'companies.toml'):
        (other / name).write_bytes((project / name).read_bytes())
    settings = json.loads((project / 'config.yaml').read_text())
    settings['database'] = str(project / 'data/jobhunt.sqlite3')
    (other / 'config.yaml').write_text(json.dumps(settings))
    context = multiprocessing.get_context('spawn')
    parent, child_pipe = context.Pipe()
    child = context.Process(target=_blocked_ingest, args=(str(project), child_pipe))
    child.start()
    try:
        assert parent.poll(10) and parent.recv() == 'fetching'
        assert ingest(other, fetcher=FixtureFetcher()) == {'skipped': 'overlapping run'}
        alias = tmp_path / 'database-alias'
        alias.symlink_to(project / 'data/jobhunt.sqlite3')
        settings['database'] = str(alias)
        (other / 'config.yaml').write_text(json.dumps(settings))
        assert ingest(other, fetcher=FixtureFetcher())['skipped'] == 'overlapping run'
    finally:
        assert child.pid is not None
        os.kill(child.pid, signal.SIGKILL)
        child.join(10)
    assert child.exitcode == -signal.SIGKILL
    assert ingest(other, fetcher=FixtureFetcher())['companies_ok'] == 1


@pytest.mark.parametrize(
    'payload',
    [
        {'jobs': [], 'meta': None},
        {'jobs': [job(absolute_url={'bad': 'url'})]},
        {'jobs': [job(location=None)]},
        {'jobs': [job(updated_at={'bad': 'date'})]},
        {'jobs': [job(id=['bad'])]},
    ],
)
def test_malformed_feed_records_error_and_continues_next_company(project, payload):
    with (project / 'companies.toml').open('a') as file:
        file.write('\n[[companies]]\nname="Next"\nsource="greenhouse"\ntoken="next"\n')

    class Feeds:
        def get(self, source, token):
            return payload if token == 'example' else {'jobs': [job(9)]}

    result = ingest(project, fetcher=Feeds())
    assert len(result['failures']) == 1 and result['companies_ok'] == 1
    with db_for(project) as db:
        assert db.execute("SELECT last_error FROM sources WHERE company='Example'").fetchone()[0]
        assert db.execute('SELECT company FROM jobs').fetchone()[0] == 'Next'


def test_timestamp_overflow_is_a_source_error_and_next_board_continues(project):
    (project / 'companies.toml').write_text("""[[companies]]
name = "Bad"
source = "lever"
token = "bad"

[[companies]]
name = "Example"
source = "greenhouse"
token = "example"
""")

    class Mixed:
        def get(self, source, token):
            if source == 'lever':
                return [
                    {
                        'id': 'a',
                        'text': 'Software Intern',
                        'hostedUrl': 'https://example.invalid/a',
                        'updatedAt': 1e99,
                    }
                ]
            return {'jobs': [job()]}

    result = ingest(project, fetcher=Mixed())
    assert len(result['failures']) == 1 and 'Bad' in result['failures'][0]
    assert result['companies_ok'] == 1 and result['new_jobs'] == 1
    with db_for(project) as db:
        assert 'timestamp' in db.execute("SELECT last_error FROM sources WHERE company='Bad'").fetchone()[0]


def test_partial_run_does_not_report_health_while_other_source_is_held(project):
    (project / 'companies.toml').write_text("""[[companies]]
name = "Example"
source = "greenhouse"
token = "example"

[[companies]]
name = "Other"
source = "lever"
token = "other"
""")

    class Both:
        def __init__(self, green):
            self.green = green

        def get(self, source, token):
            return {'jobs': self.green} if source == 'greenhouse' else []

    ingest(project, fetcher=Both([job(1)]))
    with db_for(project) as db:
        before = db.execute('SELECT last_ok_at FROM heartbeat').fetchone()[0]
    assert ingest(project, fetcher=Both([]), source='greenhouse')['warnings']
    assert ingest(project, fetcher=Both([]), source='lever')['warnings'] == []
    with db_for(project) as db:
        assert db.execute('SELECT last_ok_at FROM heartbeat').fetchone()[0] == before
    ingest(project, fetcher=Both([job(1)]), source='greenhouse')
    with db_for(project) as db:
        assert db.execute('SELECT last_ok_at FROM heartbeat').fetchone()[0] != before
