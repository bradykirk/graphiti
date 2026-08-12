"""Monkey-patch FastMCP's streamable HTTP runner to honor X-Forwarded-* headers,
then hand off to the unmodified graphiti_mcp_server.main().

Needed because FastMCP hardcodes uvicorn.Config(...) without proxy_headers, so
behind an HTTPS reverse proxy (Traefik, Cloudflare) uvicorn emits redirects
with scheme=http and the client refuses to follow the downgrade.
It also gates every request behind an unguessable path prefix when
MCP_URL_SECRET is set.
"""

import os
import sys

from mcp.server.fastmcp import FastMCP


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


FastMCP.run_streamable_http_async = _run_streamable_http_async

sys.path.insert(0, '/app/mcp')
sys.path.insert(0, '/app/mcp/src')

# Importing graphiti_mcp_server triggers FastMCP() construction, which
# auto-enables DNS-rebinding protection with allowed_hosts limited to
# localhost because the module instantiates FastMCP without passing host=.
# Later it sets mcp.settings.host = "0.0.0.0", binding 0.0.0.0 BUT leaves
# the rebinding filter intact — so any Host: other than localhost gets 421.
# Turn it off so public traffic (Host: graphiti.example.com) is accepted.
import graphiti_mcp_server  # noqa: E402

_ts = graphiti_mcp_server.mcp.settings.transport_security
if _ts is not None:
    _ts.enable_dns_rebinding_protection = False

if __name__ == '__main__':
    graphiti_mcp_server.main()
