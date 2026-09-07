---
name: psych-mcp-oauth
description: >-
  Authorize Psych against an MCP server protected by OAuth 2.1: `psych_runtime.McpOAuth`
  on the Spec, wiring `OAuthClient` onto `McpPool`, client_credentials versus
  authorization_code, dynamic client registration, PKCE, scope step-up on a 403,
  and the `AuthorizationRedirectPort` a consumer implements because Psych runs no
  browser and no callback listener. Use whenever an MCP server returns 401 or 403
  to Psych, someone asks how to authenticate a Psych MCP connection, mentions
  WWW-Authenticate, insufficient_scope, protected resource metadata or dynamic
  registration in a Psych context, or hits ReauthorizationRequired or
  StepUpExhausted. Read before wiring OAuth, because an acquired token changes a
  connection's pool key and getting that wrong reintroduces the tenant leak that
  keying by URL causes.
---

# OAuth 2.1 for MCP

Psych ships a full OAuth 2.1 client that the MCP client drives automatically on
a 401 or 403 from a protected server.

## Configure the server

```python
psych_runtime.McpServer(
    name="github",
    url="https://mcp.example.com/github",
    oauth=psych_runtime.McpOAuth(
        grant="client_credentials",  # or "authorization_code"
        preregistered_client_id=None,
        client_secret_credential="github-oauth-secret",  # a NAME, never a secret
        cimd_url=None,
        allow_dynamic_registration=True,
        application_type="native",
        client_name="psych",
        redirect_uris=(),
    ),
)
```

The config lives per server rather than per pool, so one Spec naming two servers
behind two different authorization servers under two different grants works.
`client_secret_credential` is a credential **name** resolved through your
`SecretResolver`, exactly like `McpServer.credential`, so nothing secret lands
in an exported Version.

## Wire the client onto the pool

```python
from psych_runtime.tools.mcp import McpPool
from psych_runtime.tools.oauth import OAuthClient

pool = McpPool(transport=transport, secrets=secrets, oauth=OAuthClient(...))
```

Without an `OAuthClient`, a server behind OAuth still fails on its first 401
with `McpServerUnreachable`, except the message now says the server wants OAuth
rather than only naming the status code.

## What happens automatically

| Trigger | Call | Retried |
|---|---|---|
| 401 with `WWW-Authenticate: Bearer` | `OAuthClient.start` | Once |
| 403 naming `insufficient_scope` | `OAuthClient.step_up` | Once |
| Every ordinary request | `OAuthClient.bearer_token` first | Refreshes before it can 401 |

Concurrent callers for the same key share one refresh rather than racing.
`evict(scope, resource=...)` forgets a session outside the normal expiry path.

All four calls take a plain `resource` string, the MCP server's URL, and
canonicalise it internally. No caller has to track an issuer.

## The pool key moves when a token is acquired

Acquiring a token during `connect()` changes the connection's credential
identity, and therefore its pool key, from whatever was resolved beforehand
(`None`, for a server with no static credential) to the OAuth grant's identity.
`McpPool.get_or_connect` **moves** the connection to that new key rather than
leaving it under the old one, where a future unauthenticated lookup could still
find it. That would be the same isolation mistake as pooling by URL, aimed at
one tenant's own two requests instead of two tenants'.

A refresh or a step-up never moves the key: both replace the token in place
without changing the grant's identity, so a connection stays pooled where it is
for as long as it lives.

The same discipline applies to registrations and sessions: never key one by less
than `(tenant, principal, issuer/resource)`.

## Two grants

**`client_credentials`** is the default and needs no browser. A consumer using
only this never implements `AuthorizationRedirectPort` at all.

**`authorization_code`** needs one HTTP hop through a browser, and Psych owns no
HTTP server, so it does not run that hop. `AuthorizationRedirectPort` is the
seam you implement over your existing web app.
`InMemoryAuthorizationRedirect` is a test-only stand-in that performs the hop
itself over a real loopback socket. PKCE is generated and required, and the
authorization response's issuer is validated rather than trusted.

## Discovery and registration

Protected resource metadata and authorization server metadata are fetched from
their well-known URLs. When the server supports it and
`allow_dynamic_registration` is true, a client is registered dynamically; set
`preregistered_client_id` to use one you already have.

Psych serves no Client ID Metadata Document. `cimd_url` points at one you host.

## Errors worth recognising

`OAuthError` is the base. The ones that usually mean a configuration problem
rather than a transient one: `DiscoveryError`, `UnsupportedGrant`,
`ClientRegistrationError`, `IssuerMismatch`, `InvalidCanonicalUri`,
`PkceRequired`. The ones that mean the flow needs a person:
`AuthorizationDenied`, `ReauthorizationRequired`, `StepUpExhausted`,
`NoActiveSession`.

## Gotchas

- **Never put a token in a Spec.** Only credential names.
- **`grant` defaults to `client_credentials`.** If your server needs a user's
  own consent, set `authorization_code` and implement the redirect port.
- **Adding an `oauth` field changes the Version hash,** as any Spec field does.
  That is correct: an agent that authenticates differently is a different agent.
