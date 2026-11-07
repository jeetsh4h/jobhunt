"""Classification outcomes exposed by `show`, `list`, and the application queue."""

import json

from conftest import FixtureFetcher, db_for, job

from jobhunt.ingest import ingest
from jobhunt.queue import queue


def test_restriction_notes_inform_without_hiding_jobs(project):
    cases = [
        ('We offer visa sponsorship.', 'says sponsorship available'),
        ('F-1 students using CPT are eligible.', 'mentions CPT/OPT/F-1 welcome'),
        ('Build software with us.', None),
        ('We offer visa sponsorship. No sponsorship for this role.', 'says no visa sponsorship'),
        ('We do not currently offer visa sponsorship.', 'says no visa sponsorship'),
        ('We are not able to offer visa sponsorship.', 'says no visa sponsorship'),
        ('Visa sponsorship is not available.', 'says no visa sponsorship'),
        ('No\u200b\u00a0 visa   sponsorship.', 'says no visa sponsorship'),
        ('U.S. citizenship required.', 'US citizenship required'),
        ('U.S. Person Required.', 'export-control/US-person language'),
        ('TS/SCI clearance required before internship.', 'clearance mentioned'),
        ('Clearance is beneficial but not required.', None),
    ]
    ingest(
        project,
        fetcher=FixtureFetcher(
            [
                job(i, title=f'Software Intern Summer 2027 Team {i}', content=text)
                for i, (text, _) in enumerate(cases, 1)
            ]
        ),
    )
    with db_for(project) as db:
        assert len(queue(db, limit=50)) == len(cases)
        assert [r[0] for r in db.execute('SELECT restriction_note FROM jobs ORDER BY id')] == [
            expected for _, expected in cases
        ]


def test_queue_only_targets_software_internships_for_the_current_season(project):
    cases = [
        ('Software Engineering Intern Summer 2027', '', True),
        ('Forward-Deployed Engineer Intern', 'Summer 2027', True),
        ('Backend Engineer Co-op', 'May 24 - Aug 20, 2027', True),
        ('Software Intern', 'Summer (May - September 2027)', True),
        ('Software Intern', '2027 internship: May - August', True),
        ('Software Intern', 'Summer 2027. Graduate in Fall 2027 or Spring 2028.', True),
        ('Software Intern Summer 2027', 'Past Fall 2027 pilot.', True),
        ('Software Intern Fall 2027', 'We recruited for Summer 2027 last year.', False),
        ('Software Intern Summer 2028', 'Summer 2027', False),
        ('Software Intern', 'Summer 2027 pilot. This internship is for the Fall 2027 term.', False),
        ('Software Intern', 'No season stated.', False),
        ('Hardware Engineer Intern Summer 2027', '', False),
        ('Data Science Intern Summer 2027', '', False),
        ('Solutions Engineer Intern Summer 2027', '', False),
        ('Machine Learning Research Intern Summer 2027', '', False),
        ('Senior Software Engineer Summer 2027', '', False),
        ('International Partnerships Summer 2027', '', False),
        ('High School Internship, Software Engineering (Summer 2027)', '', False),
        ('PhD Intern, Software Engineer', 'Summer 2027. PhD required.', False),
        ('Software Intern Summer 2027', 'PhD or MS required.', True),
    ]
    ingest(
        project,
        fetcher=FixtureFetcher(
            [
                job(i, title=f'{title} Team {i}', content=description)
                for i, (title, description, _) in enumerate(cases, 1)
            ]
        ),
    )
    with db_for(project) as db:
        assert {r['id'] for r in queue(db, limit=50)} == {
            i for i, (_, _, expected) in enumerate(cases, 1) if expected
        }


def test_queue_country_filter_handles_unknown_places_and_description_offices(project):
    cases = [
        ('Toronto, ON', '', False),
        ('Doha, Qatar', '', False),
        ('Remote - non-US', '', False),
        ('London, UK', '', False),
        ('Indianapolis, IN', '', True),
        ('Albuquerque, New Mexico', '', True),
        ('Remote', '', True),
        ('Paris, France | New York, NY', '', True),
        ('In-Office', 'Available Locations Lisbon, PT. Available Terms Summer 2027.', False),
        ('In-Office', 'In office 3 days a week in London, UK.', False),
        ('In-Office', 'Available Locations New York, NY. Available Terms Summer 2027.', True),
        ('Austin, TX', 'In office in London, UK.', True),
    ]
    ingest(
        project,
        fetcher=FixtureFetcher(
            [
                job(i, title=f'Software Intern Summer 2027 Team {i}', location={'name': place}, content=text)
                for i, (place, text, _) in enumerate(cases, 1)
            ]
        ),
    )
    with db_for(project) as db:
        assert {r['id'] for r in queue(db, limit=50)} == {
            i for i, (_, _, expected) in enumerate(cases, 1) if expected
        }


def test_show_normalizes_locations_without_confusing_similarly_named_places(project, cli, capsys):
    cases = [
        ('New York,NY', 'NYC metro'),
        ('Newark,NJ', 'NYC metro'),
        ('Albany, New York', 'Albany, New York'),
        ('Newark, DE', 'Newark, DE'),
        ('Remote - United States', 'Remote - US'),
        ('New York, NY | San Francisco, CA', 'NYC metro | San Francisco, CA'),
    ]
    ingest(
        project,
        fetcher=FixtureFetcher(
            [
                job(i, title=f'Software Intern Summer 2027 Team {i}', location={'name': place})
                for i, (place, _) in enumerate(cases, 1)
            ]
        ),
    )
    capsys.readouterr()
    for i, (_, expected) in enumerate(cases, 1):
        assert cli('show', str(i)) == 0
        assert json.loads(capsys.readouterr().out)['location_normalized'] == expected


def test_custom_role_keywords_change_which_jobs_reach_the_queue(project):
    path = project / 'filters.yaml'
    rules = json.loads(path.read_text())
    rules['software_keywords'] = ['widget engineer']
    path.write_text(json.dumps(rules))
    ingest(
        project,
        fetcher=FixtureFetcher(
            [
                job(1, title='Widget Engineer Intern Summer 2027'),
                job(2, title='Software Intern Summer 2027'),
            ]
        ),
    )
    with db_for(project) as db:
        assert [r['id'] for r in queue(db)] == [1]


def test_list_does_not_prioritize_a_city_without_a_user_preference(project, cli, capsys):
    ingest(
        project,
        fetcher=FixtureFetcher(
            [
                job(1, title='Zulu Software Intern Summer 2027', location={'name': 'New York, NY'}),
                job(2, title='Alpha Software Intern Summer 2027', location={'name': 'Boston, MA'}),
            ]
        ),
    )
    capsys.readouterr()
    assert cli('list') == 0
    assert [json.loads(line)['id'] for line in capsys.readouterr().out.splitlines()] == [2, 1]
