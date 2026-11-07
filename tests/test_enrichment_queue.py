import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import FixtureFetcher, ListFetcher, db_for, job

from jobhunt.cli import execute, parser
from jobhunt.enrich import enrich
from jobhunt.ingest import ingest
from jobhunt.queue import queue
from jobhunt.workflow import digest, mark

PAYLOADS = json.loads((Path(__file__).parent / 'fixtures/posting_enrichment.json').read_text())


class EnrichFetcher:
    def posting(self, source, token, identifier):
        return PAYLOADS[source]


def list_row(
    identifier, url, *, title='Software Intern', location='New York, NY', company='Acme', season='Summer'
):
    return {
        'id': identifier,
        'url': url,
        'title': title,
        'company_name': company,
        'locations': [location],
        'active': True,
        'season': season,
        'terms': ['Summer 2027'],
    }


def test_enrich_cap_recency_classification_and_reingest_preserves_text(listed):
    rows = [
        list_row('gh', 'https://boards.greenhouse.io/acme/jobs/123'),
        list_row('lv', 'https://jobs.lever.co/acme/abc-123', company='Other'),
        list_row('as', 'https://jobs.ashbyhq.com/acme/def-456', company='Third', location='In-Office'),
    ]
    fetcher = ListFetcher(simplify=rows)
    ingest(listed, source='simplify', fetcher=fetcher)
    with db_for(listed) as db:
        db.execute(
            'UPDATE jobs SET posted_at=CASE requisition_id '
            "WHEN 'gh' THEN '2026-11-06' WHEN 'lv' THEN '2026-11-05' ELSE '2026-11-04' END"
        )
    ats = EnrichFetcher()
    assert enrich(listed, limit=1, fetcher=ats)['enriched'] == 1
    with db_for(listed) as db:
        assert (
            db.execute("SELECT requisition_id FROM jobs WHERE description_text LIKE '%Pay $40%'").fetchone()[
                0
            ]
            == 'gh'
        )
    assert enrich(listed, fetcher=ats)['enriched'] == 2
    ingest(listed, source='simplify', fetcher=fetcher)
    with db_for(listed) as db:
        result = {r['requisition_id']: r for r in db.execute('SELECT * FROM jobs')}
        assert result['gh']['pay_hourly_usd'] == 50 and 'Pay $40' in result['gh']['description_text']
        assert result['as']['country'] == 'non-US' and result['as']['pay_hourly_usd'] == 60
        assert [r['requisition_id'] for r in queue(db)] == ['gh']
    assert enrich(listed, fetcher=ats)['attempted'] == 0


def test_short_link_failure_keeps_the_original_posting_available(listed):
    class Offline(ListFetcher):
        def resolve(self, url):
            raise OSError('fixture offline')

    fetcher = Offline(vanshb03=[list_row('bad', 'https://go.vansh.nyc/bad')])
    for _ in range(2):
        assert not ingest(listed, source='vanshb03', fetcher=fetcher)['failures']
    with db_for(listed) as db:
        entries = queue(db)
        assert len(entries) == 1 and entries[0]['resolved_url'] == 'https://go.vansh.nyc/bad'


def test_distinct_locations_and_terms_kept(listed):
    rows = [
        list_row('1', 'https://example.invalid/1'),
        list_row('2', 'https://example.invalid/2', location='Boston, MA'),
        list_row('3', 'https://example.invalid/3', season='Winter'),
        list_row('4', 'https://example.invalid/4', season='Spring'),
        list_row('5', 'https://example.invalid/5', season='Summer 2026'),
    ]
    ingest(listed, source='vanshb03', fetcher=ListFetcher(vanshb03=rows))
    with db_for(listed) as db:
        assert {row['requisition_id'] for row in queue(db)} == {'1', '2'}
    mark(listed, 3, 'applied')
    with db_for(listed) as db:
        assert {row['requisition_id'] for row in queue(db)} == {'1', '2'}


def test_queue_strict_priority_applied_and_foreign_exclusions(project, capsys):
    rows = [
        job(
            i,
            title=f'Software Intern Summer 2027 Team {i}',
            location={'name': 'Boston, MA'},
            content='$40 per hour',
        )
        for i in range(1, 9)
    ]
    ingest(project, fetcher=FixtureFetcher(rows))
    with db_for(project) as db:
        db.execute("UPDATE jobs SET posted_at='2026-11-05',first_seen='2026-11-01'")
        db.execute("UPDATE jobs SET posted_at='2026-11-06',pay_hourly_usd=20 WHERE id=1")
        db.execute('UPDATE jobs SET pay_hourly_usd=1000 WHERE id=2')
        db.execute("UPDATE jobs SET location_normalized='NYC metro' WHERE id=3")
        db.execute("UPDATE jobs SET updated_at='2026-11-06',posted_at='2026-11-04' WHERE id=4")
        db.execute("UPDATE jobs SET country='non-US' WHERE id=8")
    for status, identifier in [('applying', 5), ('skipped', 6), ('interested', 7)]:
        mark(project, identifier, status)
    with db_for(project) as db:
        at = datetime(2026, 11, 6, 23, tzinfo=UTC)
        ranked = queue(db, 40, at=at, preferred=['nyc METRO'])
        assert [r['id'] for r in ranked] == [1, 2, 3, 7, 4]
        # Without a preferred location, a same-day, same-pay tie goes to the newer ID.
        assert [r['id'] for r in queue(db, 40, at=at)] == [1, 2, 7, 3, 4]
        assert ranked[0]['age_days'] < 1
        text = digest(db, project)
        assert text.startswith('Fresh (posted in the last 48h)') and 'Team 5' not in text
    execute(parser().parse_args(['--root', str(project), 'queue', '--limit', '2']))
    output = capsys.readouterr().out
    assert 'resolved URL | ATS | needs account' in output and '$20.00/h' in output


def test_closed_identity_does_not_hide_still_open_row(project):
    ingest(project, fetcher=FixtureFetcher([job(1), job(2)]))
    with db_for(project) as db:
        db.execute("UPDATE jobs SET url='https://example.invalid/same'")
        db.execute("UPDATE jobs SET status='closed' WHERE id=1")
        assert [r['id'] for r in queue(db)] == [2]


def test_new_short_link_dedupes_against_board(listed):
    rows = [list_row('new', 'https://go.vansh.nyc/new', title='Software Engineering Intern')]

    class Resolved(ListFetcher):
        def resolve(self, url):
            return 'https://example.invalid/jobs/1?utm_source=x'

    fetcher = Resolved(vanshb03=rows)
    ingest(listed, source='greenhouse', fetcher=fetcher)
    result = ingest(listed, source='vanshb03', fetcher=fetcher)
    assert result['duplicates'] == 1
    with db_for(listed) as db:
        assert len(queue(db)) == 1


def test_enrich_failure_leaves_description_and_classification_intact(listed):
    ingest(
        listed,
        source='simplify',
        fetcher=ListFetcher(simplify=[list_row('one', 'https://jobs.lever.co/acme/abc-123')]),
    )

    class Broken:
        def posting(self, *args):
            raise OSError('fixture failure')

    with db_for(listed) as db:
        before = tuple(
            db.execute('SELECT description_text,pay_hourly_usd,matches_target FROM jobs').fetchone()
        )
    assert len(enrich(listed, fetcher=Broken())['failures']) == 1
    with db_for(listed) as db:
        assert (
            tuple(db.execute('SELECT description_text,pay_hourly_usd,matches_target FROM jobs').fetchone())
            == before
        )


def test_closed_board_does_not_discard_active_list_posting(listed):
    ingest(listed, source='greenhouse', fetcher=ListFetcher())
    with db_for(listed) as db:
        db.execute("UPDATE jobs SET status='closed'")

    class Resolved(ListFetcher):
        def resolve(self, url):
            return 'https://example.invalid/jobs/1'

    fetcher = Resolved(
        vanshb03=[
            list_row('open', 'https://go.vansh.nyc/open', title='Software Engineering Intern, Summer 2027')
        ]
    )
    assert ingest(listed, source='vanshb03', fetcher=fetcher)['new_jobs'] == 1
    with db_for(listed) as db:
        assert len(queue(db)) == 1 and queue(db)[0]['source'] == 'vanshb03'


def test_fresh_exact_times_boundary_fallback_and_remaining_pay_order(project, capsys):
    from datetime import timedelta

    at = datetime.now(UTC)
    rows = [job(i, title=f'Software Intern Summer 2027 Fresh Team {i}') for i in range(1, 8)]
    ingest(project, fetcher=FixtureFetcher(rows))
    with db_for(project) as db:
        # Same-day salary must never outrank a newer fresh posting.
        for identifier, hours, pay in (
            (1, 1, 10),
            (2, 2, 200),
            (3, 48, 50),
            (4, 49, 80),
            (5, 49, 90),
            (6, 100, 300),
        ):
            db.execute(
                'UPDATE jobs SET posted_at=?,pay_hourly_usd=? WHERE id=?',
                ((at - timedelta(hours=hours)).isoformat(), pay, identifier),
            )
        db.execute(
            'UPDATE jobs SET posted_at=NULL,first_seen=?,updated_at=? WHERE id=7',
            ((at - timedelta(minutes=30)).isoformat(), at.isoformat()),
        )
        ranked = queue(db, 20, at=at)
        assert [r['id'] for r in ranked] == [7, 1, 2, 3, 5, 4, 6]
        assert [r['fresh'] for r in ranked] == [True] * 4 + [False] * 3
        assert [r['id'] for r in queue(db, 2, at=at)] == [7, 1]
        report = digest(db, project)
        assert report.startswith('Fresh (posted in the last 48h)')
        assert report.index('Fresh Team 7') < report.index('Fresh Team 1') < report.index('Fresh Team 2')
        assert report.index('Other queue items') < report.index('Fresh Team 5')
    execute(parser().parse_args(['--root', str(project), 'queue', '--limit', '2']))
    output = capsys.readouterr().out
    assert (
        output.index('Fresh (posted in the last 48h)')
        < output.index('Fresh Team 7')
        < output.index('Fresh Team 1')
    )
    mark(project, 7, 'applied')
    with db_for(project) as db:
        assert 7 not in [r['id'] for r in queue(db, at=at)]


@pytest.mark.parametrize('command', ['queue', 'digest'])
def test_account_sites_hidden_by_default_and_opt_in_preserves_freshness(project, capsys, command):
    from datetime import timedelta

    urls = [
        'https://t-mobile.wd1.myworkdayjobs.com/External/job/Intern_REQ380650',
        'https://careers.example.icims.com/jobs/22/job',
        'https://example.avature.net/careers/33',
        'https://example.taleo.net/job?job=44',
        'https://example.successfactors.com/jobs/55',
        'https://unknown.invalid/jobs/66',
        'https://job-boards.greenhouse.io/example/jobs/77',
    ]
    ingest(
        project,
        fetcher=FixtureFetcher(
            [
                job(i, title=f'Software Intern Summer 2027 Account Team {i}', absolute_url=url)
                for i, url in enumerate(urls, 1)
            ]
        ),
    )
    with db_for(project) as db:
        for identifier in range(1, 8):
            db.execute(
                'UPDATE jobs SET posted_at=? WHERE id=?',
                ((datetime.now(UTC) - timedelta(hours=identifier)).isoformat(), identifier),
            )
        assert [r['id'] for r in queue(db)] == [6, 7]
        assert [r['id'] for r in queue(db, 3, include_accounts=True)] == [1, 2, 3]
    capsys.readouterr()
    args = ['--root', str(project), command]
    assert execute(parser().parse_args(args)) == 0
    output = capsys.readouterr().out
    assert 'Account Team 1' not in output and 'Account Team 6' in output and 'Account Team 7' in output
    assert execute(parser().parse_args([*args, '--include-accounts'])) == 0
    output = capsys.readouterr().out
    assert output.index('Account Team 1') < output.index('Account Team 6')
    assert 'account=yes' in output
    mark(project, 1, 'applied')
    with db_for(project) as db:
        assert 1 not in [r['id'] for r in queue(db, include_accounts=True)]


def test_workday_variants_appear_once_and_marks_preserve_distinct_requisitions_and_employers(project):
    rows = []
    for i, (requisition, scope) in enumerate(
        [
            ('REQ380650', 'acme.wd1.myworkdayjobs.com/en-US/External'),
            ('J00170964', 'wd5.myworkdaysite.com/en-US/recruiting/acme/External'),
            ('Job_Req_55892', 'acme.wd1.myworkdayjobs.com/en-US/External'),
            ('R26_1137-1', 'wd5.myworkdaysite.com/en-US/recruiting/acme/External'),
        ]
    ):
        base = f'https://{scope}'
        urls = [
            base + f'/job/NY/Intern_{requisition}',
            base.upper().replace('/EN-US/', '/FR-FR/') + f'/JOB/Boston/Changed_{requisition.lower()}/apply/',
            base + f'/job/NY/Distinct_{requisition}-2',
            base.replace('acme', 'other') + f'/job/NY/Intern_{requisition}',
        ]
        rows.extend(
            job(i * 4 + j, title=f'Software Intern Summer 2027 Team {i}-{j}', absolute_url=url)
            for j, url in enumerate(urls, 1)
        )
    ingest(project, fetcher=FixtureFetcher(rows))
    with db_for(project) as db:
        assert len(queue(db, include_accounts=True)) == 12
    for i in range(4):
        mark(project, i * 4 + 2, 'applied')
    with db_for(project) as db:
        assert {row['id'] for row in queue(db, include_accounts=True)} == {
            i * 4 + j for i in range(4) for j in (3, 4)
        }


def test_imported_pay_uses_hourly_usd_and_ignores_bonuses_and_foreign_currency(project, cli, capsys):
    cases = [
        ('$40-$60/hr', 50),
        ('$2,000 per week', 50),
        ('$8,650/month', 50),
        ('$100K\u2013$108K annually', 50),
        ('A $50,000 signing bonus', None),
        ('CAD $50/hour', None),
        ('$50/hour CAD', None),
        ('USD $50/hour', 50),
    ]
    ingest(
        project,
        fetcher=FixtureFetcher(
            [
                job(i, title=f'Software Intern Summer 2027 Pay Team {i}', content=text)
                for i, (text, _) in enumerate(cases, 1)
            ]
        ),
    )
    capsys.readouterr()
    for i, (_, expected) in enumerate(cases, 1):
        assert cli('show', str(i)) == 0
        assert json.loads(capsys.readouterr().out)['pay_hourly_usd'] == expected


def test_enrichment_handles_missing_pay_text_and_foreign_structured_pay(listed):
    payloads = {
        '1': {
            'id': 1,
            'content': 'Software internship Summer 2027.',
            'pay_input_ranges': [
                {
                    'min_cents': 4000,
                    'max_cents': 4000,
                    'currency_type': 'USD',
                    'title': 'Hourly base pay',
                    'blurb': None,
                }
            ],
        },
        '2': {'id': '2', 'descriptionPlain': 'Software internship Summer 2027. $40/hr', 'salaryRange': None},
        '3': {
            'id': '3',
            'descriptionPlain': 'Software internship Summer 2027. $50/hr',
            'salaryRange': {'currency': 'CAD', 'interval': 'hour', 'min': 50, 'max': 50},
        },
    }

    class Postings:
        def posting(self, source, token, identifier):
            return payloads[identifier]

    rows = [
        list_row('1', 'https://boards.greenhouse.io/acme/jobs/1', company='One'),
        list_row('2', 'https://jobs.lever.co/acme/2', company='Two'),
        list_row('3', 'https://jobs.lever.co/acme/3', company='Three'),
    ]
    ingest(listed, source='simplify', fetcher=ListFetcher(simplify=rows))
    assert not enrich(listed, fetcher=Postings())['failures']
    with db_for(listed) as db:
        assert [r[0] for r in db.execute('SELECT pay_hourly_usd FROM jobs ORDER BY requisition_id')] == [
            40,
            40,
            None,
        ]
        assert len(queue(db)) == 3
