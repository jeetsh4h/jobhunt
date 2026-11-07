"""Manual application preparation. Never contacts employers or calls models."""

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .db import audit, connect
from .queue import queue, queue_sections
from .settings import config, ensure_active
from .shortlist import since_time

APPLICATION_STATES = ('interested', 'applying', 'applied', 'skipped', 'rejected', 'interview', 'offer')
TEMPLATE = Path(__file__).resolve().parents[2] / 'answers.example.yaml'
PHONE_REFERENCE = '[phone: answers.yaml contact.phone]'


def answers(root):
    template = json.loads(TEMPLATE.read_text())
    path = root / 'answers.yaml'
    supplied = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(supplied, dict):
        raise ValueError('answers.yaml must be an object using JSON syntax')
    unknown = supplied.keys() - template.keys()
    if unknown:
        raise ValueError('unknown answers fields: ' + ', '.join(sorted(unknown)))
    result, missing = {}, []
    for key, default in template.items():
        value = supplied.get(key, {} if isinstance(default, dict) else '')
        if isinstance(default, dict):
            if not isinstance(value, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError(f'answers.{key} must map text keys to text values')
            if key != 'approved_free_text' and value.keys() - default.keys():
                raise ValueError(f'unknown answers.{key} field')
            value = {**{field: '' for field in default}, **value}
            for field, entry in value.items():
                if not entry.strip():
                    if key == 'eeo':
                        value[field] = 'decline to self-identify'
                    else:
                        missing.append(key + '.' + field)
        elif not isinstance(value, str):
            raise ValueError(f'answers.{key} must be text')
        elif not value.strip():
            missing.append(key)
        result[key] = value
    if not path.exists():
        missing.insert(0, 'answers.yaml (copy answers.example.yaml and fill it)')
    return result, missing


def resume_variant(settings, role):
    return settings.get('resume_variants', {}).get(role, 'software-engineer')


def answer(root, key):
    path = root / 'answers.yaml'
    if not path.exists():
        raise ValueError('answers.yaml missing')
    supplied = json.loads(path.read_text())
    value = supplied
    for part in key.split('.'):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f'unknown answer key: {key}')
        value = value[part]
    if not isinstance(value, str):
        raise ValueError('answer key must select a single text value')
    if not value.strip():
        raise ValueError(f'answer value missing: {key}')
    phone = supplied.get('contact', {}).get('phone', '')
    return value if key == 'contact.phone' else mask_phone(value, phone)


def mask_phone(content, phone):
    if not phone:
        return content
    digits = re.sub(r'\D', '', phone)
    variants = {digits}
    if len(digits) == 11 and digits.startswith('1'):
        variants.add(digits[1:])
    elif len(digits) == 10:
        variants.add('1' + digits)
    for value in sorted(variants, key=len, reverse=True):
        if len(value) >= 7:
            pattern = r'(?<!\d)\+?' + r'[\W_]*'.join(value) + r'(?!\d)'
            content = re.sub(pattern, lambda _: PHONE_REFERENCE, content)
    return content


def mark(root, job_id, status, note=None):
    ensure_active(root)
    settings = config(root)
    with connect(root / settings['database']) as db:
        job = db.execute('SELECT role_family FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job:
            raise ValueError('job ID not found')
        ensure_active(root)
        at = datetime.now(UTC).isoformat()
        db.execute(
            """INSERT INTO applications(job_id,status,resume_variant,notes,updated_at,applied_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,
            notes=CASE WHEN ? IS NULL THEN applications.notes ELSE excluded.notes END,
            updated_at=excluded.updated_at,applied_at=coalesce(applications.applied_at,excluded.applied_at)""",
            (
                job_id,
                status,
                resume_variant(settings, job['role_family']),
                note or '',
                at,
                at if status == 'applied' else None,
                note,
            ),
        )
        audit(db, 'mark', 'applications', json.dumps({'job_id': job_id, 'status': status}))


def resume_details(root, settings, variant):
    """Relative PDF paths resolve against resume_repository when set, otherwise the jobhunt root."""
    base = Path(settings['resume_repository']).expanduser() if settings.get('resume_repository') else root
    pdf = base / Path(settings.get('resume_pdfs', {}).get(variant, f'resume/{variant}.pdf')).expanduser()
    source = settings.get('resume_source')
    warning = None
    if not pdf.is_file():
        warning = f'MISSING resume PDF: {pdf}'
    elif source:
        result = subprocess.run(
            ['git', '-C', str(base), 'log', '-1', '--format=%ct', '--', source],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode or not result.stdout.strip():
            warning = f'Cannot determine newest {source} commit; check resume freshness manually.'
        elif pdf.stat().st_mtime < int(result.stdout.strip()):
            warning = f'STALE resume PDF: older than newest {source} commit. Rebuild your resume.'
    return pdf, warning


def education_check(fields):
    current, previous = fields['education_current'], fields['education_previous']
    if not current['school'].strip():
        return ['education_current.school is MISSING; fill it in answers.yaml.']
    details = ', '.join(
        part
        for part in (' '.join(p for p in (current['degree'], current['major']) if p.strip()), current['end'])
        if part.strip()
    )
    lines = [
        f'Always enter {current["school"]} as the current school.',
        f'1. {current["school"]} — current' + (f', {details}.' if details else '.'),
    ]
    if previous['school'].strip():
        lines.append(f'2. {previous["school"]} — previous school.')
    return lines


def apply_pack(root, job_id):
    ensure_active(root)
    settings = config(root)
    with connect(root / settings['database'], readonly=True) as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job:
            raise ValueError('job ID not found')
        application = db.execute(
            'SELECT resume_variant FROM applications WHERE job_id=?', (job_id,)
        ).fetchone()
    variant = application[0] if application else resume_variant(settings, job['role_family'])
    pdf, warning = resume_details(root, settings, variant)
    fields, missing = answers(root)
    lines = [
        f'# {job["title"]} — {job["company"]}',
        f'Job: {job["url"]}',
        f'Location: {job["location_raw"]}',
        f'Source: {job["source"]}',
        f'Season evidence: {job["season_evidence"] or "not established"}',
        f'Restriction note: {job["restriction_note"] or "none detected"}',
        f'Resume variant: {variant}',
        f'Resume PDF: {pdf}',
        'Review the posting dates, office requirements, and form questions yourself.',
    ]
    if job['status'] == 'closed':
        lines.append('WARNING: this posting is closed in the last feed.')
    if warning:
        lines.append('WARNING: ' + warning)
        missing.append('current resume PDF / freshness check')
    lines += ['', '## Education check', *education_check(fields), '', '## Form answers']
    for key, value in fields.items():
        if isinstance(value, dict):
            lines += ['', f'### {key}']
            lines += [
                f'- {field}: '
                f'{PHONE_REFERENCE if key == "contact" and field == "phone" else entry or "MISSING"}'
                for field, entry in value.items()
            ]
        else:
            lines.append(f'- {key}: {value or "MISSING"}')
    lines += ['', '## MISSING — owner must supply']
    lines += [f'- {field}' for field in missing] or ['- No missing configured fields.']
    lines += [
        '- Any work-authorization or sponsorship question whose wording differs from the '
        'owner-provided answers must be answered by the owner.',
        '- Any required free-text question without an approved answer must be answered by the owner.',
    ]
    ensure_active(root)
    directory = root / 'packs'
    directory.mkdir(mode=0o700, exist_ok=True)
    out = directory / f'{job_id}.md'
    out.write_text(mask_phone('\n'.join(lines) + '\n', fields['contact']['phone']))
    out.chmod(0o600)
    return out


def digest(db, root, since='24h', *, include_accounts=False):
    cutoff = since_time(since)
    settings = config(root)
    at = datetime.now(UTC)
    week = (at - timedelta(days=at.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    applied = db.execute('SELECT count(*) FROM applications WHERE applied_at>=?', (week,)).fetchone()[0]
    heartbeat = db.execute('SELECT max(last_ok_at) FROM heartbeat').fetchone()[0]
    stale = not heartbeat or at - datetime.fromisoformat(heartbeat) > timedelta(hours=26)
    paused = settings['paused'] or (root / 'PAUSE').exists()
    warnings = [
        r[0]
        for r in db.execute(
            'SELECT coalesce(last_error,warning) FROM sources '
            'WHERE last_error IS NOT NULL OR warning IS NOT NULL'
        )
    ]
    new = db.execute('SELECT count(*) FROM jobs WHERE first_seen>=?', (cutoff,)).fetchone()[0]
    # Summaries count observed transitions; last_seen is not a closure time.
    closed = 0
    for row in db.execute(
        "SELECT detail FROM audit_log WHERE action='ingest_complete' AND julianday(at)>=julianday(?)",
        (cutoff,),
    ):
        closed += json.loads(row[0]).get('closed_jobs', 0)
    target = settings.get('weekly_application_target', 15)
    lines = queue_sections(
        queue(
            db,
            limit=15,
            at=at,
            include_accounts=include_accounts,
            preferred=settings.get('preferred_locations', []),
        )
    )
    lines += [
        '',
        'Top 15 queue items shown above.',
        '',
        f'Applications this week: {applied}/{target} (target >={target})'
        if target
        else f'Applications this week: {applied}',
        f'Runner: {"STALE" if stale else "fresh"}; last heartbeat: {heartbeat or "missing"}; '
        f'paused: {str(paused).lower()}',
        f'Since {since}: {new} new jobs; {closed} closures.',
        '',
        'Warnings:',
    ]
    lines += ['- ' + warning for warning in warnings] or ['- none']
    _, missing = answers(root)
    lines += ['', 'Waiting on owner:']
    lines += ['- ' + field for field in missing]
    for row in db.execute(
        "SELECT job_id,status FROM applications WHERE status IN ('interested','applying') ORDER BY updated_at"
    ):
        lines.append(f'- job {row["job_id"]}: {row["status"]}')
    if applied < target:
        lines.append('- Apply using the prepared packs; weekly target has not been reached.')
    return '\n'.join(lines) + '\n'
