# Secret-path protection for the public Graphiti MCP endpoint

Date: 2026-08-12
Status: Approved, not yet implemented
Repo: `bradykirk/graphiti` fork, `mcp_server/`

## Problem

The deployed MCP server answers at `https://graphiti.bradykirkpatrick.com/mcp` with no
authentication. Verified on 2026-08-12:

- A plain `POST /mcp` with `Accept: application/json, text/event-stream` returns a full
  MCP `initialize` handshake, with no credential.
- `/.well-known/oauth-protected-resource` returns 404, so the server advertises no auth.

Anyone who learns the hostname reaches the tools `add_memory`, `search_nodes`,
`delete_episode`, and `clear_graph`. That is full read, write, and destroy access to the graph.

## Constraint that drives the design

The endpoint must stay usable from two cloud clients:

- **Grok custom connector** — the dialog accepts a URL only. It offers no OAuth fields, no
  API-key field, and no custom-header field.
- **claude.ai custom connector** — the UI offers OAuth 2.0 fields only (authorization URL,
  token URL, client ID, client secret). There is no static bearer-token field outside a
  beta org-admin `static_headers` option.

Grok is the binding constraint. It can send no credential at all. Therefore the secret must
travel inside the URL path. Every scheme that needs a header or an OAuth flow is out of
scope for this iteration.

## Goal and non-goal

**Goal:** the endpoint becomes unreachable by hostname scanning, and the owner can revoke
access in one step.

**Non-goal:** per-user identity. This design does not prove *who* is calling. A real token
check becomes possible when Grok adds an auth field. That is a later spec.

## Design

### Placement

An ASGI middleware wrapping `streamable_http_app()`, added in `mcp_server/proxy_shim.py`.

Rationale for code over Traefik labels:

- It lives in git and ships inside the image, so a Coolify redeploy cannot drop it.
- Traefik labels live in the Coolify UI and are edited by hand.
- `proxy_shim.py` already monkey-patches the HTTP layer, so this matches the fork's pattern.

### Public URL shape

```
https://graphiti.bradykirkpatrick.com/s/<32-hex-secret>/mcp
```

32 hex characters carry 128 bits of entropy.

Use the form with **no trailing slash**. A trailing slash triggers the app's own 307
redirect. That redirect is what broke the first Grok connector attempt on 2026-08-12.

### Request handling

| Incoming path | Action |
| --- | --- |
| `/health` | Pass through unchanged, so the Coolify healthcheck keeps working. |
| `/s/<correct-secret>/...` | Strip the prefix, set `root_path`, pass to the app. |
| Anything else | Return 404, plain body, no detail. |

Three required properties:

1. **Timing-safe compare.** Use `hmac.compare_digest`. A byte-by-byte compare leaks the
   secret through response timing.
2. **404, not 403.** A 403 confirms to a scanner that a protected resource exists.
3. **Set `scope["root_path"] = "/s/<secret>"`** alongside the rewritten `path` and
   `raw_path`. Without it, any redirect the app generates rebuilds a `Location` that omits
   the secret prefix, so the client follows it to a 404.

### Configuration

One new environment variable, `MCP_URL_SECRET`, set in the Coolify deploy.

When it is unset, the middleware does not load and the server logs a warning at startup.
This fail-open choice keeps local development and the test suite working with the same
image. The Cloudflare rule below covers the deployed case if the variable is ever lost.

### Second layer at Cloudflare

Cloudflare fronts the deploy (`server: cloudflare` on every response). Add a WAF custom
rule that blocks `/mcp` and `/mcp/` at the edge. This holds even if a future deploy loses
`MCP_URL_SECRET`.

### Rotation

1. Generate a new 32-hex value.
2. Update `MCP_URL_SECRET` in Coolify and redeploy.
3. Paste the new URL into the Grok connector and the claude.ai connector.

The old URL stops working at redeploy.

## Testing

Unit tests against the middleware, no network needed:

- Correct prefix reaches the wrapped app, and the app sees `path == "/mcp"`.
- Wrong prefix returns 404.
- Missing prefix returns 404.
- `/health` returns 200 with no prefix.
- `root_path` is set to the prefix on a passing request.
- Middleware is absent when `MCP_URL_SECRET` is unset.

Post-deploy verification with curl:

```bash
curl -o /dev/null -w '%{http_code}\n' https://graphiti.bradykirkpatrick.com/mcp          # 404
curl -o /dev/null -w '%{http_code}\n' https://graphiti.bradykirkpatrick.com/s/wrong/mcp  # 404
curl -o /dev/null -w '%{http_code}\n' https://graphiti.bradykirkpatrick.com/health       # 200
curl -sS -X POST https://graphiti.bradykirkpatrick.com/s/<secret>/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'   # 200 handshake
```

Then reconnect both connectors and call one read tool from each.

## Accepted risk

The secret is a bearer token carried in a URL. Whoever holds the URL holds full access. It
is stored in Cloudflare request logs and inside each connector's saved configuration. The
mitigation is cheap rotation, described above.

This design defeats hostname scanning, which is the realistic threat to a public endpoint
that no attacker knows about yet. It does not defeat an attacker who reads the URL.

## Deploy note

Rebuilds of this image are not reproducible: the Docker build deletes `uv.lock` and
re-resolves dependencies. Check container startup logs after the redeploy.
