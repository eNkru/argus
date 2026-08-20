# Deferred work

Features intentionally out of scope for v1, documented so the design stays
extensible.

## Persistent sessions

A `POST /v1/sessions` / `GET /v1/sessions` / `DELETE /v1/sessions/{id}` surface
backed by long-lived `BrowserContext` objects keyed by id, with cookie
export/import endpoints (`/v1/sessions/{id}/cookies`). This lets a caller log in
*through* argus once and reuse the authenticated session across fetches, instead
of passing the cookie jar on every request.

**Why deferred:** the optional `cookies` field on `/v1/fetch` already covers the
Taobao login-gated use case (caller exports their jar once and passes it each
time). Persistent sessions add lifecycle, idle-teardown, and storage-state
persistence complexity that isn't justified until a caller actually wants argus
to *own* the login flow rather than just transport authenticated requests.

**Design hook when built:** the per-request ephemeral context in
`routes/fetch.py` is the template; a `SessionRegistry` would hold live contexts
by id, `BrowserManager.ensure_browser` stays shared, and `add_cookies` /
`new_context` move from per-request to per-session.

## Per-session proxy

A `proxy` field on session create so different callers can route through
different egress IPs (the main lever for not getting IP-banned across many
clients). Depends on persistent sessions above.

## Rate limiting per token

A per-token token-bucket in `auth.py` to protect the single shared browser from
a noisy client. Currently `ARGUS_CONCURRENCY` bounds total concurrency globally;
per-token quotas would add fairness.

## Metrics endpoint

`GET /metrics` (Prometheus) exposing fetch count / latency histogram /
consecutive-failure gauge. The `FailureTracker` already holds the counter.
