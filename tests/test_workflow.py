import json
import os
import subprocess

import pytest
from conftest import FixtureFetcher, db_for, snapshot

from jobhunt.cli import execute, parser
from jobhunt.ingest import ingest
from jobhunt.settings import Paused
from jobhunt.workflow import apply_pack, digest, mark


def test_mark_application_workflow_audited_and_paused(project):
    ingest(project, fetcher=FixtureFetcher())
    for status in ('interested', 'applying', 'applied', 'interview'):
        mark(project, 1, status, 'owner note' if status == 'interested' else None)
    with db_for(project) as db:
        row = db.execute('SELECT * FROM applications').fetchone()
        assert row['status'] == 'interview' and row['notes'] == 'owner note'
        assert row['resume_variant'] == 'software-engineer' and row['applied_at']
    (project / 'PAUSE').touch()
    before = snapshot(project)
    with pytest.raises(Paused):
        mark(project, 1, 'skipped')
    assert snapshot(project) == before


def test_apply_pack_includes_missing_answers_resume_and_freshness(project, tmp_path):
    ingest(project, fetcher=FixtureFetcher())
    resume = tmp_path / 'resume-repo'
    resume.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(resume)], check=True)
    (resume / 'cv.tex').write_text('owner resume source')
    subprocess.run(['git', '-C', str(resume), 'add', 'cv.tex'], check=True)
    subprocess.run(
        [
            'git',
            '-C',
            str(resume),
            '-c',
            'user.name=Fixture',
            '-c',
            'user.email=fixture@example.invalid',
            'commit',
            '--quiet',
            '-m',
            'fixture',
        ],
        check=True,
    )
    (resume / 'resumes').mkdir()
    pdf = resume / 'resumes/software-engineer.pdf'
    pdf.write_bytes(b'fixture PDF')
    os.utime(pdf, (1, 1))
    settings = json.loads((project / 'config.yaml').read_text())
    settings['resume_repository'] = str(resume)
    settings['resume_source'] = 'cv.tex'
    settings['resume_pdfs'] = {'software-engineer': 'resumes/software-engineer.pdf'}
    (project / 'config.yaml').write_text(json.dumps(settings))
    out = apply_pack(project, 1)
    content = out.read_text()
    assert out == project / 'packs/1.md'
    assert 'https://example.invalid/jobs/1' in content
    assert 'Season evidence: Summer 2027' in content
    assert 'Resume variant: software-engineer' in content
    assert 'STALE resume PDF' in content
    assert '- phone: [phone: answers.yaml contact.phone]' in content
    assert '- contact.phone' in content
    assert (
        'sponsorship_now_or_future: MISSING' in content and 'sponsorship_for_internship: MISSING' in content
    )
    assert 'decline to self-identify' in content and 'MISSING — owner must supply' in content
    assert out.stat().st_mode & 0o777 == 0o600
    (project / 'PAUSE').touch()
    before = snapshot(project)
    with pytest.raises(Paused):
        apply_pack(project, 1)
    assert snapshot(project) == before


def test_digest_health_jobs_weekly_applications_and_owner_items(project, capsys):
    ingest(project, fetcher=FixtureFetcher())
    mark(project, 1, 'applied')
    (project / 'PAUSE').touch()
    with db_for(project) as db:
        db.execute("UPDATE sources SET warning='fixture source warning'")
        text = digest(db, project)
    assert 'Applications this week: 1/15' in text
    assert 'paused: true' in text and 'last heartbeat:' in text
    assert '1 new jobs; 0 closures' in text
    assert 'Software Engineering Intern, Summer 2027' not in text
    assert text.startswith('Fresh (posted in the last 48h)')
    assert 'fixture source warning' in text and 'authorization.sponsorship_now_or_future' in text
    out = project / 'digest.md'
    assert execute(parser().parse_args(['--root', str(project), 'digest', '--out', str(out)])) == 0
    assert 'Applications this week: 1/15' in out.read_text()
    capsys.readouterr()


def test_digest_uses_configured_weekly_target(project):
    ingest(project, fetcher=FixtureFetcher())
    mark(project, 1, 'applied')
    settings = json.loads((project / 'config.yaml').read_text())
    for target, expected in (
        (1, 'Applications this week: 1/1 (target >=1)'),
        (0, 'Applications this week: 1\n'),
    ):
        settings['weekly_application_target'] = target
        (project / 'config.yaml').write_text(json.dumps(settings))
        with db_for(project) as db:
            text = digest(db, project)
        assert expected in text and 'weekly target has not been reached' not in text


def test_apply_pack_masks_phone_everywhere_and_answer_prints_only_requested_value(project, capsys):
    ingest(project, fetcher=FixtureFetcher())
    phone = '+1 (212) 555-0199'
    supplied = {
        'contact': {'phone': phone},
        'name': {'first': 'Owner'},
        'approved_free_text': {'signature': 'Call +1-212-555-0199 or (212) 555 0199.'},
    }
    (project / 'answers.yaml').write_text(json.dumps(supplied))
    capsys.readouterr()
    assert execute(parser().parse_args(['--root', str(project), 'apply-pack', '1'])) == 0
    output = capsys.readouterr().out
    content = (project / 'packs/1.md').read_text()
    assert phone not in output + content and '555' not in content
    assert '- phone: [phone: answers.yaml contact.phone]' in content
    assert content.count('[phone: answers.yaml contact.phone]') == 3
    assert execute(parser().parse_args(['--root', str(project), 'answer', 'contact.phone'])) == 0
    assert capsys.readouterr().out == phone + '\n'
    assert execute(parser().parse_args(['--root', str(project), 'answer', 'name.first'])) == 0
    assert capsys.readouterr().out == 'Owner\n'
    assert (project / 'answers.yaml').read_text() == json.dumps(supplied)


@pytest.mark.parametrize(
    'key,message',
    [
        ('contact', 'single text value'),
        ('missing.key', 'unknown answer key'),
        ('name.first', 'answer value missing'),
    ],
)
def test_answer_rejects_objects_unknown_and_missing_values_without_stdout(project, cli, capsys, key, message):
    (project / 'answers.yaml').write_text(json.dumps({'contact': {'phone': ''}, 'name': {'first': ''}}))
    assert cli('answer', key) == 1
    captured = capsys.readouterr()
    assert captured.out == '' and message in captured.err


@pytest.mark.parametrize('phone', ['(212) 555-0199', '+1 (212) 555-0199'])
def test_phone_variants_are_masked_in_packs_and_other_answers(project, capsys, phone):
    ingest(project, fetcher=FixtureFetcher())
    variants = [
        separator.join(digits)
        for separator in ('', '\u00a0', '\u202f', '\n', '/_\u2013', ' (')
        for digits in ('2125550199', '12125550199')
    ]
    supplied = {
        'contact': {'phone': phone},
        'approved_free_text': {'signature': 'Call ' + ' or '.join(variants) + '.'},
    }
    (project / 'answers.yaml').write_text(json.dumps(supplied))
    content = apply_pack(project, 1).read_text()
    assert content.count('[phone: answers.yaml contact.phone]') == len(variants) + 1
    assert not any(
        character.isdigit() for character in content.split('### approved_free_text')[1].split('## MISSING')[0]
    )
    capsys.readouterr()
    assert (
        execute(parser().parse_args(['--root', str(project), 'answer', 'approved_free_text.signature'])) == 0
    )
    assert (
        capsys.readouterr().out
        == 'Call ' + ' or '.join(['[phone: answers.yaml contact.phone]'] * len(variants)) + '.\n'
    )
    assert execute(parser().parse_args(['--root', str(project), 'answer', 'contact.phone'])) == 0
    assert capsys.readouterr().out == phone + '\n'


def test_pack_education_check_comes_from_answers_with_current_school_first(project):
    ingest(project, fetcher=FixtureFetcher())
    # Input order cannot put the previous school first in the generated pack.
    supplied = {
        'education_previous': {'school': 'Example College'},
        'education_current': {
            'school': 'Example University',
            'degree': 'M.S.',
            'major': 'Computer Science',
            'end': 'December 2027 (expected)',
        },
    }
    (project / 'answers.yaml').write_text(json.dumps(supplied))
    content = apply_pack(project, 1).read_text()
    assert 'Always enter Example University as the current school.' in content
    assert '1. Example University — current, M.S. Computer Science, December 2027 (expected).' in content
    assert (
        content.index('1. Example University')
        < content.index('2. Example College')
        < content.index('## Form answers')
    )
    assert content.index('### education_current') < content.index('### education_previous')
    (project / 'answers.yaml').unlink()
    content = apply_pack(project, 1).read_text()
    assert 'education_current.school is MISSING' in content and 'Example University' not in content
