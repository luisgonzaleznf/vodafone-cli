"""Browser sign-in for the MCP server: a small local web page that runs auth.LoginFlow.

`vodafone mcp` is a local stdio server, so a host's OAuth "Authenticate" button (meant for remote
servers) doesn't apply. Instead its `login` tool starts this page and sends the user to it: page 1
takes the usuario and password, page 2 picks the line for the SMS code (masked numbers only), and
page 3 takes the code and writes the token set. The password and code go from the browser to this
process and on to Vodafone; they never pass through the MCP host or the model.

Security:
- Bound to 127.0.0.1 on a random free port. Every request must carry the sign-in's random secret
  (secrets.token_urlsafe(32)) as its first path segment, compared in constant time.
- The Host header must be exactly 127.0.0.1:<port> (DNS rebinding), and every POST must carry
  Origin http://127.0.0.1:<port>. The first step that succeeds binds the sign-in to that browser
  with an HttpOnly cookie, so a second client that learns the link can't take over.
- The password and code travel only in urlencoded POST bodies; a query string is refused, and
  nothing is logged (http.server's request and error logs are silenced).
- A strict Content-Security-Policy: no scripts, no external resources, one hashed inline style.
- The page stops serving once the sign-in succeeds or SIGNIN_TTL (10 minutes) passes, and only
  one sign-in runs at a time (SignInManager).
- Pages never echo what was typed. They show fixed messages, plus a VodafoneError's own message,
  which names at most an allowlisted OAuth2 error code or numeric ecode, as the CLI shows it.
"""

import base64
import contextlib
import hashlib
import hmac
import html
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import auth
from .errors import ApiShapeError, AuthError, BlockedError, VodafoneError
from .models import LoginOutput
from .tokens import FileTokenStore

SIGNIN_TTL = 600.0  # seconds a sign-in page lives
MAX_CODE_ATTEMPTS = 3  # wrong SMS codes before the sign-in starts over
_MAX_BODY = 4096
_COOKIE = "vodafone_signin"

_CSS = (
    "body{margin:0;background:#f4f4f4;color:#1a1a1a;font:16px/1.5 -apple-system,BlinkMacSystemFont,"
    "'Segoe UI',Roboto,Helvetica,Arial,sans-serif}"
    "main{max-width:26rem;margin:3rem auto;padding:2rem;background:#fff;border-radius:12px;"
    "box-shadow:0 1px 4px rgba(0,0,0,.12)}"
    "h1{margin:0 0 1rem;font-size:1.4rem;border-left:6px solid #e60000;padding-left:.6rem}"
    "label{display:block;margin:.8rem 0 .2rem;font-weight:600}"
    "input[type=text],input[type=password]{box-sizing:border-box;width:100%;padding:.6rem;font-size:1rem;"
    "border:1px solid #999;border-radius:6px}"
    "fieldset{border:0;margin:0;padding:0}"
    "fieldset label{font-weight:400;font-family:ui-monospace,Menlo,monospace}"
    "button{margin-top:1.2rem;padding:.6rem 1.2rem;font-size:1rem;border:0;border-radius:6px;"
    "background:#e60000;color:#fff;cursor:pointer}"
    "button.plain{background:none;color:#555;padding:0;text-decoration:underline;font-size:.9rem}"
    ".error{background:#fdecea;border:1px solid #f5c2c0;border-radius:6px;padding:.6rem .8rem}"
    ".detail{color:#555;font-size:.85rem}"
    "footer{max-width:26rem;margin:0 auto;color:#666;font-size:.8rem;text-align:center}"
)
_CSP = (
    "default-src 'none'; "
    f"style-src 'sha256-{base64.b64encode(hashlib.sha256(_CSS.encode()).digest()).decode()}'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
_HEADERS = {
    "Content-Security-Policy": _CSP,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Not no-referrer: with it, browsers send `Origin: null` on the page's own form posts.
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _page(title: str, body: str, error: str = "") -> bytes:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title} · Mi Vodafone</title><style>{_CSS}</style></head><body><main>"
        f"<h1>{title}</h1>{error}{body}</main>"
        "<footer>vodafone-cli · a sign-in page served on this computer</footer></body></html>"
    ).encode()


def _error(text: str, detail: str = "") -> str:
    shown = f'<br><span class="detail">{html.escape(detail)}</span>' if detail else ""
    return f'<p class="error" role="alert">{html.escape(text)}{shown}</p>'


def _notice(title: str, text: str) -> bytes:
    return _page(title, f"<p>{html.escape(text)}</p>")


_NOT_FOUND = _notice("Not found", "There is nothing here.")
_BAD_HOST = _notice("Wrong address", "Open the sign-in link exactly as it was given, starting with http://127.0.0.1.")
_FORBIDDEN = _notice("Not allowed", "This request did not come from the sign-in page.")
_OTHER_BROWSER = _notice(
    "In use elsewhere", "This sign-in was started in another browser window. Continue there, or ask to sign in again."
)
_EXPIRED = _notice("Link expired", "This sign-in link has expired. Ask Claude to sign in to Vodafone again.")
_USED = _notice("Signed in", "This sign-in link has been used: you are signed in, and can close this tab.")
_REFUSED = _notice("Not understood", "That request was not understood.")

_START_OVER = '<form method="post" action="restart"><button class="plain">Start over</button></form>'


class SignIn:
    """One browser sign-in: a LoginFlow behind a local HTTP server, from creation until it
    succeeds, expires or is stopped. The server starts at once; `url` is the link to open."""

    def __init__(
        self,
        store: FileTokenStore,
        *,
        ttl: float = SIGNIN_TTL,
        serial: contextlib.AbstractContextManager | None = None,
    ):
        self.store = store
        self.secret = secrets.token_urlsafe(32)
        self.id = secrets.token_urlsafe(16)  # for the MCP host (elicitation id); unrelated to the secret
        self.result: LoginOutput | None = None
        self.done = threading.Event()  # the token set was written
        self.closed = threading.Event()  # the server stopped serving
        # Vodafone requests run under the MCP server's tool lock, so they never overlap a tool call.
        self._serial = serial or contextlib.nullcontext()
        self._flow = auth.LoginFlow(store)
        self._step = "credentials"  # -> "line" -> "code" -> "done"
        self._line = ""
        self._attempts = 0
        self._browser: str | None = None
        self._steps = threading.Lock()
        self._stopping = threading.Lock()
        self._stopped = False
        self._deadline = time.monotonic() + ttl

        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.signin = self
        self.port = self._httpd.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host}"
        self.url = f"{self.origin}/{self.secret}/"
        threading.Thread(target=self._serve, name="vodafone-signin", daemon=True).start()
        self._timer = threading.Timer(ttl, self.stop)
        self._timer.daemon = True
        self._timer.start()

    def _serve(self) -> None:
        try:
            self._httpd.serve_forever(poll_interval=0.1)
        finally:
            self._httpd.server_close()
            self.closed.set()

    @property
    def active(self) -> bool:
        """Still serving: not finished, stopped or expired."""
        return not self._stopped and not self.done.is_set() and time.monotonic() < self._deadline

    def seconds_left(self) -> int:
        return max(0, int(self._deadline - time.monotonic()))

    def stop(self) -> None:
        """Stop serving; the link is dead from then on. Safe from any thread except the server's."""
        with self._stopping:
            if self._stopped:
                return
            self._stopped = True
        self._timer.cancel()
        self._httpd.shutdown()
        self._httpd.server_close()  # the port is closed when stop() returns
        self._flow = auth.LoginFlow(self.store)  # drop the first-factor token and cookies

    # --- requests ----------------------------------------------------------------------------

    @property
    def bound(self) -> bool:
        """A step has succeeded, so only the browser holding its cookie may continue."""
        return self._browser is not None

    def admits(self, cookie: str | None) -> bool:
        """Whether this browser may use the sign-in: anyone with the link until a step succeeds,
        then only the browser holding that step's cookie."""
        return self._browser is None or (cookie is not None and hmac.compare_digest(cookie, self._browser))

    def page(self, cookie: str | None) -> tuple[int, bytes]:
        """The current step's page, for a browser this sign-in admits."""
        with self._steps:
            if not self.active:
                return 410, _USED if self.done.is_set() else _EXPIRED
            if not self.admits(cookie):
                return 403, _OTHER_BROWSER
            return 200, self._render()

    def post(self, action: str, form: dict[str, list[str]], cookie: str | None) -> tuple[int, bytes, str | None]:
        """Run one step; returns (status, page, cookie to set). The browser binding is checked here,
        under the step lock and after the body was read: a request that passed the early check
        while the sign-in was unbound, then held back its body, is refused once a step has bound it."""
        with self._steps:
            if not self.active:
                return 410, _USED if self.done.is_set() else _EXPIRED, None
            if not self.admits(cookie):
                return 403, _OTHER_BROWSER, None
            if action == "restart":
                self._restart()
                return 200, self._render(), None
            if action != {"credentials": "start", "line": "send", "code": "finish"}.get(self._step):
                if action == "start" and self._step in ("line", "code"):
                    self._restart()  # back to page 1 in the browser, then a fresh start
                else:
                    return 409, self._render(_error("That step is already done; continue below.")), None
            return getattr(self, f"_{action}")(form)

    def _restart(self) -> None:
        self._flow = auth.LoginFlow(self.store)
        self._step, self._line, self._attempts = "credentials", "", 0

    def _start(self, form: dict[str, list[str]]) -> tuple[int, bytes, str | None]:
        username, password = _field(form, "username").strip(), _field(form, "password")
        if not username or not password:
            return 200, self._render(_error("Enter your usuario and password.")), None
        try:
            with self._serial:
                self._flow.start(username, password)
        except Exception as exc:
            return 200, self._render(_failure(exc, "Vodafone did not accept that usuario and password.", username, password)), None
        self._step = "line"
        cookie = None
        if self._browser is None:
            self._browser = cookie = secrets.token_urlsafe(32)
        return 200, self._render(), cookie

    def _send(self, form: dict[str, list[str]]) -> tuple[int, bytes, str | None]:
        line = _field(form, "line")
        if line not in self._flow.lines:
            return 200, self._render(_error("Choose one of the numbers listed.")), None
        try:
            with self._serial:
                self._flow.send_code(line)
        except Exception as exc:
            return 200, self._render(_failure(exc, "Vodafone could not send the SMS code.")), None
        self._step, self._line = "code", line
        return 200, self._render(), None

    def _finish(self, form: dict[str, list[str]]) -> tuple[int, bytes, str | None]:
        code = _field(form, "code").strip()
        if not code:
            return 200, self._render(_error("Enter the code from the SMS.")), None
        try:
            with self._serial:
                self.result = self._flow.finish(code)
        except AuthError as exc:
            self._attempts += 1
            if self._attempts >= MAX_CODE_ATTEMPTS:
                self._restart()
                return 200, self._render(_error("Too many attempts. Start again with your usuario and password.")), None
            return 200, self._render(_failure(exc, "Vodafone did not accept that code.", code)), None
        except Exception as exc:
            return 200, self._render(_failure(exc, "Signing in failed.", code)), None
        self._step = "done"
        self.done.set()
        threading.Thread(target=self.stop, daemon=True).start()  # after this reply is sent
        return 200, self._render(), None

    def _render(self, error: str = "") -> bytes:
        if self._step == "credentials":
            return _page(
                "Sign in to Mi Vodafone",
                "<p>Use the usuario and password of the Mi Vodafone app or website.</p>"
                '<form method="post" action="start">'
                '<label for="username">Usuario (mobile number or email)</label>'
                '<input type="text" id="username" name="username" autocomplete="username" required autofocus>'
                '<label for="password">Password</label>'
                '<input type="password" id="password" name="password" autocomplete="current-password" required>'
                "<button>Continue</button></form>",
                error,
            )
        if self._step == "line":
            options = "".join(
                f'<label><input type="radio" name="line" value="{html.escape(line)}"{" checked" if i == 0 else ""}> '
                f"{html.escape(line)}</label>"
                for i, line in enumerate(self._flow.lines)
            )
            return _page(
                "Where should we send the code?",
                "<p>Vodafone sends the code by SMS to the line you choose.</p>"
                f'<form method="post" action="send"><fieldset><legend>Line</legend>{options}</fieldset>'
                f"<button>Send the code</button></form>{_START_OVER}",
                error,
            )
        if self._step == "code":
            return _page(
                "Enter the SMS code",
                f"<p>Vodafone has sent a code by SMS to {html.escape(self._line)}.</p>"
                '<form method="post" action="finish"><label for="code">SMS code</label>'
                '<input type="text" id="code" name="code" autocomplete="one-time-code" inputmode="numeric" '
                'maxlength="12" required autofocus><button>Sign in</button></form>'
                f"{_START_OVER}",
                error,
            )
        return _notice("Signed in", "Signed in, you can close this tab. The Vodafone tools work now.")


def _field(form: dict[str, list[str]], name: str) -> str:
    values = form.get(name) or [""]
    return values[0]


def _failure(exc: Exception, text: str, *typed: str) -> str:
    """An error box: fixed text, plus a VodafoneError's own message, which the client limits to an
    allowlisted OAuth2 error code or ecode. That message is dropped if it contains anything the
    user typed, and anything else is reported by kind only."""
    if not isinstance(exc, VodafoneError):
        return _error(text, f"unexpected internal error: {type(exc).__name__}")
    if isinstance(exc, BlockedError):
        text = "Vodafone's sign-in is unavailable or refused this request; wait a few minutes and try again."
    elif isinstance(exc, ApiShapeError):
        text = "Vodafone's sign-in answered in an unexpected way; it may have changed."
    detail = exc.message
    if any(value and value in detail for value in typed):
        detail = exc.kind
    return _error(text, detail)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    signin: SignIn

    def handle_error(self, request, client_address) -> None:
        pass  # the default prints a traceback to stderr, i.e. the MCP host's log


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "vodafone-cli"
    sys_version = ""
    timeout = 15  # seconds a client may stall a request

    def log_message(self, format: str, *args: object) -> None:
        pass  # the request line carries the secret

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        # The default page echoes the request line; ours is fixed text.
        self.close_connection = True
        self._reply(code, _REFUSED)

    def do_GET(self) -> None:
        self._dispatch(post=False)

    def do_POST(self) -> None:
        self._dispatch(post=True)

    def _dispatch(self, *, post: bool) -> None:
        signin = self.server.signin
        if self.headers.get("Host") != signin.host:
            return self._reply(400, _BAD_HOST)
        path, query = self.path.partition("?")[::2]
        parts = path.split("/")
        if len(parts) != 3 or parts[0] or not hmac.compare_digest(
            parts[1].encode("utf-8", "surrogateescape"), signin.secret.encode()
        ):
            return self._reply(404, _NOT_FOUND)
        if query or "?" in self.path:
            return self._reply(400, _REFUSED)
        if not signin.active:
            if signin.done.is_set():
                return self._reply(410, _USED)
            threading.Thread(target=signin.stop, daemon=True).start()
            return self._reply(410, _EXPIRED)
        cookie = self._cookie()
        if not signin.admits(cookie):  # a fast reject; SignIn.page and .post check again under the step lock
            return self._reply(403, _OTHER_BROWSER)
        action = parts[2]
        if not post:
            return self._reply(*signin.page(cookie)) if action == "" else self._reply(404, _NOT_FOUND)

        if self.headers.get("Origin") != signin.origin:
            return self._reply(403, _FORBIDDEN)
        if action not in ("start", "send", "finish", "restart"):
            return self._reply(404, _NOT_FOUND)
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            return self._reply(415, _REFUSED)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._reply(411, _REFUSED)
        if not 0 <= length <= _MAX_BODY:
            return self._reply(413, _REFUSED)
        try:
            form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True, max_num_fields=8)
        except ValueError:
            return self._reply(400, _REFUSED)
        self._reply(*signin.post(action, form, cookie))

    def _cookie(self) -> str | None:
        # By hand: 127.0.0.1 cookies of other local apps (any port) arrive here too, and one that
        # http.cookies can't parse would hide ours.
        for pair in self.headers.get("Cookie", "").split(";"):
            name, _, value = pair.strip().partition("=")
            if name == _COOKIE:
                return value
        return None

    def _reply(self, status: int, body: bytes, cookie: str | None = None) -> None:
        self.send_response(status)
        for name, value in {**_HEADERS, "Content-Type": "text/html; charset=utf-8"}.items():
            self.send_header(name, value)
        if cookie:
            path = f"/{self.server.signin.secret}/"
            self.send_header("Set-Cookie", f"{_COOKIE}={cookie}; Path={path}; HttpOnly; SameSite=Lax")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class SignInManager:
    """At most one browser sign-in at a time per MCP server."""

    def __init__(
        self,
        store: FileTokenStore,
        *,
        ttl: float = SIGNIN_TTL,
        serial: contextlib.AbstractContextManager | None = None,
    ):
        self._store, self._ttl, self._serial = store, ttl, serial
        self._lock = threading.Lock()
        self._current: SignIn | None = None

    def begin(self) -> SignIn:
        """The sign-in in progress, or a new one if there is none."""
        with self._lock:
            if self._current is not None and self._current.active:
                return self._current
            if self._current is not None:
                self._current.stop()
            self._current = SignIn(self._store, ttl=self._ttl, serial=self._serial)
            return self._current

    def find(self, signin_id: str | None) -> SignIn | None:
        """The current sign-in if its id matches (an id echoed back by the MCP host)."""
        current = self._current
        if current is None or signin_id is None or not hmac.compare_digest(signin_id.encode(), current.id.encode()):
            return None
        return current

    def cancel(self, signin: SignIn) -> None:
        with self._lock:
            signin.stop()
            if self._current is signin:
                self._current = None

    def close(self) -> None:
        with self._lock:
            if self._current is not None:
                self._current.stop()
                self._current = None


def open_browser(url: str, timeout: float = 10.0) -> bool:
    """webbrowser.open(url) in a child Python whose output goes to /dev/null, so whatever launcher
    it picks (osascript, xdg-open, a browser binary) can't write to the MCP stdio channel. The URL
    goes to the child on stdin, not in its argv, which other local users can list. True if a
    browser was started."""
    try:
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", "import sys, webbrowser; sys.exit(not webbrowser.open(sys.stdin.read()))"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    try:
        child.communicate(url.encode(), timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        return False
    return child.returncode == 0
