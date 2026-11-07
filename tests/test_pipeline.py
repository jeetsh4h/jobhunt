import json

import pytest
from conftest import FixtureFetcher, db_for, job, snapshot

from jobhunt.ingest import ingest
from jobhunt.settings import Paused


def test_idempotent_ingest_and_reopen(project):
    first = ingest(project, fetcher=FixtureFetcher())
    assert first['new_jobs'] == 1
    with db_for(project) as db:
        first_seen = db.execute('SELECT first_seen FROM jobs').fetchone()[0]
    second = ingest(project, fetcher=FixtureFetcher())
    assert second['new_jobs'] == 0
    with db_for(project) as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT first_seen FROM jobs').fetchone()[0] == first_seen
        assert db.execute('SELECT count(*) FROM heartbeat').fetchone()[0] == 1
    ingest(project, fetcher=FixtureFetcher([]))
    ingest(project, fetcher=FixtureFetcher())
    with db_for(project) as db:
        assert db.execute('SELECT status,first_seen FROM jobs').fetchone()[:] == ('open', first_seen)


def test_absent_jobs_closed_never_deleted(project):
    ingest(project, fetcher=FixtureFetcher([job(1), job(2)]))
    summary = ingest(project, fetcher=FixtureFetcher([job(2)]))
    assert summary['closed_jobs'] == 1
    with db_for(project) as db:
        assert [tuple(row) for row in db.execute('SELECT requisition_id,status FROM jobs ORDER BY id')] == [
            ('1', 'closed'),
            ('2', 'open'),
        ]


@pytest.mark.parametrize('failure', [OSError('offline'), ValueError('invalid JSON')])
def test_failed_feed_never_closes_or_refreshes_heartbeat(project, failure):
    ingest(project, fetcher=FixtureFetcher())
    with db_for(project) as db:
        before = db.execute('SELECT last_ok_at FROM heartbeat').fetchone()[0]
    result = ingest(project, fetcher=FixtureFetcher(error=failure))
    assert len(result['failures']) == 1
    with db_for(project) as db:
        assert db.execute('SELECT status FROM jobs').fetchone()[0] == 'open'
        assert db.execute('SELECT last_error FROM sources').fetchone()[0]
        assert db.execute('SELECT last_ok_at FROM heartbeat').fetchone()[0] == before


@pytest.mark.parametrize('existing', [False, True])
def test_dry_run_changes_no_files_or_database(project, existing):
    if existing:
        ingest(project, fetcher=FixtureFetcher())
    before = snapshot(project)
    result = ingest(project, dry_run=True, fetcher=FixtureFetcher([job(2)]))
    assert result['jobs_seen'] == 1
    assert snapshot(project) == before


@pytest.mark.parametrize('method', ['config', 'file'])
def test_pause_blocks_ingest_including_dry_run(project, method):
    if method == 'config':
        settings = json.loads((project / 'config.yaml').read_text())
        settings['paused'] = True
        (project / 'config.yaml').write_text(json.dumps(settings))
    else:
        (project / 'PAUSE').touch()
    before = snapshot(project)
    for dry in (False, True):
        with pytest.raises(Paused):
            ingest(project, dry_run=dry, fetcher=FixtureFetcher())
    assert snapshot(project) == before


def test_pause_during_request_blocks_snapshot_mutation(project):
    class PauseInFlight(FixtureFetcher):
        def get(self, source, token):
            settings = json.loads((project / 'config.yaml').read_text())
            settings['paused'] = True
            (project / 'config.yaml').write_text(json.dumps(settings))
            return super().get(source, token)

    with pytest.raises(Paused):
        ingest(project, fetcher=PauseInFlight())
    with db_for(project) as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM heartbeat').fetchone()[0] == 0


def test_cli_pause_resume_and_read_commands(project, cli, capsys):
    ingest(project, fetcher=FixtureFetcher())
    assert cli('pause') == 0
    assert cli('ingest') == 1
    assert cli('status') == 0
    assert '"paused": true' in capsys.readouterr().out
    assert cli('resume') == 0
    assert (
        cli('list', '--status', 'open', '--role-family', 'software_internship', '--since', '2020-01-01') == 0
    )
    assert 'Software Engineering' in capsys.readouterr().out
    assert cli('show', '1') == 0
    assert cli('export-csv', '--matches-target') == 0
    assert 'description_text' in capsys.readouterr().out
    assert cli('heartbeat-check') == 0
    with db_for(project) as db:
        assert {'pause', 'resume'} <= {r[0] for r in db.execute('SELECT action FROM audit_log')}
        db.execute("UPDATE heartbeat SET last_ok_at='2020-01-01T00:00:00+00:00'")
    assert cli('heartbeat-check') == 1
