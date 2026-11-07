"""Recorded HTTP responses exercise imports without contacting job boards."""

import gzip
import io
import json
import time
import urllib.error
import urllib.request
from email.message import Message

import pytest
from conftest import ROOT, FixtureFetcher, db_for, example_settings

from jobhunt.fetch import Fetcher
from jobhunt.ingest import ingest


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, *, compressed=False):
        super().__init__(body)
        self.headers = {'Content-Type': 'application/json'}
        if compressed:
            self.headers['Content-Encoding'] = 'gzip'


class RecordedOpener(urllib.request.OpenerDirector):
    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)

    def open(self, fullurl, data=None, timeout=60):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize('source', ['greenhouse', 'lever', 'ashby'])
def test_recorded_company_board_imports_searchable_jobs(project, source, cli, capsys):
    (project / 'companies.toml').write_text(
        f'[[companies]]\nname="Recorded"\nsource="{source}"\ntoken="recorded"\n'
    )
    fetcher = Fetcher(example_settings())
    payload = (ROOT / 'tests/fixtures' / f'{source}.json').read_bytes()
    fetcher.opener = RecordedOpener([Response(payload)])
    assert not ingest(project, fetcher=fetcher)['failures']
    capsys.readouterr()
    assert cli('list', '--include-non-us') == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows and all(row['title'] and row['company'] == 'Recorded' for row in rows)
    assert cli('show', str(rows[0]['id'])) == 0
    assert '<p>' not in json.loads(capsys.readouterr().out)['description_text']


def test_temporary_network_failure_recovers_without_losing_jobs(project):
    ingest(project, fetcher=FixtureFetcher())
    fetcher = Fetcher(example_settings())
    fetcher.opener = RecordedOpener(
        [
            urllib.error.URLError('temporary connection failure'),
            Response(json.dumps({'jobs': FixtureFetcher().rows}).encode()),
        ]
    )
    assert not ingest(project, fetcher=fetcher)['failures']
    with db_for(project) as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE status='open'").fetchone()[0] == 1


def test_unavailable_board_reports_failure_and_preserves_existing_jobs(project, cli, capsys):
    ingest(project, fetcher=FixtureFetcher())
    fetcher = Fetcher(example_settings())
    fetcher.opener = RecordedOpener(
        [
            urllib.error.HTTPError('https://example.invalid', 404, 'Not Found', Message(), None),
        ]
    )
    assert ingest(project, fetcher=fetcher)['failures']
    capsys.readouterr()
    assert cli('status') == 0
    assert '404' in capsys.readouterr().out
    with db_for(project) as db:
        assert db.execute('SELECT status FROM jobs').fetchone()[0] == 'open'


def test_compressed_community_list_imports_jobs(project):
    (project / 'companies.toml').write_text('[[lists]]\nsource="simplify"\n')
    payload = (ROOT / 'tests/fixtures/simplify_list.json').read_bytes()
    fetcher = Fetcher(example_settings())
    fetcher.opener = RecordedOpener([Response(gzip.compress(payload), compressed=True)])
    assert not ingest(project, fetcher=fetcher)['failures']
    with db_for(project) as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE company='Acme Corp'").fetchone()[0] == 1


def test_slow_board_cannot_stall_an_import_indefinitely(project):
    class SlowResponse(Response):
        def read(self, size=-1):
            time.sleep(5)
            return super().read(size)

    settings = example_settings()
    settings['response_deadline_seconds'] = 1.2
    fetcher = Fetcher(settings)
    fetcher.opener = RecordedOpener([SlowResponse(b'{"jobs":[]}')])
    started = time.monotonic()
    assert ingest(project, fetcher=fetcher)['failures']
    assert time.monotonic() - started < 3
