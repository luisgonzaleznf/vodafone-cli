# vodafone-cli

A JSON-first command-line client for **your own** Mi Vodafone (Vodafone España) account. It talks
directly to Vodafone's DXL REST API (`api.dxl.local.vodafone.es`) over plain HTTPS, so there is no
browser or scraping at runtime. Its JSON output is designed for scripts and coding agents, and a
built-in read-only [MCP](https://modelcontextprotocol.io) server exposes the same data as tools.
Claude Code or Codex can run that server with [one command](#mcp-server), and you sign in from the
chat through a page in your own browser.

## Features

| Command | Description | Status |
|---|---|---|
| `vodafone auth login` | Sign in (usuario + password + SMS code) and save the token set | ✅ |
| `vodafone me` | Customer account: status, segment, debt, flags, holder contact | ✅ |
| `vodafone lines` | Every line (mobile, landline, fibre) with its tariff | ✅ |
| `vodafone usage` | In-plan allowances (data / voice / SMS) per line | ✅ |
| `vodafone auth status` | Access-token validity and refresh-token days remaining (offline) | ✅ |
| `vodafone mcp` | Read-only MCP server (stdio): the read commands as tools, plus browser sign-in | ✅ |
| `vodafone invoices list` | Invoices with amount, dates and status | ✅ |
| `vodafone invoices download` | Invoice PDFs (one, the latest, or all) | ✅ |
| `vodafone auth refresh` | Refresh the access token (and rotate the refresh token); reads do it automatically | ✅ |

Write actions (add-ons, tariff changes, roaming, …) are out of scope for this release.

## Requirements

- A Mi Vodafone account you can log in to in a browser
- macOS or Linux. Homebrew brings its own Python; the install script and `uv tool install` use
  [uv](https://docs.astral.sh/uv/), which fetches Python 3.12+ if you don't have it. Working from
  source needs Python 3.12+ and uv.

## Installation

Pick one; each puts a `vodafone` command on your `PATH`. Check it with `vodafone --version`, then
[sign in](#authentication) with `vodafone auth login`. To use only the MCP server, you can skip this:
see [MCP server](#mcp-server).

### Homebrew

This repository is its own Homebrew tap (the formula is [`Formula/vodafone.rb`](Formula/vodafone.rb)):

```bash
brew tap luisgonzaleznf/vodafone-cli https://github.com/luisgonzaleznf/vodafone-cli
brew install luisgonzaleznf/vodafone-cli/vodafone
```

Upgrade with `brew update && brew upgrade luisgonzaleznf/vodafone-cli/vodafone`. The formula
installs the release's exact dependency set from `uv.lock` (prebuilt wheels, no Rust toolchain)
into its own virtualenv on Homebrew's Python 3.14.

### Install script

```bash
curl -fsSL https://raw.githubusercontent.com/luisgonzaleznf/vodafone-cli/main/install.sh | sh
```

[`install.sh`](install.sh) installs uv with Astral's official installer if it is missing, then runs
`uv tool install` for the pinned release, so `vodafone` lands in uv's tool bin directory (usually
`~/.local/bin`). Running it again is safe: the same release is left as it is. It never replaces an
editable (development) install unless asked to. Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `VODAFONE_CLI_VERSION` | the current release | Release to install; installs the tag `v<version>` |
| `VODAFONE_CLI_REF` | `v$VODAFONE_CLI_VERSION` | Any git ref instead (branch, tag or commit) |
| `VODAFONE_CLI_SOURCE` | `https://github.com/luisgonzaleznf/vodafone-cli.git` | Repository URL, e.g. `ssh://git@github.com/luisgonzaleznf/vodafone-cli.git`. Only plain `https://`, `ssh://` and `file://` URLs are accepted (no password, token, query or fragment); let a git credential helper supply credentials |
| `VODAFONE_CLI_FORCE` | unset | `1` replaces an editable install, or another `vodafone` in uv's bin directory |

uv's own variables (`UV_TOOL_DIR`, `UV_TOOL_BIN_DIR`, …) apply as usual, and so do the uv
installer's (`UV_INSTALL_DIR`, `UV_UNMANAGED_INSTALL`, `XDG_BIN_HOME`, …): the script looks for a
freshly installed uv where the installer put it.

### uv tool install

```bash
uv tool install git+https://github.com/luisgonzaleznf/vodafone-cli@v0.2.0
```

This is what the install script runs. To move to another release, run it again with that tag.
Unlike Homebrew, `uv tool install` (and so the install script) resolves dependencies within the
ranges in `pyproject.toml` at install time rather than using `uv.lock`'s exact versions.

### From source (development)

```bash
git clone https://github.com/luisgonzaleznf/vodafone-cli.git
cd vodafone-cli
uv sync
uv run vodafone --help
uv tool install --editable .    # optional: put this checkout's `vodafone` on your PATH
```

An editable install runs the checkout's code, so changes apply without reinstalling.

If more than one copy is installed, the first `vodafone` on your `PATH` wins (`which -a vodafone`):
uv tools live in `~/.local/bin`, Homebrew's in `$(brew --prefix)/bin`. To uninstall, run
`brew uninstall vodafone && brew untap luisgonzaleznf/vodafone-cli` or `uv tool uninstall vodafone-cli`;
neither removes your token file.

## Authentication

### Sign in

```bash
vodafone auth login
```

Prompts for your usuario (the account's sign-in: mobile number or email) and password, asks which
of the account's lines should get the SMS code (shown masked, e.g. `6*****789`; pick with ↑/↓ and
Enter), then asks for the code — the same sign-in as the web app, no browser. On success it writes
the token set to the token file, and every other command works. The password is read with a
hidden prompt; neither it nor the SMS code is stored or logged. No text from the server's SMS
challenge is shown (the CLI prints a fixed "code sent" line), and auth errors name only a standard
OAuth2 error code (e.g. `invalid_grant`) or Vodafone's numeric one (e.g. `ecode 1005`, returned for
a wrong usuario or password), never the endpoint's free-text message or headers, which could echo
what was sent.

With the [MCP server](#mcp-server) you can sign in from the chat instead: see
[How sign-in works](#how-sign-in-works).

### Browser token export (alternative)

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
3. An installed copy (Homebrew, the install script, `uv tool install`, `uvx`):
   `~/.config/vodafone-cli/tokens.json`, or `$XDG_CONFIG_HOME/vodafone-cli/tokens.json` when
   `XDG_CONFIG_HOME` is set, whatever directory it runs from
4. A source checkout (`uv run`, or an editable install): `./.local-secrets/tokens.json` if it exists,
   else `.local-secrets/tokens.json` in the checkout

`auth login` writes it with mode `0600`, through a fresh private temp file that is atomically
renamed into place. The MCP server's [browser sign-in](#how-sign-in-works) writes the same file.
Run `vodafone auth status` to see when the access token expires (it never refreshes anything).

The access token lasts about 24 hours and the refresh token about 90 days. Every read command (and
read tool) refreshes the access token first when it has expired or expires within a minute, if the
refresh token is still valid: two requests (the refresh grant, then the identity JWT), no SMS code,
and the rotated tokens are saved to the file. `vodafone auth refresh` does the same on demand.

A refresh is tried once per command, never in a loop, and a failed one leaves the file as it was.
If the access token is still valid (it had less than a minute left), the read goes ahead with it.
Otherwise:

- Vodafone refused the refresh token (`invalid_grant`, say): exit 3 (`auth_expired`); sign in
  again. Until you do, each read tries the refused token once more.
- Vodafone is unavailable or blocked the request: exit 5, as for any other request; try again
  later, the session is not lost.

Since the CLI and several MCP hosts can run at once and the refresh token rotates, every refresh
and sign-in holds a lock on `tokens.json.lock` (an empty file next to the token file, mode
`0600`); a process that waited for it uses the tokens the other one saved. If that file can't be
created (a read-only folder), a read that needs a refresh exits 2. Token values are never printed
or logged.

## Usage

Every command writes JSON to stdout (`--version` and `--help` print plain text).

```bash
vodafone --version            # installed version, e.g. 0.2.0
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
      "issued_at": "…", "due_at": "…", "currency": "EUR",
      "amount": 0.0,                   // the total to pay, IVA included
      "net_amount": 0.0                // before tax
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
| 2 | `usage` | Invalid arguments (unknown line, unknown invoice id, …), a `--dir` whose filesystem has no hard links, or a token-file folder where the lock file can't be created |
| 3 | `auth_expired` | Not signed in: the token file is missing or malformed, or the access token expired and can't be renewed (no refresh token, it expired, or Vodafone refused it); run `vodafone auth login` |
| 3 | `access_denied` | HTTP 401 while the token is still valid: that account or line was refused, not the session |
| 5 | `blocked`, `upstream_unavailable` | Bot-protection page, 403 or 429; or a Vodafone 5xx, timeout or stall. Back off before retrying |
| 7 | `api_changed` | Unexpected status or response shape (including a non-finite number such as `NaN`, which is never written to the JSON output); the API has probably changed |

Requests are sent one at a time with a short delay between them and are never retried
automatically. The only automatic requests are the refresh of an expiring access token before a
read: the refresh grant and the identity JWT.

## MCP server

`vodafone mcp` runs a Model Context Protocol server on stdio, so Claude Code, Codex, Claude Desktop
and other MCP hosts can read your account as tools, and sign you in from the chat. It needs
[uv](https://docs.astral.sh/uv/) (`brew install uv`, or Astral's installer) and git, which uv uses
to fetch the package (on macOS, `xcode-select --install` or `brew install git`); nothing else has
to be installed first.

**Claude Code:**

```bash
claude mcp add --scope user vodafone -- uvx --from git+https://github.com/luisgonzaleznf/vodafone-cli@v0.2.0 vodafone mcp
```

**Codex:**

```bash
codex mcp add vodafone -- uvx --from git+https://github.com/luisgonzaleznf/vodafone-cli@v0.2.0 vodafone mcp
```

Then ask the assistant to sign in to Vodafone (see [below](#how-sign-in-works)). Each line pins a
release: to upgrade, remove the server (`claude mcp remove --scope user vodafone`,
`codex mcp remove vodafone`) and add it again with the new tag. The first start downloads and
builds the package, which can take longer than a host waits for a new server. If the host reports
a timeout, fill uv's cache once from a terminal, then restart the host:

```bash
uvx --from git+https://github.com/luisgonzaleznf/vodafone-cli@v0.2.0 vodafone --version
```

**With Homebrew** (or another [installation](#installation) that puts `vodafone` on your `PATH`):

```bash
claude mcp add --scope user vodafone -- vodafone mcp
```

**Claude Desktop and other hosts using a JSON config.** Such hosts often start servers with a
minimal `PATH`, so give the absolute path of `uvx` (`which uvx`) or of `vodafone` (`which vodafone`):

```json
{
  "mcpServers": {
    "vodafone": {
      "command": "/opt/homebrew/bin/uvx",
      "args": ["--from", "git+https://github.com/luisgonzaleznf/vodafone-cli@v0.2.0", "vodafone", "mcp"]
    }
  }
}
```

### How sign-in works

- `vodafone mcp` is a local server, running on your own computer. There is no Authenticate button
  for it in Claude Code's `/mcp`: that button is for remote servers that sign in with OAuth.
- Ask Claude to sign in to Vodafone. It calls the `login` tool, and a sign-in page opens in your
  browser. The page is served by the MCP server itself, at `http://127.0.0.1` on your machine.
  Depending on the app, Claude either shows you the link and asks before opening it, or the browser
  opens on its own and the link is also in the chat.
- On that page you type your usuario and password, choose which line gets the SMS code, and type
  the code. Your password and SMS code go from the page to Vodafone; they never pass through the
  chat or the model.
- The tokens are saved to `~/.config/vodafone-cli/tokens.json` (mode `0600`; see
  [Token file location](#token-file-location) for other places) and renewed automatically, so the
  tools keep working for about 90 days. After that, ask Claude to sign in again.
- The CLI is optional: the MCP server needs neither `vodafone` on your `PATH` nor
  `vodafone auth login`. Both use the same token file, so signing in with either works for both.

The page works only from your computer, for 10 minutes, and for one sign-in; it loads nothing from
the internet. Each link carries its own random secret, and only one sign-in runs at a time: asking
again while one is open gives you the same link. Once a sign-in is over, its old link only shows a
"link expired" or "signed in" notice. While a sign-in is in progress, `auth_status` reports
`signed_in: false`.

### Tools

| Tool | Arguments | Same output as |
|---|---|---|
| `get_account` | none | `vodafone me` |
| `list_lines` | `account` (optional) | `vodafone lines` |
| `get_usage` | `line`, `account` (both optional) | `vodafone usage [--line …]` |
| `list_invoices` | `account` (optional) | `vodafone invoices list` |
| `auth_status` | none | `vodafone auth status`, plus `signed_in` and `reason` (offline) |
| `login` | none | opens the browser sign-in |

- Each read tool returns the CLI's JSON, both as text and as structured content with an output
  schema. A failed call is returned with `isError` set and the CLI's `{"error": {...}}` payload
  as its text. When you are not signed in (no token file, or a session that can't be renewed), the
  error's type is `auth_expired` and its message points at the `login` tool.
- The read tools and `auth_status` are annotated read-only. `login` is not, because it writes the
  local token file, but it changes nothing in your Vodafone account. There are no write tools, and
  invoice PDF downloads are CLI-only.
- Tool calls run one at a time. The token file is re-read on every call, so a new sign-in or a
  re-exported file is picked up without a restart, and an access token that is about to expire is
  refreshed first.

**Development, from a checkout:** the host starts the server from its own working directory, so
run it through uv and pass the token file explicitly (use `uv`'s absolute path if the host can't
find it):

```bash
claude mcp add vodafone -- uv --directory /path/to/vodafone-cli run \
  vodafone --tokens /path/to/vodafone-cli/.local-secrets/tokens.json mcp
```

## Limitations

- **Native authentication** reproduces the web app's requests: a `csrf-token` cookie matching the
  `vf-csrf-token` header, the session cookies the server sets during the SMS step, and the masked
  number from the password grant as the SMS target. If Vodafone changes that flow, sign-in (in the
  CLI and in the MCP server's browser page alike) may break before the data commands do; browser
  token export still works then. A failed refresh, automatic or not, leaves the token file
  untouched: a refused one exits 3, Vodafone being unavailable exits 5.
- Sessions last as long as the refresh token, about 90 days, then need a new sign-in with an SMS
  code.
- Tokens are read from a JSON file; OS keychain storage is planned.
- This is an unofficial client of an undocumented API, so it may break whenever Vodafone changes
  its backend.

## Development

```bash
uv sync
uv run pytest                     # offline: mocked DXL API, synthetic data only
VODAFONE_LIVE=1 uv run pytest     # also runs read-only checks against your account
shellcheck install.sh
```

Releases (version bump, tag, Homebrew formula, install script, PyPI) are described in
[RELEASING.md](RELEASING.md).

```
src/vodafone/
  cli.py          Typer app: commands, exit codes
  auth.py         native login (password + SMS OTP) in steps, token refresh and status
  mcp_server.py   stdio MCP server (vodafone mcp): read tools, auth_status, login
  signin.py       the MCP server's local browser sign-in page
  api.py          read operations (account, lines, usage, invoices)
  client.py       DxlClient: headers, pacing, HTTP error mapping
  models.py       wire models (DXL responses) and output models (CLI JSON)
  output.py       JSON rendering shared by the CLI and the MCP server
  tokens.py       token-set loading, expiry checks and the token-file lock
  errors.py       typed errors and exit codes
  config.py       hosts, header constants, token-file lookup
tests/            unit tests (auth, refresh, token store, CLI, client, invoices, sign-in page, MCP server, packaging) and opt-in live tests
Formula/          Homebrew formula (this repository is its own tap)
install.sh        curl | sh installer (uv tool install of the pinned release)
```

## Disclaimer

This project is not affiliated with or endorsed by Vodafone. Use it only with your own account.
Vodafone's terms of service may restrict automated access; you are responsible for how you use
this tool.

## License

[MIT](LICENSE)
