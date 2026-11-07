import json

from conftest import FixtureFetcher, db_for, job

from jobhunt.cli import execute, parser
from jobhunt.ingest import ingest
from jobhunt.shortlist import shortlist


def test_shortlist_us_ms_software_dates_rank_and_non_excluding_notes(project, capsys):
    rows = [
        job(1, title='Software Intern Summer 2027', location={'name': 'Remote - US'}),
        job(2, title='Software Intern Summer 2027', location={'name': 'Boston, MA'}),
        job(3, title='Software Intern Summer 2027', content='We do not currently offer visa sponsorship.'),
        job(4, title='Hardware Engineer Intern Summer 2027'),
        job(5, title='Software Intern Summer 2027', location={'name': 'Toronto, ON'}),
        job(6, title='PhD Software Intern Summer 2027', content='PhD required'),
        job(7, title='Software Intern Fall 2027', content='We recruited for Summer 2027 last year.'),
        job(8, title='Software Intern Summer 2027', location={'name': 'Remote'}),
    ]
    ingest(project, fetcher=FixtureFetcher(rows))
    with db_for(project) as db:
        result = shortlist(db, since='7d', preferred=['NYC metro'])
        assert [r['requisition_id'] for r in result] == ['3', '8', '2', '1']
        assert result[0]['restriction_note'] == 'says no visa sponsorship'
        assert len(shortlist(db, limit=2)) == 2
    capsys.readouterr()
    assert execute(parser().parse_args(['--root', str(project), 'list'])) == 0
    listed = [json.loads(line)['id'] for line in capsys.readouterr().out.splitlines()]
    assert 5 not in listed
    assert execute(parser().parse_args(['--root', str(project), 'shortlist', '--since', '7d'])) == 0
    assert 'says no visa sponsorship' in capsys.readouterr().out
