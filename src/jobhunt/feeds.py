"""Validate complete feed shapes before any snapshot reaches SQLite."""

from datetime import UTC, datetime
from urllib.parse import urlsplit

from .classify import strip_html


def mapping(value):
    if not isinstance(value, dict):
        raise ValueError('expected job metadata object')
    return value


def text(value):
    if not isinstance(value, str):
        raise ValueError('expected job text string')
    return value


def sequence(value):
    if not isinstance(value, list):
        raise ValueError('expected job metadata list')
    return value


def date_value(value):
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, UTC).isoformat()
        except OverflowError, OSError, ValueError:
            raise ValueError('invalid job timestamp') from None
    if not isinstance(value, str):
        raise ValueError('invalid job timestamp')
    datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value


def parse(source: str, payload) -> list[dict]:
    if source not in ('lever', 'greenhouse', 'ashby'):
        raise ValueError('unsupported source')
    if isinstance(payload, dict) and (
        any(k in payload for k in ('error', 'errors')) or payload.get('success') is False
    ):
        raise ValueError('feed error envelope rejected')
    if source == 'lever':
        rows = payload
    else:
        if not isinstance(payload, dict) or 'jobs' not in payload:
            raise ValueError('feed missing jobs snapshot')
        rows = payload['jobs']
    if not isinstance(rows, list):
        raise ValueError('jobs snapshot must be a list')
    if source == 'greenhouse' and mapping(payload.get('meta', {})).get('total', len(rows)) != len(rows):
        raise ValueError('incomplete Greenhouse snapshot: meta.total mismatch')
    result, seen = [], set()
    for row in rows:
        row = mapping(row)
        if source == 'greenhouse':
            identifier, title, url = row.get('id'), row.get('title'), row.get('absolute_url')
            # Greenhouse's public API uses numeric IDs; normalize those once.
            if isinstance(identifier, int) and not isinstance(identifier, bool):
                identifier = str(identifier)
            location = mapping(row.get('location', {})).get('name', '')
            description = row.get('content', '')
            posted, updated = row.get('first_published'), row.get('updated_at')
        elif source == 'lever':
            identifier, title, url = row.get('id'), row.get('text'), row.get('hostedUrl')
            categories = mapping(row.get('categories', {}))
            locations = categories.get('allLocations') or [categories.get('location', '')]
            location = ' | '.join(text(loc) for loc in sequence(locations))
            description = text(row.get('descriptionPlain') or row.get('description', ''))
            for item in sequence(row.get('lists', [])):
                item = mapping(item)
                description += ' ' + text(item.get('text', '')) + ' ' + text(item.get('content', ''))
            description += ' ' + text(row.get('additionalPlain') or row.get('additional', ''))
            posted, updated = row.get('createdAt'), row.get('updatedAt')
        else:
            identifier, title, url = row.get('id'), row.get('title'), row.get('jobUrl')
            locations = [row.get('location', '')] + [
                mapping(loc).get('location', '') for loc in sequence(row.get('secondaryLocations', []))
            ]
            location = ' | '.join(text(loc) for loc in locations)
            if row.get('isRemote') is not None and not isinstance(row['isRemote'], bool):
                raise ValueError('invalid isRemote boolean')
            if row.get('isRemote') and 'remote' not in location.lower():
                location = 'Remote - ' + location
            description = row.get('descriptionPlain') or row.get('descriptionHtml', '')
            posted, updated = row.get('publishedAt'), row.get('updatedAt')
        if not isinstance(identifier, str) or not identifier.strip() or identifier in seen:
            raise ValueError('missing/string job identity or duplicate ID')
        title, url = text(title), text(url)
        parsed_url = urlsplit(url)
        if not strip_html(title) or parsed_url.scheme not in ('http', 'https') or not parsed_url.hostname:
            raise ValueError('missing required title or invalid job URL')
        seen.add(identifier)
        result.append(
            dict(
                requisition_id=identifier,
                title=strip_html(title),
                url=url,
                location_raw=text(location),
                description_text=strip_html(text(description)),
                posted_at=date_value(posted),
                updated_at=date_value(updated),
            )
        )
    return result
