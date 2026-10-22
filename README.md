# vodafone-cli

A JSON-first command-line client for **your own** Mi Vodafone (Vodafone España) account. It talks
directly to Vodafone's DXL REST API (`api.dxl.local.vodafone.es`) over plain HTTPS, so there is no
browser or scraping at runtime. Its JSON output is designed for scripts and coding agents, and a
built-in read-only [MCP](https://modelcontextprotocol.io) server exposes the same data as tools.

## Features

| Command | Description | Status |
|---|---|---|
| `vodafone auth login` | Sign in (usuario + password + SMS code) and save the token set | ⚠️ full live sign-in awaiting owner verification |
| `vodafone me` | Customer account: status, segment, debt, flags, holder contact | ✅ |
| `vodafone lines` | Every line (mobile, landline, fibre) with its tariff | ✅ |
| `vodafone usage` | In-plan allowances (data / voice / SMS) per line | ✅ |
| `vodafone auth status` | Access-token validity and refresh-token days remaining (offline) | ✅ |
| `vodafone mcp` | Read-only MCP server (stdio) exposing the read commands as tools | ✅ |
| `vodafone invoices list` | Invoices with amount, dates and status | ✅ |
| `vodafone invoices download` | Invoice PDFs (one, the latest, or all) | ✅ |
| `vodafone auth refresh` | Refresh the access token | ⚠️ CSRF refusal fixed; successful rotation awaiting live verification (see [Limitations](#limitations)) |

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

### Sign in

Full password + SMS sign-in still needs owner verification. Browser token export below is the
verified fallback while that check is pending.

```bash
vodafone auth login
```

Prompts for your usuario (mobile number or email) and password, sends an SMS code to one of your
lines, and asks for it — the same sign-in as the web app, no browser. On success it writes the
token set to the token file, and every other command works. The password and the SMS code are
read with hidden prompts and are never stored or logged. No text from the server's SMS challenge
is shown (the CLI prints a fixed "code sent" line), and auth errors name only a standard OAuth2
error code (e.g. `invalid_grant`), never the endpoint's free-text message or headers, which could
echo what was sent.

### Browser token export (verified fallback)

If you'd rather not type credentials into the CLI, log in at
<https://m.vodafone.es/mves/login> and export the web app's `localStorage` values with the
`mcare-LocalStore.key.` prefix into `.local-secrets/tokens.json`:

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

### Token file location

The CLI looks for the file in this order, and `auth login` writes to the same place:

1. `--tokens PATH`
2. `$VODAFONE_TOKENS_FILE`
3. `./.local-secrets/tokens.json`
4. `.local-secrets/tokens.json` in the checkout

`auth login` writes it with mode `0600`, through a fresh private temp file that is atomically
renamed into place. Run `vodafone auth status` to see when the access token expires.
`vodafone auth refresh` makes one exchange attempt and saves any rotated tokens. If it returns
`invalid_grant`, obtain a fresh token set by signing in again or exporting the current browser
session (see [Limitations](#limitations)). Token values are never printed or logged.

## Usage

Every command writes JSON to stdout.

```bash
vodafone auth login           # sign in (usuario + password + SMS code); saves the token set
vodafone auth status          # token expiry; exits 3 if the access token has expired
vodafone auth refresh         # exchange the refresh token and save any rotated tokens
vodafone me                   # account details
vodafone lines                # lines and tariffs (same as `vodafone lines list`)
vodafone usage                # allowances for every line
vodafone usage --line 1234    # one line: full number / service id, or unique trailing digits
vodafone invoices list        # invoices
vodafone invoices download            # latest invoice PDF into the current directory
vodafone invoices download all --dir ~/facturas
vodafone invoices download <id>       # one invoice; ids come from `invoices list`
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

// vodafone invoices download all --dir facturas
{
  "account_id": "…",
  "directory": "facturas",
  "invoices": [
    {"id": "…", "month": "2026-10", "file": "facturas/factura_2026-10_<id>.pdf", "bytes": 812345, "status": "downloaded"},
    {"id": "…", "month": "2026-09", "file": "facturas/factura_2026-09_<id>.pdf", "bytes": 798112, "status": "skipped"}
  ]
}
```

Invoice PDFs are saved as `factura_YYYY-MM_<id>.pdf`: billing month plus invoice id, so invoices of
different accounts (or two bills in one month) never share a file. Without a billing month the
name is `factura_<id>.pdf`; ids or dates that would not make a plain file name are rejected, so
nothing is written outside `--dir`. Files are written with mode `0600`, and a PDF only appears under
its final name once complete: it is written to a hidden temp file and hard-linked into place. On a
filesystem without hard links (FAT, some network shares) the download stops with a `usage` error
and writes nothing, rather than risk a partial or overwritten file; pick another `--dir`. Existing
files (including one that appears during the download) are skipped, never overwritten. `all`
downloads newest first, one at a time with a 2-second gap, because a burst of downloads stalls
Vodafone's backend; it stops at the first failure and the error names the files saved so far.

`unlimited: true` means there is no numeric cap; the totals then show whatever Vodafone reports
(often `0` for voice and SMS). Vodafone itself publishes usage data with a 24–48 h delay.

### Errors and exit codes

Errors are written to stderr as JSON:

```json
{"error": {"type": "access_denied", "message": "…", "exit_code": 3, "http_status": 401, "transaction_id": "…"}}
```

`transaction_id`, when present, is the `vf-trace-transaction-id` the CLI generated for the failing
request (a random UUID), for matching Vodafone-side logs. Error messages never echo a value the
server sent back from the request (credentials, tokens, query values such as your NIF) nor raw
response headers: such values are masked or replaced by fixed text.

| Exit code | Error types | Meaning |
|---|---|---|
| 0 | | Success |
| 1 | `internal` | Unexpected error (only the exception type is reported) |
| 2 | `usage` | Invalid arguments (unknown line, unknown invoice id, …), or a `--dir` whose filesystem has no hard links |
| 3 | `auth_expired` | The access token expired, or the token file is missing or malformed; run `vodafone auth login` |
| 3 | `access_denied` | HTTP 401 while the token is still valid: that account or line was refused, not the session |
| 5 | `blocked`, `upstream_unavailable` | Bot-protection page, 403 or 429; or a Vodafone 5xx, timeout or stall. Back off before retrying |
| 7 | `api_changed` | Unexpected status or response shape (including a non-finite number such as `NaN`, which is never written to the JSON output); the API has probably changed |

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
- All tools are annotated read-only. No write tools are exposed, and invoice PDF downloads are
  CLI-only.
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

- **Native authentication:** the missing `csrf-token` cookie that caused HTTP 403 `access_denied`
  on both login and refresh has been fixed. Auth requests now pair that cookie with the locally
  generated or stored `vf-csrf-token`; browser cookies and HTTP/2 are not needed to pass this check.
  A fake-account password grant now reaches HTTP 401, and the tested stored refresh token returns
  HTTP 401 `invalid_grant`. Full password + SMS login and successful refresh rotation still need
  owner verification with current credentials. A refused refresh leaves the token file untouched
  and exits 3. Browser token export remains the verified fallback.
- Tokens are read from a JSON file; OS keychain storage is planned.
- This is an unofficial client of an undocumented API, so it may break whenever Vodafone changes
  its backend.

## Development

```bash
uv sync
uv run pytest                     # offline: mocked DXL API, synthetic data only
VODAFONE_LIVE=1 uv run pytest     # also runs read-only checks against your account
```

```
src/vodafone/
  cli.py          Typer app: commands, exit codes
  auth.py         native login (password + SMS OTP) and token refresh
  mcp_server.py   read-only stdio MCP server (vodafone mcp)
  api.py          read operations (account, lines, usage, invoices)
  client.py       DxlClient: headers, pacing, HTTP error mapping
  models.py       wire models (DXL responses) and output models (CLI JSON)
  output.py       JSON rendering shared by the CLI and the MCP server
  tokens.py       token-set loading and expiry checks
  errors.py       typed errors and exit codes
  config.py       hosts, header constants, token-file lookup
tests/            unit tests (auth, token store, CLI, client, invoices, MCP server) and opt-in live tests
```

## Disclaimer

This project is not affiliated with or endorsed by Vodafone. Use it only with your own account.
Vodafone's terms of service may restrict automated access; you are responsible for how you use
this tool.

## License

[MIT](LICENSE)
