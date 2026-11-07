import os
import shutil
import subprocess
import sys

import pytest
from conftest import ROOT


@pytest.mark.parametrize('failure', ['', 'ingest', 'enrich'])
def test_watch_publishes_queue_and_digest_despite_source_failures(tmp_path, failure):
    (tmp_path / 'scripts').mkdir()
    script = tmp_path / 'scripts/fresh-watch'
    shutil.copyfile(ROOT / 'scripts/fresh-watch', script)
    script.chmod(0o755)
    (tmp_path / 'bin').mkdir()
    launcher = tmp_path / 'bin/jobhunt'
    launcher.write_text(
        f'#!{sys.executable}\n'
        + """import os
from pathlib import Path
import sys
if sys.argv[1] == 'queue':
    print('Example internship queue')
if sys.argv[1] == 'digest':
    Path('digest.md').write_text('Fresh (posted in the last 48h)\\n')
raise SystemExit(1 if sys.argv[1] == os.environ['WATCH_TEST_FAILURE'] else 0)
"""
    )
    launcher.chmod(0o755)
    result = subprocess.run(
        [str(script)],
        env={**os.environ, 'WATCH_TEST_FAILURE': failure},
        cwd=tmp_path.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (1 if failure else 0)
    assert 'Example internship queue' in result.stdout
    assert (tmp_path / 'digest.md').read_text().startswith('Fresh (posted in the last 48h)')


@pytest.mark.skipif(shutil.which('systemd-analyze') is None, reason='systemd not installed')
def test_timer_calendar_runs_once_daily_at_nine_local():
    timer = (ROOT / 'systemd/jobhunt.timer').read_text()
    calendar = next(line.split('=', 1)[1] for line in timer.splitlines() if line.startswith('OnCalendar='))
    result = subprocess.run(
        ['systemd-analyze', 'calendar', '--iterations=3', '--base-time=2026-11-06 00:00:00', calendar],
        env={**os.environ, 'TZ': 'America/New_York', 'LC_ALL': 'C'},
        capture_output=True,
        text=True,
        check=True,
    )
    # Ask systemd itself to expand the actual unit's expression.
    import re

    occurrences = re.findall(
        r'(?:Next elapse:|Iteration #\d+:)\s+\w+ (\d{4}-\d{2}-\d{2}) (\d\d:\d\d:\d\d)', result.stdout
    )
    assert occurrences == [(f'2026-11-{day:02}', '09:00:00') for day in (6, 7, 8)]
