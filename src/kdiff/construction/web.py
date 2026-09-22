"""Public-source capture with pinned DNS, redirect checks and exact evidence spans."""

import http.client
import ipaddress
import os
import socket
import ssl
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, parse_qsl
from urllib.robotparser import RobotFileParser

from pydantic import Field

from kdiff.core.contracts import Contract, TimeRange, digest
from kdiff.construction.sources import BatchBuilder


class FetchPolicy(Contract):
    allowed_hosts: list[str] = Field(min_length=1)
    max_calls: int = Field(default=10, ge=1, le=100)
    max_bytes: int = Field(default=4 * 1024 * 1024, ge=1, le=16 * 1024 * 1024)
    timeout: float = Field(default=15, gt=0, le=60)
    min_interval: float = Field(default=1, ge=0.1, le=60)
    max_redirects: int = Field(default=3, ge=0, le=5)
    max_seconds: float = Field(default=60, ge=1, le=300)
    user_agent: str = 'KnowledgeDiffusionResearch/0.2'


def public_destination(url, hosts, resolver=socket.getaddrinfo):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or not parts.hostname or parts.hostname.lower() not in hosts
            or parts.username or parts.password or parts.fragment or parts.port not in {None, 443}):
        raise ValueError('Source URL is outside the approved HTTPS scope')
    host = parts.hostname.lower()
    if any(key.lower() in {'token', 'api_key', 'apikey', 'access_token', 'password', 'signature', 'sig'}
           for key, _ in parse_qsl(parts.query)):
        raise ValueError('Source credentials must not appear in stored URLs')
    addresses = {info[4][0] for info in resolver(host, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in addresses):
        raise ValueError('Source resolved to a non-public destination')
    return parts, sorted(addresses)[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        # Do not resolve the hostname a second time after validating its addresses.
        sock = socket.create_connection((self.ip, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class PublicFetcher:
    def __init__(self, policy: FetchPolicy):
        self.policy, self.calls, self.last = policy, 0, 0.0
        self.robots = {}
        self.deadline = time.monotonic() + policy.max_seconds

    def _request(self, url, accept, authorization=None):
        if self.calls >= self.policy.max_calls:
            raise ValueError('Source request budget exhausted')
        remaining = self.deadline - time.monotonic()
        if remaining <= self.policy.min_interval:
            raise ValueError('Source wall-clock budget exhausted')
        parts, ip = public_destination(url, {x.lower() for x in self.policy.allowed_hosts})
        time.sleep(max(0, self.policy.min_interval - (time.monotonic() - self.last)))
        self.calls += 1
        self.last = time.monotonic()
        connection = PinnedHTTPS(parts.hostname, ip, min(self.policy.timeout, remaining))
        try:
            headers = {'User-Agent': self.policy.user_agent, 'Accept': accept, 'Accept-Encoding': 'identity'}
            if authorization:
                headers['Authorization'] = authorization
            connection.request('GET', parts.path or '/' if not parts.query else (parts.path or '/') + '?' + parts.query, headers=headers)
            response = connection.getresponse()
            chunks, size = [], 0
            while size <= self.policy.max_bytes:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('Source wall-clock budget exhausted')
                if connection.sock:
                    connection.sock.settimeout(min(self.policy.timeout, remaining))
                chunk = response.read1(min(65536, self.policy.max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b''.join(chunks)
            if len(data) > self.policy.max_bytes:
                raise ValueError('Source response byte budget exhausted')
            return response.status, dict((k.lower(), v) for k, v in response.getheaders()), data
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError('Public source unavailable') from None
        finally:
            connection.close()

    def get(self, url, *, accept='text/html,application/pdf', credential_env=None, honor_robots=True):
        original_host = urlsplit(url).hostname
        authorization = None
        if credential_env:
            token = os.environ.get(credential_env)
            if not token:
                raise ValueError('Source credential is not configured')
            authorization = 'Bearer ' + token
        for _ in range(self.policy.max_redirects + 1):
            parts, _ = public_destination(url, {x.lower() for x in self.policy.allowed_hosts})
            if parts.hostname != original_host and authorization:
                raise ValueError('Authenticated redirects cannot change host')
            if honor_robots:
                origin = f'https://{parts.hostname}'
                if origin not in self.robots:
                    status, headers, data = self._request(origin + '/robots.txt', 'text/plain')
                    if status == 404:
                        data = b'User-agent: *\nAllow: /'
                    elif status != 200:
                        raise ValueError('Cannot establish public crawling permission')
                    robot = RobotFileParser()
                    robot.parse(data.decode('utf-8', errors='replace').splitlines())
                    self.robots[origin] = robot
                robot = self.robots[origin]
                if not robot.can_fetch(self.policy.user_agent, url):
                    raise ValueError('Source access restrictions disallow this URL')
                delay = robot.crawl_delay(self.policy.user_agent)
                if delay and delay > self.policy.min_interval:
                    if delay > self.deadline - time.monotonic():
                        raise ValueError('Required source crawl delay exceeds remaining budget')
                    time.sleep(max(0, delay - (time.monotonic() - self.last)))
            status, headers, data = self._request(url, accept, authorization)
            if status in {301, 302, 303, 307, 308}:
                if not headers.get('location'):
                    raise ValueError('Redirect has no location')
                url = urljoin(url, headers['location'])
                continue
            if status != 200:
                raise RuntimeError(f'Source unavailable, HTTP status {status}')
            if headers.get('content-encoding', 'identity') != 'identity':
                raise ValueError('Unexpected encoded source body')
            return {'url': url, 'content_type': headers.get('content-type', '').split(';')[0], 'data': data}
        raise ValueError('Source redirect budget exceeded')


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def capture_document(data, mime, url, store):
    if len(data) > 4 * 1024 * 1024:
        raise ValueError('Document exceeds bounded extraction limit')
    raw = store.capture(data)
    if mime == 'text/html':
        parser = HTMLText()
        parser.feed(data.decode('utf-8', errors='strict'))
        text = '\n'.join(parser.parts)
    elif mime == 'application/pdf':
        import pymupdf
        with pymupdf.open(stream=data, filetype='pdf') as pdf:
            if pdf.page_count > 100:
                raise ValueError('PDF page budget exceeded')
            text = '\n'.join(f'[page {i+1}]\n{page.get_text()}' for i, page in enumerate(pdf))
    elif mime == 'text/plain':
        text = data.decode('utf-8')
    else:
        raise ValueError('Unsupported public document MIME type')
    if len(text) > 100000:
        raise ValueError('Document text budget exceeded')
    text_id = store.put_bytes(text.encode())
    return {**raw, 'url': url, 'mime': mime, 'text_artifact': text_id,
            'transform': 'document-text-v1', 'status': 'complete_for_declared_scope'}


class SpanEntity(Contract):
    key: str
    kind: str
    name: str
    subtype: str | None = None
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class SpanFact(Contract):
    head: str
    relation: str
    tail: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str
    # Dates must be attested verbatim within the cited span.
    date_text: str | None = None
    observation_kind: str = 'other'
    qualifiers: dict = Field(default_factory=dict)


class SpanExtraction(Contract):
    entities: list[SpanEntity] = Field(max_length=100)
    facts: list[SpanFact] = Field(max_length=200)


def validate_extraction(document, extraction: SpanExtraction, store, namespace):
    text = store.get_bytes(document['text_artifact']).decode()
    b = BatchBuilder(namespace, 'web', store)
    b.record({}, document['url'], raw=store.get_bytes(document['sha256']))
    ids = {}
    for entity in extraction.entities:
        if entity.key in ids or text[entity.start:entity.end] != entity.name:
            raise ValueError('Entity name is not an exact source span or key is duplicated')
        identity = digest([entity.kind, entity.name, entity.start, entity.end])
        ids[entity.key] = b.entity(entity.kind, document['url'] + '#' + identity,
            {'name' if entity.kind in {'Author', 'Institution', 'Topic'} else 'title': entity.name},
            f'text:{document["text_artifact"]}:{entity.start}:{entity.end}', subtype=entity.subtype)
    for fact in extraction.facts:
        if text[fact.start:fact.end] != fact.quote or not fact.quote:
            raise ValueError('Claimed quotation is absent from captured source')
        if fact.head not in ids or fact.tail not in ids:
            raise ValueError('Unknown extracted endpoint')
        names = {e.key: e.name for e in extraction.entities}
        if names[fact.head] not in fact.quote or names[fact.tail] not in fact.quote:
            raise ValueError('Relation span must mention both endpoints')
        if fact.date_text and fact.date_text not in fact.quote:
            raise ValueError('Claimed date absent from source span')
        if fact.relation == 'hasSite' and fact.observation_kind != 'operation':
            raise ValueError('Operational site requires an operational evidence assertion')
        qualifiers = {**fact.qualifiers, 'text_artifact': document['text_artifact'], 'quote': fact.quote}
        b.edge(ids[fact.head], fact.relation, ids[fact.tail],
               f'text:{document["text_artifact"]}:{fact.start}:{fact.end}',
               TimeRange.parse(fact.date_text), fact.observation_kind, qualifiers)
    # Text transform is a replay operand in addition to original document bytes.
    if document['text_artifact'] != document['sha256']:
        b.sources[document['text_artifact']] = {**store.capture(text.encode()), 'source': 'web_text',
            'source_record_id': document['url'], 'derived_from': document['sha256'],
            'transform': document['transform'], 'synthetic': namespace.startswith('fixture:')}
    return b.finish()
