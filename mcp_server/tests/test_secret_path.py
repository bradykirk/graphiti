"""Tests for the secret URL-path gate.

Drives the middleware as a raw ASGI callable, so no HTTP server or FastMCP
import is needed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from utils.secret_path import (  # noqa: E402
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


async def test_correct_prefix_reaches_app_with_prefix_stripped():
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    sent = await call(app, f'{PREFIX}/mcp')

    assert status_of(sent) == 200
    assert len(seen) == 1
    assert seen[0]['path'] == '/mcp'
    assert seen[0]['raw_path'] == b'/mcp'


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


async def test_health_is_reachable_without_the_prefix():
    seen = []
    app = SecretPathMiddleware(make_inner_app(seen), SECRET)

    sent = await call(app, '/health')

    assert status_of(sent) == 200
    assert seen[0]['path'] == '/health'


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
