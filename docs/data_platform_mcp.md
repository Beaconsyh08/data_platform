# External assistants through MCP

The platform exposes `/mcp` using stateless Streamable HTTP, with JSON responses and protocol
versions 2025-03-26, 2025-06-18 and 2025-11-25. GET/SSE and DELETE/session termination return 405;
there are no server-initiated requests or model sampling calls. Codex and Claude Code provide their
own model connection and pay their own conversational inference costs. Never send model API keys
to the platform. Dataset processing and any model calls inside a submitted operator still use the
platform executor and its configured resources.

## Access

Use a reachable HTTPS platform origin and trust its CA through the client/operating system trust
store. An internal address requires LAN/VPN connectivity. Development and production have separate
accounts, credentials and data scopes. The CLI installer includes the platform CA for CLI requests;
other MCP clients need to trust that CA separately.

With CLI 0.3.18 or later, create a separate credential for each client:

```bash
data-platform mcp-token --server https://platform.example.com --username YOUR_ACCOUNT \
  --token-name my-codex --token-file "$HOME/.config/data-platform-mcp.token"
```

The parent directory must already exist. The CLI prompts for the platform password, logs out after
issuance, writes the credential to a new mode-0600 file, and never prints it. It refuses to overwrite
an existing file. Credentials default to 30 days; `--token-days` accepts 1–90. The default scopes are
`read` and `prepare`; use `--read-only` for a Viewer account or a client that should only read.
A credential cannot submit, confirm, approve, delete, run shell commands, read server files, or
access arbitrary SQL. Maximum 20 unexpired, unrevoked credentials per account.

```bash
export DATA_PLATFORM_TOKEN="$(cat "$HOME/.config/data-platform-mcp.token")"
```

Keep this environment variable out of shared configuration, logs and version control. Launch the
client from this environment. Model credentials remain in its existing model/provider setup.

Codex configuration:

```toml
[mcp_servers.data_platform]
url = "https://platform.example.com/mcp"
bearer_token_env_var = "DATA_PLATFORM_TOKEN"
```

Claude Code `.mcp.json`:

```json
{
  "mcpServers": {
    "data_platform": {
      "type": "http",
      "url": "https://platform.example.com/mcp",
      "headers": {"Authorization": "Bearer ${DATA_PLATFORM_TOKEN}"}
    }
  }
}
```

This release uses manually provisioned bearer credentials. OAuth discovery/login and Claude web or
Desktop connector acceptance are not implemented. Do not run `codex mcp login` for this endpoint.

List credential metadata or revoke one:

```bash
data-platform mcp-tokens --server https://platform.example.com --username YOUR_ACCOUNT
data-platform mcp-revoke --server https://platform.example.com --username YOUR_ACCOUNT \
  --credential-id CREDENTIAL_UUID
```

Credential hashes, expiry, scopes and owner are persisted in the additive `dp_mcp_credentials` table.
Account disabling, password replacement, credential expiry/revocation and current dataset grants
are checked again on subsequent calls. Bearer authentication is accepted only at `/mcp`; it does not
authenticate ordinary browser, credential-management or confirmation routes. Cookies alone cannot
call `/mcp`. Credential management requires the normal platform login and environment CSRF checks.

## Tools and execution

| Tool | Contract |
| --- | --- |
| `list_datasets` | Authorized IDs and bounded names; cursor/limit pagination, no filesystem paths |
| `read_context` | Explicit `location_id` and optional existing context shape; bounded evidence, no raw video/rows |
| `list_operators` / `describe_operator` | Registered schemas and current executor/role availability |
| `preview_pipeline` | Validate one-source draft, at most 12 steps; never save or execute |
| `prepare_action` | Persist a confirmed-only operation; requires `prepare` scope and an operator role |
| `get_action` | Own action, current execution state and authorized result links |
| `get_job` | Own task summary bound to a permitted location |
| `get_result_links` | Existing validated links for an owned action, without rebuilding |

Describe the operator before choosing parameters. Use a new UUID `request_id` for each user intent,
and reuse it for retries. The server namespaces it by account; duplicate requests return the same
persisted action, while different parameters under the same ID conflict. No Qwen turn, model receipt,
Pipeline or Job is created by preparing an action. Metadata and report text remain untrusted content.

`prepare_action` returns `confirmation_url`. The user opens it, logs into the same platform account,
reviews dataset/parameters and clicks **Confirm and run**. Reopening the link is inert. Existing
AssistantConversation/AssistantTurn records preserve input identity, revision and history; confirmation
reuses the existing authorization, source identity, idempotency, Pipeline and Agent contracts. A token
cannot confirm its own action. Later permission/data changes invalidate stale operations. An already
confirmed job follows normal lifecycle rules; revoking a credential does not cancel running work.

The platform's assistant model can be disabled or unconfigured while MCP queries and browser
confirmation continue to work. Tool results returned to external clients may enter their chosen model's
context; only account-authorized summaries and links are returned.

## Release acceptance

Server adds MCP routes and an additive credential table; Agent Job 2, caption/result sync protocols,
execution limits and confirmation contracts are unchanged. CLI 0.3.18 adds credential management.
Keep old executor generations alive until their active attempts finish during a compatible rollout.

Tests cover expiry/revocation/password changes, cookies versus bearer, invalid origins/protocols,
read-only scope, dataset revocation, account isolation, concurrent retry and confirmed-only execution.
Run real Chrome confirmation with the platform model disabled and the official MCP Python SDK client
against the actual HTTP transport. After development installation, repeat TLS/login, SDK discovery,
preparation, browser confirmation, acknowledged local/remote execution and result navigation; approve
that immutable artifact before production promotion. Publish immutable CLI wheel/installer/CA objects,
verify installation and compatibility in both environments, then advance the stable installer.
