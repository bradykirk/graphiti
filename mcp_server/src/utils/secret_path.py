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
# blocking it would fail every deploy. It returns no graph data. /health/ is
# included too: before this middleware existed, Starlette 307-redirected
# /health/ to /health, and a healthcheck URL configured with the trailing
# slash must keep working.
PUBLIC_PATHS = frozenset({'/health', '/health/'})

# 128 bits of entropy at 32 hex characters is the design target (see
# docs/superpowers/specs/2026-08-12-mcp-secret-path-design.md). 16 characters
# is a floor well below that, meant only to reject obviously guessable values
# like "graphiti", not to certify a secret as strong.
MIN_SECRET_LENGTH = 16

_NOT_FOUND_BODY = b'Not Found'


class SecretPathMiddleware:
    """Require /s/<secret>/ on every request, then strip it."""

    def __init__(self, app, secret: str):
        if not secret:
            raise ValueError('secret must not be empty')
        self.app = app
        self.prefix = f'/s/{secret}'

    async def __call__(self, scope, receive, send):
        scope_type = scope.get('type')

        # Lifespan carries no path and MUST reach the app, or the session
        # manager never starts. Every other non-http scope is refused: a
        # websocket scope carries a path (the app registers no websocket
        # route today, so this has been unreachable in practice, not safe
        # by design), and any future scope type not handled here is refused
        # by default rather than passed through unchecked.
        if scope_type == 'lifespan':
            await self.app(scope, receive, send)
            return

        if scope_type == 'websocket':
            await send({'type': 'websocket.close', 'code': 1000})
            return

        if scope_type != 'http':
            return

        path = scope.get('path', '')

        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        if not self._prefix_matches(path):
            await self._not_found(send)
            return

        scope = dict(scope)
        # Under ASGI, root_path is a PREFIX OF path, not a substitute for it.
        # Starlette strips root_path off path to route, and builds redirect
        # Location headers from the full path. So leave path and raw_path alone:
        # rewriting them makes the 307 drop the secret and strand the client.
        scope['root_path'] = self.prefix

        await self.app(scope, receive, send)

    def _prefix_matches(self, path: str) -> bool:
        candidate = path[: len(self.prefix)]
        if len(candidate) != len(self.prefix):
            return False
        # Constant-time, so response timing does not leak the secret.
        # Encode with surrogatepass to handle decoded paths with surrogates.
        if not hmac.compare_digest(
            candidate.encode('utf-8', 'surrogatepass'),
            self.prefix.encode('utf-8'),
        ):
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

    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(f'{SECRET_ENV_VAR} must be at least {MIN_SECRET_LENGTH} characters')

    logger.info('Secret path protection is enabled.')
    return SecretPathMiddleware(app, secret)
