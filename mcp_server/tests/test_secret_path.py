"""Tests for the secret URL-path gate.

Drives the middleware as a raw ASGI callable, so no HTTP server or FastMCP
import is needed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from utils.secret_path import (  # noqa: E402
    MIN_SECRET_LENGTH,
    SECRET_ENV_VAR,
    SecretPathMiddleware,
    wrap_with_secret_path,
)

SECRET = 'a1b2c3d4e5f60718293a4b5c6d7e8f90'
PREFIX = f'/s/{SECRET}'


def make_inner_app(seen: list):
    """An ASGI app that records the scope it was called with and returns 200."""

    async def app(scope, receive, send):
        seen.append(scope)
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    return app


async def call(app, path: str):
    """Drive one HTTP request through an ASGI app; return the sent messages."""
    scope = {
        'type': 'http',
        'method': 'POST',
        'path': path,
        'raw_path': path.encode(),
        'headers': [],
    }
    sent = []

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def status_of(sent: list) -> int:
    return sent[0]['status']


async def test_correct_prefix_reaches_app_with_full_path():
    """ASGI requires root_path to prefix path, so path is passed through whole."""
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    sent = await call(app, f'{PREFIX}/mcp')

    assert status_of(sent) == 200
    assert len(seen) == 1
    assert seen[0]['path'] == f'{PREFIX}/mcp'
    assert seen[0]['raw_path'] == f'{PREFIX}/mcp'.encode()


async def test_root_path_is_set_so_redirects_keep_the_prefix():
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    await call(app, f'{PREFIX}/mcp')

    assert seen[0]['root_path'] == PREFIX


async def test_wrong_secret_returns_404_and_never_calls_the_app():
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    sent = await call(app, '/s/00000000000000000000000000000000/mcp')

    assert status_of(sent) == 404
    assert seen == []


async def test_bare_mcp_path_returns_404():
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    sent = await call(app, '/mcp')

    assert status_of(sent) == 404
    assert seen == []


async def test_prefix_must_end_on_a_segment_boundary():
    """A path that merely starts with the secret must not pass."""
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    sent = await call(app, f'{PREFIX}extra/mcp')

    assert status_of(sent) == 404
    assert seen == []


async def test_non_ascii_in_path_returns_404():
    """Non-ASCII in path must not raise; must return 404."""
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    sent = await call(app, '/s/a1b2c3d4e5f60718293a4b5c6d7e8f9é/mcp')

    assert status_of(sent) == 404
    assert seen == []


async def test_health_is_reachable_without_the_prefix():
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    sent = await call(app, '/health')

    assert status_of(sent) == 200
    assert seen[0]['path'] == '/health'


async def test_health_with_trailing_slash_is_reachable_without_the_prefix():
    """Before this middleware existed, /health/ 307-redirected to /health.
    A healthcheck URL configured with the trailing slash must still work."""
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    sent = await call(app, '/health/')

    assert status_of(sent) == 200
    assert seen[0]['path'] == '/health/'


async def test_path_exactly_equal_to_prefix_reaches_app():
    """No remainder after the prefix still passes the segment-boundary check."""
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    sent = await call(app, PREFIX)

    assert status_of(sent) == 200
    assert seen[0]['path'] == PREFIX


async def test_lifespan_scope_passes_through():
    """The app must still start. Blocking lifespan would break the server."""
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    async def receive():
        return {'type': 'lifespan.startup'}

    async def send(message):
        pass

    await app({'type': 'lifespan'}, receive, send)

    assert len(seen) == 1
    assert seen[0]['type'] == 'lifespan'


async def test_websocket_scope_is_refused_and_never_reaches_the_app():
    """A websocket scope carries a path, unlike lifespan, so it must be
    refused explicitly rather than passed through with the other non-http
    scopes. The wrapped app registers no websocket route, so this must
    close the connection without ever calling the app."""
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    scope = {
        'type': 'websocket',
        'path': f'{PREFIX}/mcp',
        'raw_path': f'{PREFIX}/mcp'.encode(),
        'headers': [],
    }
    sent = []

    async def receive():
        return {'type': 'websocket.connect'}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)

    assert seen == []
    assert sent == [{'type': 'websocket.close', 'code': 1000}]


async def test_starlette_router_redirect_keeps_the_secret_prefix():
    """A trailing slash triggers Starlette's 307. The Location must keep the prefix."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def endpoint(request):
        return PlainTextResponse('ok')

    inner = Starlette(routes=[Route('/mcp', endpoint, methods=['POST'])])
    app = SecretPathMiddleware(inner, SECRET)

    scope = {
        'type': 'http',
        'method': 'POST',
        'path': f'{PREFIX}/mcp/',
        'raw_path': f'{PREFIX}/mcp/'.encode(),
        'query_string': b'',
        'scheme': 'http',
        'server': ('testserver', 80),
        'headers': [],
    }
    sent = []

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)

    assert status_of(sent) == 307
    location = dict(sent[0]['headers'])[b'location'].decode()
    assert location.endswith(f'{PREFIX}/mcp')


def test_empty_secret_is_rejected():
    with pytest.raises(ValueError):
        SecretPathMiddleware(make_inner_app([]), '')


def test_wrap_returns_app_unchanged_when_env_var_is_unset(monkeypatch):
    monkeypatch.delenv(SECRET_ENV_VAR, raising=False)
    inner = make_inner_app([])

    assert wrap_with_secret_path(inner) is inner


def test_wrap_wraps_when_env_var_is_set(monkeypatch):
    monkeypatch.setenv(SECRET_ENV_VAR, SECRET)
    inner = make_inner_app([])

    wrapped = wrap_with_secret_path(inner)

    assert isinstance(wrapped, SecretPathMiddleware)


def test_wrap_rejects_a_secret_containing_a_slash(monkeypatch):
    monkeypatch.setenv(SECRET_ENV_VAR, 'bad/secret')

    with pytest.raises(ValueError):
        wrap_with_secret_path(make_inner_app([]))


def test_wrap_rejects_a_secret_shorter_than_the_minimum(monkeypatch):
    monkeypatch.setenv(SECRET_ENV_VAR, 'a' * (MIN_SECRET_LENGTH - 1))

    with pytest.raises(ValueError):
        wrap_with_secret_path(make_inner_app([]))


def test_wrap_accepts_a_secret_of_exactly_the_minimum_length(monkeypatch):
    monkeypatch.setenv(SECRET_ENV_VAR, 'a' * MIN_SECRET_LENGTH)
    inner = make_inner_app([])

    wrapped = wrap_with_secret_path(inner)

    assert isinstance(wrapped, SecretPathMiddleware)
