# MCP Secret-Path Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate the public Graphiti MCP endpoint behind an unguessable URL path prefix, so hostname scanning cannot reach the tools.

**Architecture:** A dependency-free ASGI middleware wraps the FastMCP streamable-HTTP app. It requires every request to arrive under `/s/<secret>/`, strips that prefix, and sets `root_path` so the app's own redirects keep the prefix. `/health` stays open for the Coolify healthcheck. Everything else gets a bare 404. The middleware is applied in `proxy_shim.py`, beside the existing uvicorn monkey-patch.

**Tech Stack:** Python 3.10+, `mcp` SDK 1.27.2 (`mcp.server.fastmcp`), uvicorn, pytest with `asyncio_mode = auto`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-12-mcp-secret-path-design.md`

## Global Constraints

- Working directory for every command is `mcp_server/`. There is no Makefile there; use `uv run ...`.
- Ruff: `line-length = 100`, `quote-style = "single"`, `indent-style = "space"`.
- Pyright: `typeCheckingMode = "basic"`, `pythonVersion = "3.10"`, includes `src` and `tests`.
- Add no new dependencies. The middleware uses only the standard library.
- Environment variable name is exactly `MCP_URL_SECRET`. It is a plain single-underscore variable read from `os.environ`, not a nested `__` config variable.
- Never log the secret value, in any branch, at any level.
- Tests import from `src/` because `tests/conftest.py` puts `src/` on `sys.path`.

---

### Task 1: Secret-path ASGI middleware

**Files:**
- Create: `mcp_server/src/utils/secret_path.py`
- Test: `mcp_server/tests/test_secret_path.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `SecretPathMiddleware(app: ASGIApp, secret: str)` — callable ASGI app; raises `ValueError` on an empty secret.
  - `wrap_with_secret_path(app: ASGIApp) -> ASGIApp` — reads `MCP_URL_SECRET`; returns `app` unchanged when unset; raises `ValueError` when the secret contains `/`.
  - `SECRET_ENV_VAR: str = 'MCP_URL_SECRET'`
  - Task 2 calls `wrap_with_secret_path` only.

- [ ] **Step 1: Write the failing test**

Create `mcp_server/tests/test_secret_path.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd mcp_server && uv run pytest tests/test_secret_path.py -v
```

Expected: collection error, `ModuleNotFoundError: No module named 'utils.secret_path'`.

- [ ] **Step 3: Write the implementation**

Create `mcp_server/src/utils/secret_path.py`:

```python
"""Gate every HTTP request behind an unguessable URL path prefix.

Grok's custom-connector dialog accepts a URL only. It offers no OAuth fields,
no API-key field, and no custom-header field. So the credential has to travel
inside the path. See docs/superpowers/specs/2026-08-12-mcp-secret-path-design.md.
"""

import hmac
import logging
import os

logger = logging.getLogger(__name__)

SECRET_ENV_VAR = 'MCP_URL_SECRET'

# Paths served without the secret. The Coolify healthcheck hits /health, and
# blocking it would fail every deploy. It returns no graph data.
PUBLIC_PATHS = frozenset({'/health'})

_NOT_FOUND_BODY = b'Not Found'


class SecretPathMiddleware:
    """Require /s/<secret>/ on every request, then strip it."""

    def __init__(self, app, secret: str):
        if not secret:
            raise ValueError('secret must not be empty')
        self.app = app
        self.prefix = f'/s/{secret}'

    async def __call__(self, scope, receive, send):
        # Lifespan and websocket scopes carry no path. Lifespan in particular
        # MUST reach the app, or the session manager never starts.
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return

        path = scope.get('path', '')

        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        if not self._prefix_matches(path):
            await self._not_found(send)
            return

        remainder = path[len(self.prefix) :] or '/'
        scope = dict(scope)
        scope['path'] = remainder
        scope['raw_path'] = remainder.encode()
        # root_path makes the app rebuild absolute URLs with the prefix intact,
        # so its 307 redirect does not strand the client on a 404.
        scope['root_path'] = self.prefix

        await self.app(scope, receive, send)

    def _prefix_matches(self, path: str) -> bool:
        candidate = path[: len(self.prefix)]
        if len(candidate) != len(self.prefix):
            return False
        # Constant-time, so response timing does not leak the secret.
        if not hmac.compare_digest(candidate, self.prefix):
            return False
        rest = path[len(self.prefix) :]
        return rest == '' or rest.startswith('/')

    async def _not_found(self, send):
        # 404 rather than 403: a 403 confirms to a scanner that something is here.
        await send(
            {
                'type': 'http.response.start',
                'status': 404,
                'headers': [
                    (b'content-type', b'text/plain; charset=utf-8'),
                    (b'content-length', str(len(_NOT_FOUND_BODY)).encode()),
                ],
            }
        )
        await send({'type': 'http.response.body', 'body': _NOT_FOUND_BODY})


def wrap_with_secret_path(app):
    """Wrap app when MCP_URL_SECRET is set; otherwise return it unchanged."""
    secret = os.environ.get(SECRET_ENV_VAR, '').strip()

    if not secret:
        logger.warning(
            '%s is not set. The MCP endpoint is reachable by anyone who knows the host.',
            SECRET_ENV_VAR,
        )
        return app

    if '/' in secret:
        raise ValueError(f'{SECRET_ENV_VAR} must not contain "/"')

    logger.info('Secret path protection is enabled.')
    return SecretPathMiddleware(app, secret)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd mcp_server && uv run pytest tests/test_secret_path.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Format, lint, type-check**

```bash
cd mcp_server && uv run ruff format src/utils/secret_path.py tests/test_secret_path.py
cd mcp_server && uv run ruff check src/utils/secret_path.py tests/test_secret_path.py
cd mcp_server && uv run pyright src/utils/secret_path.py
```

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add mcp_server/src/utils/secret_path.py mcp_server/tests/test_secret_path.py
git commit -m "feat(mcp): add secret URL-path middleware"
```

---

### Task 2: Apply the middleware in proxy_shim

**Files:**
- Modify: `mcp_server/proxy_shim.py:15-26`

**Interfaces:**
- Consumes: `wrap_with_secret_path` from Task 1.
- Produces: nothing that later tasks read.

- [ ] **Step 1: Edit the uvicorn config**

In `mcp_server/proxy_shim.py`, replace the body of `_run_streamable_http_async` with:

```python
async def _run_streamable_http_async(self):
    import uvicorn

    # Imported here, not at module scope: sys.path only gains /app/mcp/src
    # further down this file, and this function runs after that.
    from utils.secret_path import wrap_with_secret_path

    config = uvicorn.Config(
        wrap_with_secret_path(self.streamable_http_app()),
        host=self.settings.host,
        port=self.settings.port,
        log_level=self.settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get('FORWARDED_ALLOW_IPS', '*'),
    )
    await uvicorn.Server(config).serve()
```

Also extend the module docstring's first paragraph with one sentence:

```
It also gates every request behind an unguessable path prefix when
MCP_URL_SECRET is set.
```

- [ ] **Step 2: Verify the module still imports the patched function**

```bash
cd mcp_server && uv run python -c "
import ast, pathlib
tree = ast.parse(pathlib.Path('proxy_shim.py').read_text())
fn = [n for n in tree.body if getattr(n, 'name', None) == '_run_streamable_http_async'][0]
src = ast.unparse(fn)
assert 'wrap_with_secret_path' in src, 'middleware not applied'
assert 'proxy_headers=True' in src, 'proxy fix lost'
print('proxy_shim OK')
"
```

Expected: `proxy_shim OK`.

This check parses the file instead of importing it, because importing `proxy_shim` constructs the full FastMCP server and needs database and API credentials.

- [ ] **Step 3: Format and lint**

```bash
cd mcp_server && uv run ruff format proxy_shim.py && uv run ruff check proxy_shim.py
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add mcp_server/proxy_shim.py
git commit -m "feat(mcp): gate the streamable HTTP app behind the secret path"
```

---

### Task 3: Local end-to-end check against a real ASGI app

**Files:**
- Test: `mcp_server/tests/test_secret_path.py` (append)

**Interfaces:**
- Consumes: `SecretPathMiddleware` from Task 1.
- Produces: nothing.

This task proves the middleware works against a real Starlette router, not only a stub. Starlette arrives with the `mcp` SDK, so no new dependency is added.

- [ ] **Step 1: Write the failing test**

Append to `mcp_server/tests/test_secret_path.py`:

```python
async def test_starlette_router_redirect_keeps_the_secret_prefix():
    """A trailing slash triggers Starlette's 307. The Location must keep the prefix.

    Without root_path the redirect points at /mcp, which the middleware then
    404s -- the exact failure mode this design exists to avoid.
    """
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def endpoint(request):
        return PlainTextResponse('ok')

    inner = Starlette(routes=[Route('/mcp', endpoint, methods=['POST'])])
    app = SecretPathMiddleware(inner, SECRET)

    sent = await call(app, f'{PREFIX}/mcp/')

    assert status_of(sent) == 307
    location = dict(sent[0]['headers'])[b'location'].decode()
    assert location.endswith(f'{PREFIX}/mcp')
```

- [ ] **Step 2: Run it**

```bash
cd mcp_server && uv run pytest tests/test_secret_path.py::test_starlette_router_redirect_keeps_the_secret_prefix -v
```

Expected: PASS. If it fails with a `location` of `/mcp`, `root_path` is not being applied in Task 1 — fix `secret_path.py`, do not weaken the test.

- [ ] **Step 3: Run the whole file**

```bash
cd mcp_server && uv run pytest tests/test_secret_path.py -v
```

Expected: 12 passed.

- [ ] **Step 4: Commit**

```bash
git add mcp_server/tests/test_secret_path.py
git commit -m "test(mcp): cover redirect prefix retention via a real router"
```

---

### Task 4: Document the variable and ship

**Files:**
- Modify: `mcp_server/README.md`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Add a section to `mcp_server/README.md`**

Place it after the existing configuration or environment section:

```markdown
### Protecting a public HTTP endpoint

Set `MCP_URL_SECRET` to an unguessable value, for example 32 hex characters
from `openssl rand -hex 16`. The server then serves MCP only at:

    https://<host>/s/<MCP_URL_SECRET>/mcp

Use the form with no trailing slash. `/health` stays reachable without the
secret so container healthchecks keep working. Every other path returns 404.

When `MCP_URL_SECRET` is unset the server logs a warning and stays open, which
suits local development. Do not leave it unset on a public host.

To rotate the secret: set a new value, redeploy, then update the URL in every
connected client. The old URL stops working at redeploy.
```

- [ ] **Step 2: Run the full mcp_server suite**

```bash
cd mcp_server && uv run pytest tests/ -k "not _int and not integration and not live" -v
```

Expected: no new failures against the pre-change baseline. Record the baseline first if you have not: stash the changes, run the same command, note the result, unstash.

- [ ] **Step 3: Commit**

```bash
git add mcp_server/README.md
git commit -m "docs(mcp): document MCP_URL_SECRET"
```

- [ ] **Step 4: Push and let CI build the image**

```bash
git push bradykirk main
```

The workflow `.github/workflows/build-mcp-image.yml` triggers on pushes to `main` touching `mcp_server/**`. It rebuilds `ghcr.io/bradykirk/graphiti-mcp` and updates the `:proxy-fix` tag.

Watch it:

```bash
gh run watch
```

- [ ] **Step 5: Redeploy in Coolify with force pull**

`MCP_URL_SECRET` is already set in Coolify. Confirm the name matches exactly before redeploying.

Check the container startup logs. Rebuilds re-resolve dependencies because the Docker build deletes `uv.lock`, so a new dependency version can break startup. Look for the line `Secret path protection is enabled.` If you instead see the `is not set` warning, the variable name does not match.

- [ ] **Step 6: Verify the live endpoint**

```bash
SECRET=<the value from Coolify>
HOST=https://graphiti.bradykirkpatrick.com

curl -o /dev/null -w 'bare mcp: %{http_code}\n'  "$HOST/mcp"
curl -o /dev/null -w 'wrong:    %{http_code}\n'  "$HOST/s/00000000000000000000000000000000/mcp"
curl -o /dev/null -w 'health:   %{http_code}\n'  "$HOST/health"
curl -sS -X POST "$HOST/s/$SECRET/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' | head -5
```

Expected: `404`, `404`, `200`, then a `serverInfo` handshake naming `Graphiti Agent Memory`.

- [ ] **Step 7: Update both connectors**

Paste `https://graphiti.bradykirkpatrick.com/s/<secret>/mcp` into the Grok custom connector and the claude.ai custom connector. Both are broken until you do. Then call one read tool from each to confirm.

- [ ] **Step 8: Add the Cloudflare WAF rule**

Create a custom rule that blocks requests where the URI path equals `/mcp` or `/mcp/`. This keeps the bare path dead even if a future deploy loses the environment variable.

---

## Rollback

Unset `MCP_URL_SECRET` in Coolify and redeploy. The middleware then does not load and the old `/mcp` URL works again. No image rebuild is needed for a rollback.
