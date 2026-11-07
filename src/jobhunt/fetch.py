"""GET-only public transport. No followed redirects, proxies or credentials."""

import json
import re
import signal
import ssl
import time
import urllib.error
import urllib.request
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from urllib.parse import urljoin, urlsplit

HOSTS = {'greenhouse': 'boards-api.greenhouse.io', 'lever': 'api.lever.co', 'ashby': 'api.ashbyhq.com'}
# Public GitHub lists, fetched as raw files (repo, branch, path, kind). Never other URLs.
LISTS = {
    'simplify': ('SimplifyJobs/Summer2027-Internships', 'dev', '.github/scripts/listings.json', 'json'),
    'vanshb03': ('vanshb03/Summer2027-Internships', 'dev', '.github/scripts/listings.json', 'json'),
    'speedyapply': ('speedyapply/2027-SWE-College-Jobs', 'main', 'README.md', 'text'),
}
RAW_HOST = 'raw.githubusercontent.com'
USER_AGENT = 'jobhunt/0.1 (supervised local internship research; public job-board JSON only)'
MAX_BYTES = 64 * 1024 * 1024


def endpoint(source: str, token: str) -> str:
    if source in LISTS:
        repo, branch, path, _ = LISTS[source]
        return f'https://{RAW_HOST}/{repo}/{branch}/{path}'
    if not re.fullmatch(r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*', token):
        raise ValueError('invalid board token')
    if source == 'greenhouse':
        return f'https://{HOSTS[source]}/v1/boards/{token}/jobs?content=true'
    if source == 'lever':
        return f'https://{HOSTS[source]}/v0/postings/{token}?mode=json'
    if source == 'ashby':
        return f'https://{HOSTS[source]}/posting-api/job-board/{token}'
    raise ValueError('unsupported source')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, capture=False):
        self.capture = capture

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.capture:
            raise ValueError(f'redirect refused: HTTP {code}')
        return None  # urllib exposes the unfollowed redirect as HTTPError


class ResponseDeadline(TimeoutError):
    pass


@contextmanager
def response_deadline(seconds):
    # Linux runner: an alarm bounds DNS, headers, slow-drip reads and retries,
    # rather than resetting the timeout for every socket operation.
    def expired(signum, frame):
        raise ResponseDeadline('total response deadline exceeded')

    handler = signal.signal(signal.SIGALRM, expired)
    started = time.monotonic()
    previous = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, handler)
        if previous[0]:
            signal.setitimer(
                signal.ITIMER_REAL, max(0.001, previous[0] - (time.monotonic() - started)), previous[1]
            )


def retry_after(headers, fallback):
    value = headers.get('Retry-After', '')
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except ValueError, TypeError:
            delay = fallback
    return max(0, min(30, delay))


class Fetcher:
    def __init__(self, settings: dict):
        self.interval = max(1.0, float(settings['per_host_interval_seconds']))
        self.timeout = min(60.0, max(1.0, float(settings['request_timeout_seconds'])))
        self.attempts = min(3, max(1, int(settings['request_attempts'])))
        self.deadline = min(60.0, max(0.01, float(settings.get('response_deadline_seconds', 60))))
        self.last: dict[str, float] = {}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.redirect_opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(capture=True)
        )

    def get(self, source: str, token: str):
        with response_deadline(self.deadline):
            return self._get(source, token)

    def _get(self, source: str, token: str):
        url = endpoint(source, token)
        host = RAW_HOST if source in LISTS else HOSTS[source]
        kind = LISTS[source][3] if source in LISTS else 'json'
        return self._request(url, host, kind, source in LISTS)

    def _request(self, url, host, kind='json', compressed=False):
        end = time.monotonic() + self.deadline
        for attempt in range(self.attempts):
            if time.monotonic() >= end:
                raise ResponseDeadline('total response deadline exceeded')
            # Ingest runs are serialized. Initial delay also separates requests
            # from the previous process when runs start immediately back-to-back.
            wait = (
                self.interval
                if host not in self.last
                else self.interval - (time.monotonic() - self.last[host])
            )
            if wait > 0:
                time.sleep(wait)
            self.last[host] = time.monotonic()
            headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
            if compressed:
                headers['Accept-Encoding'] = 'gzip'  # the 13 MB Simplify file is ~1.7 MB compressed
            request = urllib.request.Request(url, headers=headers, method='GET')
            delay = min(2**attempt, 4)
            try:
                with self.opener.open(
                    request, timeout=min(self.timeout, max(0.001, end - time.monotonic()))
                ) as response:
                    if response.status != 200:
                        raise ValueError(f'expected HTTP 200; got {response.status}')
                    if (
                        kind == 'json'
                        and not compressed
                        and 'json' not in response.headers.get('Content-Type', '').lower()
                    ):
                        raise ValueError('response is not JSON')
                    body = response.read(MAX_BYTES + 1)
                    if len(body) > MAX_BYTES:
                        raise ValueError('response exceeds 64 MiB bound')
                    if response.headers.get('Content-Encoding', '').lower() == 'gzip':
                        try:
                            inflater = zlib.decompressobj(31)
                            body = inflater.decompress(body, MAX_BYTES + 1)
                        except zlib.error as exc:
                            raise ValueError(f'bad gzip body: {exc}') from None
                        if len(body) > MAX_BYTES or inflater.unconsumed_tail:
                            raise ValueError('response exceeds 64 MiB bound')
                    if time.monotonic() >= end:
                        raise ResponseDeadline('total response deadline exceeded')
                    return body.decode('utf-8') if kind == 'text' else json.loads(body)
            except ResponseDeadline:
                raise
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.attempts - 1:
                    raise
                delay = retry_after(exc.headers, delay)
            except urllib.error.URLError as exc:
                # urllib wraps OSError subclasses raised while opening headers.
                # An expired total deadline must not become another retry.
                if isinstance(exc.reason, ResponseDeadline):
                    raise exc.reason from exc
                if attempt == self.attempts - 1:
                    raise
            except TimeoutError, ssl.SSLError, IncompleteRead, ConnectionResetError:
                if attempt == self.attempts - 1:
                    raise
            if time.monotonic() >= end:
                raise ResponseDeadline('total response deadline exceeded')
            time.sleep(min(delay, end - time.monotonic()))
        raise RuntimeError('retry bound reached')

    def posting(self, source, token, identifier):
        url = endpoint(source, token)  # validates source and board token
        if not re.fullmatch(r'[A-Za-z0-9_-]+', identifier):
            raise ValueError('invalid posting ID')
        if source == 'greenhouse':
            url = f'https://{HOSTS[source]}/v1/boards/{token}/jobs/{identifier}?pay_transparency=true'
        elif source == 'lever':
            url = f'https://{HOSTS[source]}/v0/postings/{token}/{identifier}'
        elif source == 'ashby':
            url += '?includeCompensation=true'
        else:
            raise ValueError('unsupported posting source')
        with response_deadline(self.deadline):
            return self._request(url, HOSTS[source])

    def resolve(self, url):
        """One request to the fixed shortener; never follow the Location URL."""
        parts = urlsplit(url)
        if (
            parts.scheme != 'https'
            or parts.netloc != 'go.vansh.nyc'
            or not re.fullmatch(r'/[A-Za-z0-9_-]+', parts.path)
        ):
            raise ValueError('unsupported short URL')
        with response_deadline(self.deadline):
            wait = self.interval - (time.monotonic() - self.last.get(parts.netloc, 0))
            if wait > 0:
                time.sleep(wait)
            self.last[parts.netloc] = time.monotonic()
            request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT}, method='GET')
            try:
                with self.redirect_opener.open(request, timeout=self.timeout):
                    return url
            except urllib.error.HTTPError as exc:
                try:
                    if exc.code not in (301, 302, 303, 307, 308):
                        raise
                    if not exc.headers.get('Location'):
                        raise ValueError('redirect missing Location')
                    target = urljoin(url, exc.headers['Location'])
                    final = urlsplit(target)
                    if (
                        final.scheme not in ('http', 'https')
                        or not final.hostname
                        or final.username
                        or final.password
                    ):
                        raise ValueError('invalid redirect Location')
                    return target
                finally:
                    exc.close()
