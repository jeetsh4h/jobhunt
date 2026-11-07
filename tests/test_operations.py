import os
import shutil
import subprocess

from conftest import ROOT, FixtureFetcher, snapshot

from jobhunt.cli import execute, parser
from jobhunt.db import connect
from jobhunt.ingest import ingest


def test_cold_launcher_dry_run_creates_no_files_or_bytecode(project):
    shutil.copytree(ROOT / 'src', project / 'src', ignore=shutil.ignore_patterns('__pycache__'))
    (project / 'bin').mkdir()
    shutil.copy2(ROOT / 'bin/jobhunt', project / 'bin/jobhunt')
    (project / '.venv').symlink_to(ROOT / '.venv', target_is_directory=True)
    fake = project / 'mock-transport'
    fake.mkdir()
    (fake / 'sitecustomize.py').write_text("""import io, urllib.request
class Response(io.BytesIO):
    status = 200
    headers = {'Content-Type': 'application/json'}
urllib.request.OpenerDirector.open = lambda *args, **kwargs: Response(b'{"jobs":[]}')
""")
    environment = dict(os.environ, PYTHONPATH=str(fake))
    environment.pop('PYTHONDONTWRITEBYTECODE', None)
    before = snapshot(project)
    result = subprocess.run(
        [str(project / 'bin/jobhunt'), 'ingest', '--dry-run'],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"companies_ok": 1' in result.stdout
    assert snapshot(project) == before
    assert not list(project.rglob('*.pyc'))


def test_paused_read_commands_leave_stored_data_unchanged(project, capsys):
    ingest(project, fetcher=FixtureFetcher())
    (project / 'PAUSE').touch()
    path = project / 'data/jobhunt.sqlite3'
    with connect(path) as db:
        before = list(db.iterdump())
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    for command in (['list'], ['show', '1'], ['export-csv'], ['status'], ['shortlist']):
        assert execute(parser().parse_args(['--root', str(project), *command])) == 0
    with connect(path, readonly=True) as db:
        assert list(db.iterdump()) == before
    capsys.readouterr()


def test_backup_cli_restores_and_integrity_check_passes(project, capsys):
    ingest(project, fetcher=FixtureFetcher())
    out = project / 'restored.sqlite3'
    assert execute(parser().parse_args(['--root', str(project), 'backup', '--out', str(out)])) == 0
    with connect(out, readonly=True) as restored:
        assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert restored.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
    capsys.readouterr()
