import json
from pathlib import Path

from conftest import ListFetcher, db_for

from jobhunt.cli import execute, parser
from jobhunt.ingest import ingest
from jobhunt.shortlist import shortlist

FIXTURES = Path(__file__).parent / 'fixtures'


def test_list_ingest_marks_source_dedupes_against_boards_and_shortlists(listed, capsys):
    result = ingest(listed, fetcher=ListFetcher())
    assert not result['failures']
    with db_for(listed) as db:
        entries = shortlist(db, 50)
        assert [row['company'] for row in entries].count('Example') == 1
        titles = {(r['source'], r['company']) for r in entries}
        assert ('simplify', 'Acme Corp') in titles and ('speedyapply', 'Wayne Enterprises') in titles
        assert ('simplify', 'Globex') not in titles  # Spring 2027
        assert ('vanshb03', 'Umbrella') in titles
        assert [row['company'] for row in entries].count('Umbrella') == 1
    capsys.readouterr()
    assert execute(parser().parse_args(['--root', str(listed), 'status'])) == 0
    status = json.loads(capsys.readouterr().out)
    assert {'greenhouse', 'simplify', 'vanshb03', 'speedyapply'} == {
        row['source'] for row in status['sources']
    }


def test_list_dedupe_keeps_earliest_first_seen_and_is_idempotent(listed):
    ingest(listed, fetcher=ListFetcher())
    with db_for(listed) as db:
        before = db.execute('SELECT id,first_seen FROM jobs ORDER BY id').fetchall()
    again = ingest(listed, fetcher=ListFetcher())
    assert again['new_jobs'] == 0
    with db_for(listed) as db:
        assert db.execute('SELECT id,first_seen FROM jobs ORDER BY id').fetchall() == before


def test_entry_removed_or_marked_inactive_closes_and_empty_list_is_held(listed):
    ingest(listed, fetcher=ListFetcher(), source='simplify')
    rows = json.loads((FIXTURES / 'simplify_list.json').read_text())
    rows[0]['active'] = False
    assert ingest(listed, fetcher=ListFetcher(simplify=rows), source='simplify')['closed_jobs'] == 1
    with db_for(listed) as db:
        assert db.execute("SELECT status FROM jobs WHERE requisition_id='s-1'").fetchone()[0] == 'closed'
    held = ingest(listed, fetcher=ListFetcher(simplify=[]), source='simplify')
    assert held['warnings'] and held['closed_jobs'] == 0


def test_bad_list_is_a_source_error_and_other_sources_continue(listed):
    result = ingest(listed, fetcher=ListFetcher(simplify={'oops': 1}))
    assert len(result['failures']) == 1 and 'simplify' in result['failures'][0]
    with db_for(listed) as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE source='speedyapply'").fetchone()[0] == 2
