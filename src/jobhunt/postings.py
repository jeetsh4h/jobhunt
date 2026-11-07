"""Public posting identities and USD base-pay normalization."""

import math
import re
from urllib.parse import parse_qs, urlsplit

from .classify import strip_html

TOKEN = r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*'
IDENTIFIER = r'[A-Za-z0-9_-]+'
MONEY = r'([\d,]+(?:\.\d+)?)\s*([kK])?'
UNIT = r'(hourly|hours?|hrs?|weekly|weeks?|monthly|months?|annually|annual|years?|yr)'
PAY = re.compile(
    r'(?<![A-Za-z])(?:USD\s*|US\s*)?\$\s*' + MONEY + r'\s*(?:USD\s*)?'
    r'(?:(?:-|\u2013|—|to)\s*\$?\s*' + MONEY + r'\s*(?:USD\s*)?)?'
    r'\s*(?:/|per\s+|an?\s+)?' + UNIT + r'\b',
    re.I,
)


def interval_divisor(unit):
    unit = re.sub(r'^1\s+', '', unit.lower().strip())
    if unit in ('hour', 'hours', 'hr', 'hrs', 'hourly'):
        return 1
    if unit in ('week', 'weeks', 'weekly'):
        return 40
    if unit in ('month', 'months', 'monthly'):
        return 173
    if unit in ('year', 'years', 'yr', 'annual', 'annually', 'yearly'):
        return 2080
    return None


def normalize_pay(low, high, currency, unit):
    divisor = interval_divisor(unit)
    if currency.upper() != 'USD' or divisor is None:
        return None
    if isinstance(low, bool) or isinstance(high, bool):
        return None
    if low is None:
        low = high
    if high is None:
        high = low
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    if not (math.isfinite(low) and math.isfinite(high)) or low < 0 or high < low:
        return None
    return (low + high) / 2 / divisor


def hourly_pay(text):
    """Only explicit dollar amounts with a period; bare bonuses are not wages."""
    values = []
    for match in PAY.finditer(text or ''):
        context = (text or '')[max(0, match.start() - 8) : match.end() + 8]
        if re.search(r'CAD|AUD|NZD|SGD|HKD|(?<![A-Za-z])(?:C|A|S)\$', context, re.I):
            continue
        low = float(match[1].replace(',', '')) * (1000 if match[2] else 1)
        high = float(match[3].replace(',', '')) * (1000 if match[4] else 1) if match[3] else low
        if match[4] and not match[2] and low < 1000:
            low *= 1000
        value = normalize_pay(low, high, 'USD', match[5])
        if value is not None:
            values.append(value)
    return max(values, default=None)


def ats_posting(url):
    """Parse hosted ATS URLs; custom company sites remain unidentified."""
    parts = urlsplit(url)
    host, path = (parts.hostname or '').lower(), parts.path.strip('/')
    if host in ('boards.greenhouse.io', 'job-boards.greenhouse.io'):
        match = re.fullmatch(f'({TOKEN})/jobs/({IDENTIFIER})(?:/application)?', path)
        if match:
            return ('greenhouse', *match.groups())
        query = parse_qs(parts.query)
        if re.fullmatch(TOKEN, path) and query.get('gh_jid', [''])[0].isdigit():
            return 'greenhouse', path, query['gh_jid'][0]
    if host == 'jobs.lever.co':
        match = re.fullmatch(f'({TOKEN})/({IDENTIFIER})(?:/apply)?', path)
        if match:
            return ('lever', *match.groups())
    if host == 'jobs.ashbyhq.com':
        match = re.fullmatch(f'({TOKEN})/({IDENTIFIER})(?:/application)?', path)
        if match:
            return ('ashby', *match.groups())
    return None


def ats_type(url):
    if posting := ats_posting(url):
        return posting[0]
    host = (urlsplit(url).hostname or '').lower()
    for suffix, name in (
        ('myworkdayjobs.com', 'workday'),
        ('myworkdaysite.com', 'workday'),
        ('icims.com', 'icims'),
        ('avature.net', 'avature'),
        ('taleo.net', 'taleo'),
        ('successfactors.com', 'successfactors'),
        ('successfactors.eu', 'successfactors'),
        ('successfactorsjobs.com', 'successfactors'),
        ('sapsf.com', 'successfactors'),
        ('sapsf.eu', 'successfactors'),
        ('sapsf.cn', 'successfactors'),
    ):
        if host == suffix or host.endswith('.' + suffix):
            return name
    return 'unknown'


def posting_content(source, payload, identifier):
    from .feeds import mapping, sequence, text

    row = mapping(payload)
    if row.get('error') or row.get('errors') or row.get('success') is False:
        raise ValueError('posting error envelope')
    if source == 'ashby':
        row = next(
            (mapping(j) for j in sequence(row.get('jobs')) if str(mapping(j).get('id')) == identifier), None
        )
        if row is None:
            raise ValueError('posting ID missing from Ashby board')
    elif str(row.get('id')) != identifier:
        raise ValueError('posting ID mismatch')
    if source == 'greenhouse':
        description = strip_html(text(row.get('content', '')))
    elif source == 'lever':
        parts = [
            text(row.get('descriptionPlain') or row.get('description', '')),
            text(row.get('additionalPlain') or row.get('additional', '')),
            text(row.get('salaryDescriptionPlain') or row.get('salaryDescription', '')),
        ]
        for item in sequence(row.get('lists', [])):
            item = mapping(item)
            parts += [text(item.get('text', '')), text(item.get('content', ''))]
        description = strip_html(' '.join(parts))
    else:
        description = strip_html(text(row.get('descriptionPlain') or row.get('descriptionHtml', '')))
    pays = []
    if source == 'lever' and row.get('salaryRange'):
        pay = mapping(row['salaryRange'])
        pays.append(
            normalize_pay(
                pay.get('min'), pay.get('max'), text(pay.get('currency', '')), text(pay.get('interval', ''))
            )
        )
    if source == 'ashby' and row.get('compensation'):
        compensation = mapping(row['compensation'])
        components = list(sequence(compensation.get('summaryComponents', [])))
        for tier in sequence(compensation.get('compensationTiers', [])):
            components += sequence(mapping(tier).get('components', []))
        for component in components:
            c = mapping(component)
            if c.get('compensationType') == 'Salary':
                pays.append(
                    normalize_pay(
                        c.get('minValue'),
                        c.get('maxValue'),
                        text(c.get('currencyCode') or ''),
                        text(c.get('interval') or ''),
                    )
                )
    if source == 'greenhouse':
        for item in sequence(row.get('pay_input_ranges', [])):
            pay = mapping(item)
            blurb = pay.get('blurb')
            label = text(pay.get('title', '')) + ' ' + text('' if blurb is None else blurb)
            unit = re.search(UNIT, label, re.I)
            low, high = pay.get('min_cents'), pay.get('max_cents')
            if isinstance(low, (int, float)) and isinstance(high, (int, float)) and unit:
                pays.append(
                    normalize_pay(low / 100, high / 100, text(pay.get('currency_type') or ''), unit[0])
                )
    # Explicit non-USD structured salary must not become USD through a bare '$' description.
    known = [p for p in pays if p is not None]
    if known:
        return description, max(known)
    if source == 'lever' and (row.get('salaryRange') or {}).get('currency') not in (None, '', 'USD'):
        return description, None
    if source == 'ashby' and row.get('compensation') and components:
        salaries = [c for c in components if c.get('compensationType') == 'Salary']
        if salaries and all(c.get('currencyCode') != 'USD' for c in salaries):
            return description, None
    return description, hourly_pay(description)
