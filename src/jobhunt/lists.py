"""Public GitHub internship lists: parsing and cross-source de-duplication.

Only active entries are returned; entries marked inactive/closed (or absent from a
later snapshot) close through the normal snapshot logic in ingest.
"""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from .classify import strip_html

LIST_ROW = '(list)'  # sources-table company for a whole list's health/state


def text_of(value):
    return ' '.join(strip_html(value).split()) if isinstance(value, str) else ''


def epoch_iso(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat()
    except OverflowError, OSError, ValueError:
        return None  # a bad timestamp on a volunteer list is a null field, not a source error


def entry(company, title, url, locations, description, posted_at, updated_at, identifier=None):
    company, title, url = text_of(company), text_of(title), url.strip() if isinstance(url, str) else ''
    parsed = urlsplit(url)
    if not company or not title or parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return None
    return dict(
        requisition_id=identifier or canonical_url(url),
        company=company,
        title=title,
        url=url,
        location_raw=' | '.join(place for place in map(text_of, locations) if place),
        description_text=description,
        posted_at=posted_at,
        updated_at=updated_at,
    )


def json_term(source, row):
    if source == 'vanshb03':
        season = row.get('season')
        # The repository is the Summer 2027 list; its season field says which cohort.
        if not isinstance(season, str) or not season.strip():
            return ''
        return season if re.search(r'\b20\d\d\b', season) else season + ' 2027'
    terms = [t for t in row.get('terms') or [] if isinstance(t, str) and t != 'N/A']
    # "Summer 2027" alongside other terms means summer is accepted; do not let the others conflict.
    return 'Summer 2027' if 'Summer 2027' in terms else ', '.join(terms)


def parse_json_list(source, payload):
    if not isinstance(payload, list):
        raise ValueError('list snapshot must be a JSON list')
    result, seen = [], set()
    for row in payload:
        if not isinstance(row, dict) or row.get('active') is not True or row.get('is_visible') is False:
            continue
        locations = row.get('locations')
        description = json_term(source, row)
        sponsorship = row.get('sponsorship')
        if isinstance(sponsorship, str) and sponsorship != 'Other':
            description += '. ' + sponsorship
        job = entry(
            row.get('company_name'),
            row.get('title'),
            row.get('url'),
            locations if isinstance(locations, list) else [],
            description + '.' if description else '',
            epoch_iso(row.get('date_posted')),
            epoch_iso(row.get('date_updated')),
            row.get('id') if isinstance(row.get('id'), str) and row.get('id') else None,
        )
        if job and job['requisition_id'] not in seen:
            seen.add(job['requisition_id'])
            result.append(job)
    if payload and not result and any(isinstance(r, dict) and r.get('active') is True for r in payload):
        raise ValueError('no valid entries in non-empty list snapshot')
    return result


def parse_table_list(payload, now=None):
    """speedyapply README: | Company | Position | Location | [Salary |] Posting | Age |"""
    if not isinstance(payload, str):
        raise ValueError('list snapshot must be text')
    now = now or datetime.now(UTC)
    result, seen, rows = [], set(), 0
    section = None
    for line in payload.splitlines():
        if line.startswith('## '):
            section = bool(re.search(r'USA.*Internships|Internships.*USA', line, re.I))
        if section is False:
            continue
        if not line.startswith('|') or '🔒' in line:
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) not in (5, 6) or cells[0].lower() == 'company' or cells[0].startswith('---'):
            continue
        rows += 1
        link = re.search(r'href="([^"]+)"', cells[-2])
        age = re.fullmatch(r'(\d+)d', cells[-1])
        location = re.sub(r'\s*\+\d+$', '', re.sub(r'<br\s*/?>', ' | ', cells[2]))
        salary = text_of(cells[3]) if len(cells) == 6 else ''
        job = entry(
            cells[0],
            cells[1],
            link.group(1) if link else '',
            [location],
            salary,
            (now - timedelta(days=int(age[1]))).isoformat() if age else None,
            None,
        )
        if job and job['requisition_id'] not in seen:
            seen.add(job['requisition_id'])
            result.append(job)
    if rows and not result:
        raise ValueError('no valid entries in non-empty list snapshot')
    return result


def parse_list(source, payload):
    return parse_table_list(payload) if source == 'speedyapply' else parse_json_list(source, payload)


def canonical_url(url):
    from .postings import ats_posting, ats_type

    if posting := ats_posting(url):
        source, token, identifier = posting
        host = {
            'greenhouse': 'job-boards.greenhouse.io',
            'lever': 'jobs.lever.co',
            'ashby': 'jobs.ashbyhq.com',
        }[source]
        path = f'{token}/jobs/{identifier}' if source == 'greenhouse' else f'{token}/{identifier}'
        return f'https://{host}/{path}'
    parts = urlsplit(url.strip())
    if ats_type(url) == 'workday':
        path = unquote(parts.path).lower()
        posting = re.fullmatch(
            r'/(?:[a-z]{2}-[a-z]{2}/)?'
            r'(?P<scope>(?:recruiting/[^/]+/)?[^/]+)/'
            r'(?:job|details)/(?P<tail>.+?)(?:/apply)?/?',
            path,
        )
        if posting:
            # Workday separates the title slug from the full requisition with the
            # first underscore. IDs themselves include underscores and multi-part
            # suffixes (Job_Req_55892, R26_1137-1, 10080069-WD-1).
            tail = posting['tail'].rsplit('/', 1)[-1]
            bare_id = re.fullmatch(r'(?:[a-z]*-?\d[a-z0-9-]*|(?:r\d*|req|job_req)_\d[a-z0-9_-]*)', tail)
            requisition = tail if bare_id else tail.partition('_')[2]
            if re.fullmatch(r'[a-z0-9_-]*\d[a-z0-9_-]*', requisition):
                return f'https://{parts.hostname.lower()}/{posting["scope"]}/job/{requisition}'
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.lower().startswith('utm_')
        and k.lower() not in ('ref', 'source', 'src', 'gh_src', 'lever-source', 'lever-origin')
    )
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), urlencode(query), '')
    )


def squash(value):
    return re.sub(r'[^a-z0-9]+', '', value.lower())


def company_key(name):
    return squash(re.sub(r'\b(?:inc|llc|corp|corporation|ltd|co|company)\b\.?', '', name, flags=re.I))


class Seen:
    """Identity index: canonical URL, or normalized company + title + overlapping location.

    The same title in different terms (Summer vs Winter) at one company and place is not a
    duplicate, so the title key also carries its season and year.
    """

    def __init__(self, rows=()):
        self.urls, self.titles = {}, {}
        for row in rows:
            self.add(
                row['resolved_url'] or row['url'],
                row['company'],
                row['title'],
                row['location_normalized'],
                term_key(row),
                row['id'],
            )
            self.urls[canonical_url(row['url'])] = row['id']

    @staticmethod
    def places(normalized):
        return {squash(p) for p in normalized.split(' | ') if p and p != 'Unknown'}

    def has(self, url, company, title, normalized, target):
        return any(True for _ in self.matches(url, company, title, normalized, target))

    def matches(self, url, company, title, normalized, target):
        if canonical_url(url) in self.urls:
            yield self.urls[canonical_url(url)]
            return
        for identifier, other in self.titles.get((company_key(company), squash(title), str(target)), ()):
            if self.places(normalized) & other:
                yield identifier

    def add(self, url, company, title, normalized, target, identifier=None):
        self.urls[canonical_url(url)] = identifier
        self.titles.setdefault((company_key(company), squash(title), str(target)), []).append(
            (identifier, self.places(normalized))
        )


def term_key(row):
    if row['matches_target']:
        return 'summer2027'
    text = row['title'] + ' ' + row['description_text']
    terms = re.findall(r'\b(summer|spring|winter|fall|autumn)\s+(?:of\s+)?(20\d\d)\b', text, re.I)
    return (
        '|'.join(sorted({season.lower().replace('autumn', 'fall') + year for season, year in terms}))
        or 'unknown'
    )


def identity_groups(rows):
    """Connected identities without deleting source rows or application history."""
    rows = list(rows)
    parents = {r['id']: r['id'] for r in rows}
    urls, titles = {}, {}

    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    for row in rows:
        identifier = row['id']
        for url in (row['url'], row['resolved_url'] or row['url']):
            key = canonical_url(url)
            if key in urls:
                parents[root(identifier)] = root(urls[key])
            urls[key] = identifier
        key = (company_key(row['company']), squash(row['title']), term_key(row))
        places = Seen.places(row['location_normalized'])
        for previous, other in titles.get(key, ()):
            if places & other:
                parents[root(identifier)] = root(previous)
        titles.setdefault(key, []).append((identifier, places))
    groups = {}
    for row in rows:
        groups.setdefault(root(row['id']), []).append(row)
    return list(groups.values())


def refresh_duplicates(db):
    # Closed originals must not hide a still-open posting from another source.
    for group in identity_groups(db.execute('SELECT * FROM jobs')):
        primary = min(group, key=lambda r: (r['status'] != 'open', r['first_seen'], r['id']))['id']
        for row in group:
            value = primary if row['id'] != primary else None
            if row['duplicate_of'] != value:
                db.execute('UPDATE jobs SET duplicate_of=? WHERE id=?', (value, row['id']))


def resolve_url(db, fetcher, url, at):
    if urlsplit(url).hostname != 'go.vansh.nyc':
        return url
    cached = db.execute('SELECT resolved_url FROM url_resolutions WHERE url=?', (url,)).fetchone()
    if cached:
        return cached[0]
    error = None
    try:
        resolved = fetcher.resolve(url)
    except (OSError, ValueError) as exc:
        resolved, error = url, f'{type(exc).__name__}: {exc}'
    db.execute('INSERT INTO url_resolutions VALUES(?,?,?,?)', (url, resolved, at, error))
    return resolved
