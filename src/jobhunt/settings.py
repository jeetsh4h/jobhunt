"""JSON syntax is a strict YAML subset, avoiding a YAML dependency."""

import json
import tomllib
from pathlib import Path


def config(root: Path) -> dict:
    path = root / 'config.yaml'
    if not path.exists():
        raise ValueError(f'{path} missing: copy config.example.yaml to config.yaml')
    result = json.loads(path.read_text())
    if not isinstance(result['paused'], bool):
        raise ValueError('config paused must be true or false')
    places = result.get('preferred_locations', [])
    if not isinstance(places, list) or not all(isinstance(place, str) and place.strip() for place in places):
        raise ValueError('preferred_locations must be a list of nonempty text values')
    target = result.get('weekly_application_target', 15)
    if not isinstance(target, int) or isinstance(target, bool) or target < 0:
        raise ValueError('weekly_application_target must be a non-negative integer')
    return result


def ensure_active(root: Path) -> None:
    if config(root)['paused'] or (root / 'PAUSE').exists():
        raise Paused('paused: use resume; ingest made no further mutations')


class Paused(RuntimeError):
    pass


def registry(root: Path, source: str | None = None) -> list[dict]:
    entries = tomllib.loads((root / 'companies.toml').read_text()).get('companies', [])
    return [entry for entry in entries if source is None or entry['source'] == source]


def filters(root: Path) -> dict:
    return json.loads((root / 'filters.yaml').read_text())


def list_registry(root: Path, source: str | None = None) -> list[dict]:
    """Public GitHub lists (`[[lists]]` in companies.toml); no token, one fixed file each."""
    entries = tomllib.loads((root / 'companies.toml').read_text()).get('lists', [])
    return [entry for entry in entries if source is None or entry['source'] == source]
