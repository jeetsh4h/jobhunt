import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path):
    shutil.copyfile(ROOT / 'config.example.yaml', tmp_path / 'config.yaml')
    shutil.copyfile(ROOT / 'filters.yaml', tmp_path / 'filters.yaml')
    (tmp_path / 'companies.toml').write_text("""[[companies]]
name = "Example"
source = "greenhouse"
token = "example"
""")
    return tmp_path


def example_settings():
    """Tests never read the developer's own config.yaml."""
    return json.loads((ROOT / 'config.example.yaml').read_text())


def job(identifier=1, **changes):
    result = {
        'id': identifier,
        'title': 'Software Engineering Intern, Summer 2027',
        'absolute_url': f'https://example.invalid/jobs/{identifier}',
        'location': {'name': 'New York, NY'},
        'content': '<p>Build useful software.</p>',
    }
    result.update(changes)
    return result


class FixtureFetcher:
    def __init__(self, rows=None, error=None):
        self.rows = rows if rows is not None else [job()]
        self.error = error

    def get(self, source, token):
        if self.error:
            raise self.error
        return {'jobs': self.rows}


def db_for(project):
    from jobhunt.db import connect

    return connect(project / 'data/jobhunt.sqlite3')


def snapshot(root):
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob('*')
        if p.is_file()
    }


@pytest.fixture
def cli(project, monkeypatch):
    from jobhunt.cli import main

    def run(*args):
        monkeypatch.setattr('sys.argv', ['jobhunt', '--root', str(project), *args])
        return main()

    return run


FIXTURES = ROOT / 'tests/fixtures'


class ListFetcher(FixtureFetcher):
    def __init__(self, board_rows=None, **payloads):
        super().__init__(board_rows)
        self.payloads = {
            'simplify': json.loads((FIXTURES / 'simplify_list.json').read_text()),
            'vanshb03': json.loads((FIXTURES / 'vanshb03_list.json').read_text()),
            'speedyapply': (FIXTURES / 'speedyapply_list.md').read_text(),
        }
        self.payloads.update(payloads)

    def get(self, source, token):
        return self.payloads[source] if source in self.payloads else super().get(source, token)


@pytest.fixture
def listed(project):
    (project / 'companies.toml').write_text(
        (project / 'companies.toml').read_text()
        + ''.join(f'\n[[lists]]\nsource = "{n}"\n' for n in ('simplify', 'vanshb03', 'speedyapply'))
    )
    return project
