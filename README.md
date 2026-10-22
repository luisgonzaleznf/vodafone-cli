# vodafone-cli

A JSON-first command-line client for **your own** Mi Vodafone (Vodafone España) account. It talks
directly to Vodafone's DXL REST API (`api.dxl.local.vodafone.es`) over plain HTTPS, so there is no
browser or scraping at runtime. Its JSON output is designed for scripts and coding agents, and a
built-in read-only [MCP](https://modelcontextprotocol.io) server exposes the same data as tools.

## Features

| Command | Description | Status |
|---|---|---|
| `vodafone me` | Customer account: status, segment, debt, flags, holder contact | ✅ |
| `vodafone lines` | Every line (mobile, landline, fibre) with its tariff | ✅ |
| `vodafone usage` | In-plan allowances (data / voice / SMS) per line | ✅ |
| `vodafone auth status` | Access-token validity and refresh-token days remaining (offline) | ✅ |
| `vodafone mcp` | Read-only MCP server (stdio) exposing the read commands as tools | ✅ |
| `vodafone invoices list` | Invoices with amount, dates and status | ⚠️ see [Limitations](#limitations) |
| `vodafone invoices download` | Invoice PDFs | 🚧 not implemented |
| `vodafone auth refresh` | Refresh the access token | 🚧 not implemented |

Write actions (add-ons, tariff changes, roaming, …) are out of scope for this release.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A Mi Vodafone account you can log in to in a browser

## Installation

```bash
git clone https://github.com/luisgonzaleznf/vodafone-cli.git
cd vodafone-cli
uv sync
uv run vodafone --help
```

To put `vodafone` on your `PATH`, run `uv tool install --editable .`

## Authentication

Mi Vodafone's login is protected by reCAPTCHA and an SMS one-time code, so the CLI never handles
your password. Instead, you log in once in a browser and give the CLI the token set the web app
keeps in `localStorage`:

1. Log in at <https://m.vodafone.es/mves/login> (password + SMS code).
2. Export the web app's `localStorage` values with the `mcare-LocalStore.key.` prefix into
   `.local-secrets/tokens.json`. That path is gitignored; keep the file private.

```jsonc
{
  "accessToken": "…",                  // bearer token, valid ~24 h
  "accessTokenExpirationTime": "…Z",
  "refreshToken": "…",                 // valid ~90 days
  "refreshTokenExpirationTime": "…Z",
  "jwt": ["…"],                        // identity JWT (string or one-element list)
  "userCsrfToken": "…",
  "exportedAt": "…Z"                   // optional
}
```

The CLI looks for the file in this order:

1. `--tokens PATH`
2. `$VODAFONE_TOKENS_FILE`
3. `./.local-secrets/tokens.json`
4. `.local-secrets/tokens.json` in the checkout

Run `vodafone auth status` to see when the access token expires. Token refresh is not implemented
yet, so re-export the file when it does. Token values are never printed or logged.

## Usage

Every command writes JSON to stdout.

```bash
vodafone auth status          # token expiry; exits 3 if the access token has expired
vodafone me                   # account details
vodafone lines                # lines and tariffs (same as `vodafone lines list`)
vodafone usage                # allowances for every line
vodafone usage --line 1234    # one line: full number / service id, or unique trailing digits
vodafone invoices list        # invoices
vodafone mcp                  # MCP server on stdio (see below)
```

Global options:

- `--tokens PATH`: token file to use
- `--account ID`: customer account to use, when one login has several

### Output

The JSON shapes are stable and defined by the output models in
[`src/vodafone/models.py`](src/vodafone/models.py).

```jsonc
// vodafone lines
{
  "account_id": "…",
  "lines": [
    {
      "id": "…",
      "kind": "mobile",                // mobile | landline | fibre | other
      "type": "Mobile postpaid",
      "service_code": "…",
      "tariff": {"code": "…", "name": "Prueba Ficticia Demo", "status": "active"}
    }
  ]
}

// vodafone usage
{
  "account_id": "…",
  "lines": [
    {
      "id": "…",
      "kind": "mobile",
      "type": "Mobile postpaid",
      "balances": [
        {
          "type": "data",
          "name": "Datos móviles",
          "group": "card_data",
          "unlimited": true,
          "total":     {"value": 1234.0, "unit": "GB"},
          "used":      {"value": 5.7,    "unit": "GB"},
          "remaining": {"value": 1228.3, "unit": "GB"}
        }
      ]
    }
  ]
}

// vodafone invoices list
{
  "account_id": "…",
  "invoices": [
    {
      "id": "…", "number": "…", "billing_account_id": "…", "status": "…", "type": "…",
      "issued_at": "…", "due_at": "…", "amount": 0.0, "net_amount": 0.0, "currency": "EUR"
    }
  ]
}
```

`unlimited: true` means there is no numeric cap; the totals then show whatever Vodafone reports
(often `0` for voice and SMS). Vodafone itself publishes usage data with a 24–48 h delay.

### Errors and exit codes

Errors are written to stderr as JSON:

```json
{"error": {"type": "access_denied", "message": "…", "exit_code": 3, "http_status": 401}}
```

| Exit code | Error types | Meaning |
|---|---|---|
| 0 | | Success |
| 1 | `internal` | Unexpected error (only the exception type is reported) |
| 2 | `usage`, `not_implemented` | Invalid arguments, or a command that is not implemented yet |
| 3 | `auth_expired` | The access token expired or the token file is missing; re-export it |
| 3 | `access_denied` | HTTP 401 while the token is still valid: that account or line was refused, not the session |
| 5 | `blocked`, `upstream_unavailable` | Bot-protection page, 403 or 429; or a Vodafone 5xx, timeout or stall. Back off before retrying |
| 7 | `api_changed` | Unexpected status or response shape; the API has probably changed |

Requests are sent one at a time with a short delay between them and are never retried
automatically.

## MCP server

`vodafone mcp` runs a read-only Model Context Protocol server on stdio, so MCP hosts such as
Claude Code or Claude Desktop can call the read commands as tools.

| Tool | Arguments | Same output as |
|---|---|---|
| `get_account` | none | `vodafone me` |
| `list_lines` | `account` (optional) | `vodafone lines` |
| `get_usage` | `line`, `account` (both optional) | `vodafone usage [--line …]` |
| `list_invoices` | `account` (optional) | `vodafone invoices list` |

- Each tool returns the CLI's JSON, both as text and as structured content with an output schema.
  A failed call is returned with `isError` set and the CLI's `{"error": {...}}` payload as its
  text.
- All tools are annotated read-only. No write tools are exposed.
- Tool calls run one at a time, and the token file is re-read on every call, so a re-exported
  file is picked up without restarting the server.

The host starts the server from its own working directory, so pass the token file explicitly.

**Claude Code:**

```bash
claude mcp add vodafone -- uv --directory /path/to/vodafone-cli run \
  vodafone --tokens /path/to/vodafone-cli/.local-secrets/tokens.json mcp
```

**Claude Desktop and other hosts using a JSON config:**

```json
{
  "mcpServers": {
    "vodafone": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/vodafone-cli", "run", "vodafone",
        "--tokens", "/path/to/vodafone-cli/.local-secrets/tokens.json", "mcp"
      ]
    }
  }
}
```

If the host can't find `uv`, use its absolute path (`which uv`).

## Limitations

- **`invoices list`** queries the TMF678 bill endpoint
  (`/es/csm-api/customerBillAPI/v3/customerBill`), filtered by billing account and the account
  holder's document ID (read from the API). In testing, Vodafone's billing backend answered with
  HTTP 500 (`No se han obtenido datos en la llamada a RMCA…`), so the command exits with code 5.
  The exact query the web app sends has not been confirmed yet.
- **`auth refresh`** is not implemented: the refresh-token request has not been captured from a
  real login yet. Re-export the token file instead (the access token lasts ~24 h).
- **`invoices download`** is not implemented: it needs the field that carries each bill's
  download token.
- Tokens are read from a JSON file; OS keychain storage is planned.
- This is an unofficial client of an undocumented API, so it may break whenever Vodafone changes
  its backend.

## Development

```bash
uv sync
uv run pytest          # offline: mocked DXL API, synthetic data only
```

```
src/vodafone/
  cli.py          Typer app: commands, exit codes
  mcp_server.py   read-only stdio MCP server (vodafone mcp)
  api.py          read operations (account, lines, usage, invoices)
  client.py       DxlClient: headers, pacing, HTTP error mapping
  models.py       wire models (DXL responses) and output models (CLI JSON)
  output.py       JSON rendering shared by the CLI and the MCP server
  tokens.py       token-set loading and expiry checks
  errors.py       typed errors and exit codes
  config.py       hosts, header constants, token-file lookup
tests/            unit tests (CLI, client, MCP server)
```

## Disclaimer

This project is not affiliated with or endorsed by Vodafone. Use it only with your own account.
Vodafone's terms of service may restrict automated access; you are responsible for how you use
this tool.

## License

[MIT](LICENSE)
