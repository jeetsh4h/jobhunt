"""Deterministic shortlist selection shared by CLI and digest."""

from datetime import UTC, datetime, timedelta


def since_time(value):
    import re

    if match := re.fullmatch(r'(\d+)([dh])', value):
        amount = int(match[1])
        delta = timedelta(days=amount) if match[2] == 'd' else timedelta(hours=amount)
        return (datetime.now(UTC) - delta).isoformat()
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (
        result.replace(tzinfo=UTC).isoformat()
        if result.tzinfo is None
        else result.astimezone(UTC).isoformat()
    )


def shortlist(db, limit=25, since=None, *, preferred=()):
    from .queue import queue

    return queue(db, limit=limit, since=since, preferred=preferred)
