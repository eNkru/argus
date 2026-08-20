# Argus API specification (v1)

Base URL: `http://<host>:8000`

## Authentication

All `/v1/*` routes require HTTP Bearer auth:

```
Authorization: Bearer <token>
```

`<token>` must be one of the comma-separated values in `ARGUS_API_TOKENS`.
`/health` is unauthenticated. Set `ARGUS_AUTH_DISABLED=true` to bypass (dev).

## OpenAPI 3.1

```yaml
openapi: 3.1.0
info:
  title: Argus
  description: General-purpose anti-detect browser fetch service.
  version: 1.0.0

components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      bearerFormat: API token
  schemas:
    Cookie:
      type: object
      required: [name, value, domain, path]
      properties:
        name:     { type: string }
        value:    { type: string }
        domain:   { type: string, description: "e.g. '.taobao.com'" }
        path:     { type: string, default: "/" }
        httpOnly: { type: boolean, default: false }
        secure:   { type: boolean, default: false }
        sameSite: { type: string, enum: [Strict, Lax, None], default: Lax }

paths:
  /health:
    get:
      summary: Service readiness
      security: []
      responses:
        "200":
          description: Service is ready. The browser may be lazy/absent by design.
          content:
            application/json:
              schema:
                type: object
                properties:
                  status:   { type: string, example: "ok" }
                  browser:  { type: string, enum: [ready, absent] }

  /v1/fetch:
    post:
      summary: Fetch a page's rendered HTML
      security: [bearerAuth: []]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [url]
              properties:
                url:            { type: string, format: uri }
                cookies:
                  type: array
                  description: "Cookies injected into the ephemeral context before navigation. Omit for login-free sites."
                  items: { $ref: "#/components/schemas/Cookie" }
                waitUntil:      { type: string, enum: [domcontentloaded, load, networkidle], default: domcontentloaded }
                renderWaitMs:   { type: integer, default: 8000 }
                timeoutMs:      { type: integer, default: 35000 }
                detectBlocked:  { type: boolean, default: true, description: "Run the blocked-signature registry on returned HTML" }
                locale:         { type: string, description: "Optional context locale, e.g. 'en-US', 'zh-CN'" }
                userAgent:      { type: string, description: "Optional context user-agent override (see fingerprint caveat in architecture.md)" }
      responses:
        "200":
          description: Never throws. ok or structured failure.
          content:
            application/json:
              schema:
                oneOf:
                  - type: object
                    properties:
                      ok:   { const: true }
                      html: { type: string }
                      url:  { type: string, format: uri }
                  - type: object
                    properties:
                      ok:        { const: false }
                      reason:    { type: string, enum: [blocked, fetch_failed] }
                      signature: { type: string, description: "Present only when reason=blocked and detectBlocked=true" }
                      retryable: { type: boolean, description: "Present only when reason=blocked; whether a fresh fetch can plausibly pass this block" }

  /v1/fetch-image:
    post:
      summary: Fetch a binary image through the browser
      security: [bearerAuth: []]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [url]
              properties:
                url:       { type: string, format: uri }
                cookies:
                  type: array
                  items: { $ref: "#/components/schemas/Cookie" }
                timeoutMs: { type: integer, default: 35000 }
                locale:    { type: string }
                userAgent: { type: string }
      responses:
        "200":
          content:
            application/json:
              schema:
                oneOf:
                  - type: object
                    properties:
                      ok:          { const: true }
                      contentType: { type: string }
                      data:        { type: string, description: "base64-encoded image bytes" }
                  - type: object
                    properties:
                      ok:     { const: false }
                      reason: { type: string, enum: [fetch_failed, non_image] }
```

## Cookie shape

Each cookie mirrors the object a browser devtools "copy all cookies" produces
(and the shape `BrowserContext.add_cookies` accepts), so a Taobao cookie jar can
be pasted verbatim:

```json
[
  { "name": "login5",    "value": "u%3D...", "domain": ".taobao.com", "path": "/", "httpOnly": true,  "secure": true, "sameSite": "Lax" },
  { "name": "_tb_token_", "value": "e3b0...", "domain": ".taobao.com", "path": "/" }
]
```

`httpOnly`, `secure`, `sameSite` are optional (default `false`, `false`, `Lax`).
`name`, `value`, `domain`, `path` are required.
