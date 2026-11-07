"""Deterministic screening hints; never legal eligibility determinations."""

import re
from html import unescape
from html.parser import HTMLParser


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'br', 'div', 'li', 'h1', 'h2', 'h3'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def strip_html(value: str) -> str:
    parser = TextParser()
    # Greenhouse may encode the entire markup once; parse after bounded unescape.
    parser.feed(unescape(value))
    parser.close()
    return ' '.join(''.join(parser.parts).split())


def contains(text: str, terms: list[str]) -> bool:
    return any(re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text, re.I) for term in terms)


US_STATES = [
    'Alabama',
    'Alaska',
    'Arizona',
    'Arkansas',
    'California',
    'Colorado',
    'Connecticut',
    'Delaware',
    'Florida',
    'Georgia',
    'Hawaii',
    'Idaho',
    'Illinois',
    'Indiana',
    'Iowa',
    'Kansas',
    'Kentucky',
    'Louisiana',
    'Maine',
    'Maryland',
    'Massachusetts',
    'Michigan',
    'Minnesota',
    'Mississippi',
    'Missouri',
    'Montana',
    'Nebraska',
    'Nevada',
    'Ohio',
    'Oklahoma',
    'Oregon',
    'Pennsylvania',
    'Tennessee',
    'Texas',
    'Utah',
    'Vermont',
    'Virginia',
    'Washington',
    'Wisconsin',
    'Wyoming',
    'New Hampshire',
    'New Jersey',
    'New Mexico',
    'New York',
    'North Carolina',
    'North Dakota',
    'Rhode Island',
    'South Carolina',
    'South Dakota',
    'West Virginia',
    'District of Columbia',
]
US_CODES = [
    'AL',
    'AK',
    'AZ',
    'AR',
    'CA',
    'CO',
    'CT',
    'DE',
    'FL',
    'GA',
    'HI',
    'ID',
    'IL',
    'IN',
    'IA',
    'KS',
    'KY',
    'LA',
    'ME',
    'MD',
    'MA',
    'MI',
    'MN',
    'MS',
    'MO',
    'MT',
    'NE',
    'NV',
    'NH',
    'NJ',
    'NM',
    'NY',
    'NC',
    'ND',
    'OH',
    'OK',
    'OR',
    'PA',
    'RI',
    'SC',
    'SD',
    'TN',
    'TX',
    'UT',
    'VT',
    'VA',
    'WA',
    'WV',
    'WI',
    'WY',
    'DC',
]
FOREIGN = (
    r'Canada|Ontario|Quebec|British Columbia|Toronto|Vancouver|Montreal|Ottawa|Waterloo|'
    r'United Kingdom|\bUK\b|London|Ireland|Dublin|Germany|Berlin|Munich|Spain|Madrid|Barcelona|'
    r'France|Paris|Netherlands|Amsterdam|Switzerland|Zurich|Poland|Warsaw|Sweden|Stockholm|'
    r'Denmark|Copenhagen|Italy|Portugal|Lisbon|Romania|Bucharest|Czech|Prague|Austria|Vienna|'
    r'Finland|Helsinki|Norway|Oslo|Belgium|Brussels|India|Bengaluru|Bangalore|Hyderabad|Mumbai|'
    r'Pune|Chennai|Delhi|Singapore|China|Beijing|Shanghai|Hong Kong|Taiwan|Taipei|Japan|Tokyo|'
    r'Korea|Seoul|Israel|Tel Aviv|Australia|Sydney|Melbourne|New Zealand|Brazil|(?<!New )Mexico|'
    r'Argentina|Chile|Colombia|EMEA|APAC|Qatar|Doha|United Arab Emirates|Dubai|Abu Dhabi|'
    r'Saudi Arabia|Riyadh|Bahrain|Kuwait|Oman|South Africa|Alberta|Calgary|Edmonton|Manitoba|'
    r'Winnipeg|Saskatchewan|Nova Scotia|Halifax|New Brunswick|Newfoundland|Prince Edward Island'
)


def clean_text(value):
    return ' '.join(re.sub('[\u200b-\u200d\ufeff]', '', strip_html(value)).split())


def location_parts(raw):
    return [
        p.strip(' ,')
        for p in re.split(
            r'\s*(?:;|\||/|•|\bor\b|\band\b)\s*|,\s*(?=[A-Z][a-z]+(?: [A-Z][a-z]+)*,\s*[A-Z]{2}\b)', raw
        )
    ]


def nyc(part):
    return bool(
        re.search(
            r'\b(?:New York(?: City)?|NYC|Brooklyn|Queens|Manhattan|Bronx|Staten Island)'
            r'(?:,\s*|\s+)(?:NY|New York)\b',
            part,
            re.I,
        )
        or re.search(r'\b(?:Jersey City|Hoboken|Newark)(?:,\s*|\s+)(?:NJ|New Jersey)\b', part, re.I)
        or re.search(
            r'(?:^|,)\s*(?:New York City|NYC|Brooklyn|Queens|Manhattan|Bronx|Staten Island)\s*(?:,|$)',
            part,
            re.I,
        )
        or re.fullmatch(
            r'(?:NYC|New York(?: City)?|Brooklyn|Queens|Manhattan|Bronx|Staten Island)', part, re.I
        )
    )


def country(raw):
    # Explicit country beats ambiguous state/province abbreviations (e.g. CA).
    parts = location_parts(raw)
    results = []
    for part in parts:
        if re.search(r'\b(?:non[- ]US|outside (?:the )?(?:US|United States))\b', part, re.I):
            results.append('non-US')
        elif re.search(r'\b(?:United States|USA|U\.?S\.?)\b', part, re.I) or nyc(part):
            results.append('US')
        elif re.search(r'(?<!\w)(?:' + FOREIGN + r')(?!\w)', part, re.I) or re.search(
            r',\s*(?:ON|BC|QC|AB|MB|SK|NS|NB|NL|PE|NT|NU|YT)\b', part
        ):
            results.append('non-US')
        elif any(re.search(r',\s*' + code + r'\b', part) for code in US_CODES) or contains(part, US_STATES):
            results.append('US')
        else:
            results.append('unknown')
    return 'US' if 'US' in results else 'non-US' if 'non-US' in results else 'unknown'


def office_mentions(description):
    """Office cities named in the description, for postings whose location metadata is non-geographic."""
    found = [
        m.group(1)
        for m in re.finditer(r'\bAvailable Locations\s+(.{1,200}?)\s+Available Terms\b', description)
    ]
    found += [
        m.group(1)
        for m in re.finditer(
            r'(?i:in[- ]office|on[- ]?site|hybrid)[^.]{0,60}?\b(?i:in|at)\s+'
            r"([A-Z][\w'-]*(?:,?\s+[A-Z][\w'-]*){0,3})",
            description,
        )
    ]
    return ' | '.join(dict.fromkeys(found))


def normalize_location(raw: str) -> str:
    normalized = []
    for part in location_parts(raw):
        if nyc(part):
            normalized.append('NYC metro')
        elif re.search(r'\bremote\b', part, re.I) and country(part) == 'US':
            normalized.append('Remote - US')
        else:
            normalized.append(part)
    return ' | '.join(dict.fromkeys(p for p in normalized if p)) or 'Unknown'


def restriction_note(text, rules):
    for note, pattern in rules['restriction_patterns']:
        candidate = text
        if note == 'clearance mentioned' and 'clearance' in text.lower():
            # Scan sentences once; a greedy whole-description substitution
            # repeatedly scanned long paragraphs even without clearance text.
            candidate = ' '.join(
                sentence
                for sentence in re.split(r'[.!?]', text)
                if not (
                    re.search(r'\bclearance\b', sentence, re.I)
                    and re.search(r'not required|not a requirement|optional', sentence, re.I)
                )
            )
        if re.search(pattern, candidate, re.I):
            return note
    return None


def season(title, description):
    text = title + '. ' + description
    positive = re.search(r'\bsummer\s+(?:of\s+)?2027\b', text, re.I)
    if not positive:
        positive = re.search(
            r'\bmay\b[^.!?()]{0,45}?(?:-|\u2013|—|to|through|until)\s*(?:aug(?:ust)?|sep(?:t(?:ember)?)?)\b[^.!?()]{0,20}?\b2027\b',
            text,
            re.I,
        )
    if not positive:
        positive = re.search(
            r'\b2027\b[^.!?]{0,45}\bmay\b[^.!?]{0,25}(?:-|\u2013|—|to|through)\s*aug(?:ust)?\b', text, re.I
        )
    other_pattern = r'\b(?:(?:fall|spring|autumn|winter)\s+(?:of\s+)?2027|summer\s+(?:of\s+)?2026)\b'
    title_terms = re.findall(r'\b(summer|fall|spring|autumn|winter)\s+(?:of\s+)?(20\d\d)\b', title, re.I)
    title_conflict = any(term.lower() != 'summer' or year != '2027' for term, year in title_terms)
    if positive is None:
        description_conflict = bool(re.search(other_pattern, description, re.I))
    else:
        # A Summer 2027 mention only in the description may be historical. If the
        # description names another season as the posting's term (not a graduation
        # date) and the title does not say Summer 2027, the term is not summer.
        in_title = bool(re.search(r'\bsummer\s+(?:of\s+)?2027\b', title, re.I))
        description_conflict = not in_title and any(
            not re.search(
                r'graduat|degree|class of|expected|commenc',
                description[description.rfind('.', 0, m.start()) + 1 : m.start()],
                re.I,
            )
            for m in re.finditer(other_pattern, description, re.I)
        )
    conflict = title_conflict or description_conflict
    evidence = positive.group(0) if positive else None
    return evidence, conflict


def classify(title: str, description: str, rules: dict, location: str = '') -> dict:
    title, description = clean_text(title), clean_text(description)
    text = title + '. ' + description
    internship = contains(title, rules['intern_keywords']) or (
        contains(title, ['summer associate', 'summer student']) and contains(description, ['internship'])
    )
    family = next((name for name, words in rules['family_keywords'].items() if contains(title, words)), None)
    software = contains(title, rules['software_keywords'])
    part_time = bool(re.search(r'\bpart[ -]time\b', text, re.I))
    if contains(title, rules['exclude_keywords']):
        role = 'excluded'
    elif family:
        role = family
    elif software:
        role = (
            'software_internship'
            if internship
            else 'software_part_time'
            if part_time
            else 'software_full_time'
        )
    else:
        role = 'other'
    phd = bool(
        re.search(r'\b(?:doctoral|doctorate)\b', text, re.I)
        or re.search(r'\b(?:ph[. ]?d\.?|doctoral|doctorate)\b', title, re.I)
        or re.search(
            r'\b(?:ph[. ]?d\.?|doctoral|doctorate)\s+(?:is\s+)?(?:required|student|degree)'
            r'|pursu\w*[^.!?]{0,90}\b(?:ph[. ]?d\.?|doctoral|doctorate)\b',
            description,
            re.I,
        )
    )
    masters = bool(re.search(r'\b(?:masters?|m\.?s\.?)\b', text, re.I))
    degree_note = (
        'high_school'
        if contains(title, ['high school', 'secondary school'])
        else 'phd'
        if phd and not masters
        else None
    )
    evidence, conflict = season(title, description)
    normalized, nation = normalize_location(location), country(location)
    if nation == 'unknown' and (inferred := office_mentions(description)) and country(inferred) != 'unknown':
        # Metadata such as "In-Office" names no place; the description's office mentions decide.
        normalized, nation = normalize_location(inferred), country(inferred)
    return dict(
        restriction_note=restriction_note(text, rules),
        role_family=role,
        matches_target=int(evidence is not None and not conflict),
        country=nation,
        degree_note=degree_note,
        season_evidence=evidence,
        season_conflict=int(conflict),
        part_time=int(part_time),
        location_normalized=normalized,
    )
