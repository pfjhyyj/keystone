"""Bounded browser actions over loopback CDP, one at a time, for the authenticated Keystone caller.

Snapshots tag visible interactive elements with generation-scoped references; click and type only
accept references from the latest snapshot, so a stale page never receives input meant for an
older one. Results carry only small projections, never cookies or full page bodies.
"""

import base64
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from itertools import count
from urllib.parse import urlsplit
from urllib.request import urlopen

import websocket

CDP = "http://127.0.0.1:9222"
MAX_BODY = 8192
MAX_TEXT = 8000
MAX_ELEMENTS = 200
MAX_SCREENSHOT = 2 * 1024 * 1024
ACTIONS = {"snapshot", "click", "type", "read", "screenshot", "open", "wait"}

SNAPSHOT = r"""
(() => {
  const generation = (window.__keystoneGeneration = (window.__keystoneGeneration || 0) + 1);
  document.querySelectorAll('[data-keystone-ref]')
    .forEach(e => e.removeAttribute('data-keystone-ref'));
  const selector = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=tab],' +
    '[role=checkbox],[role=radio],[role=option],[role=menuitem],[contenteditable=true]';
  const shown = e => {
    if (e.closest('[hidden],[inert],[aria-hidden=true]')) return false;
    const box = e.getBoundingClientRect(), style = getComputedStyle(e);
    return box.width > 0 && box.height > 0 && style.visibility === 'visible' &&
      style.display !== 'none';
  };
  const clean = value => (value || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const elements = [];
  for (const e of document.querySelectorAll(selector)) {
    if (elements.length >= %(limit)d) break;
    if (!shown(e)) continue;
    const ref = `s${generation}e${elements.length + 1}`;
    e.setAttribute('data-keystone-ref', ref);
    const secret = e.type === 'password';
    elements.push({
      ref,
      role: e.getAttribute('role') || e.tagName.toLowerCase(),
      name: clean(e.getAttribute('aria-label') || e.getAttribute('title') || e.innerText ||
        e.getAttribute('placeholder') || e.getAttribute('name')),
      value: secret || !('value' in e) || e.tagName === 'BUTTON' ? undefined : clean(e.value),
    });
  }
  return {title: clean(document.title), elements,
          text: (document.body ? document.body.innerText : '').slice(0, %(text)d)};
})()
"""

LOCATE = r"""
(ref => {
  const e = document.querySelector(`[data-keystone-ref="${CSS.escape(ref)}"]`);
  if (!e || !ref.startsWith(`s${window.__keystoneGeneration}e`)) return null;
  e.scrollIntoView({block: 'center', inline: 'center'});
  const box = e.getBoundingClientRect();
  if (box.width <= 0 || box.height <= 0) return null;
  return {x: box.left + box.width / 2, y: box.top + box.height / 2};
})(%s)
"""

FOCUS = r"""
(ref => {
  const e = document.querySelector(`[data-keystone-ref="${CSS.escape(ref)}"]`);
  if (!e || !ref.startsWith(`s${window.__keystoneGeneration}e`)) return false;
  e.focus();
  return document.activeElement === e;
})(%s)
"""

READ = r"""
(ref => {
  const e = ref === null ? document.body
    : document.querySelector(`[data-keystone-ref="${CSS.escape(ref)}"]`);
  return e ? (e.innerText || e.value || '').slice(0, %d) : null;
})(%s)
"""


class ActionError(Exception):
    def __init__(self, state):
        self.state = state


class Page:
    def __init__(self, deadline):
        self.deadline = deadline
        with urlopen(CDP + "/json/list", timeout=self.remaining()) as response:
            targets = json.loads(response.read(1024 * 1024))
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise ActionError("no_page")
        self.socket = websocket.create_connection(
            pages[0]["webSocketDebuggerUrl"], timeout=self.remaining(), suppress_origin=True
        )
        self.ids = count(1)

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return left

    def close(self):
        self.socket.close()

    def call(self, method, **params):
        identifier = next(self.ids)
        self.socket.send(json.dumps({"id": identifier, "method": method, "params": params}))
        while True:
            self.socket.settimeout(self.remaining())
            message = json.loads(self.socket.recv())
            if message.get("id") == identifier:
                if "error" in message:
                    raise ActionError("failed")
                return message.get("result", {})

    def evaluate(self, expression):
        result = self.call(
            "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True
        )
        if "exceptionDetails" in result:
            raise ActionError("failed")
        return result.get("result", {}).get("value")

    def url(self):
        return self.evaluate("location.href")

    def settle(self):
        while self.evaluate("document.readyState") != "complete":
            if self.remaining() < 0.3:
                return
            time.sleep(0.2)


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise ActionError("invalid")
    return value


def run(name, arguments, deadline):
    page = Page(deadline)
    try:
        if name == "snapshot":
            data = page.evaluate(SNAPSHOT % {"limit": MAX_ELEMENTS, "text": MAX_TEXT})
        elif name in {"click", "type"}:
            ref = json.dumps(_text(arguments.get("ref"), 32))
            if name == "click":
                point = page.evaluate(LOCATE % ref)
                if not point:
                    raise ActionError("stale")
                for kind in ("mousePressed", "mouseReleased"):
                    page.call(
                        "Input.dispatchMouseEvent",
                        type=kind, x=point["x"], y=point["y"], button="left", clickCount=1,
                    )
            else:
                if not page.evaluate(FOCUS % ref):
                    raise ActionError("stale")
                page.call("Input.insertText", text=_text(arguments.get("text"), 2000))
            data = {}
        elif name == "read":
            ref = arguments.get("ref")
            ref = json.dumps(None if ref is None else _text(ref, 32))
            text = page.evaluate(READ % (MAX_TEXT, ref))
            if text is None:
                raise ActionError("stale")
            data = {"text": text}
        elif name == "screenshot":
            shot = page.call("Page.captureScreenshot", format="jpeg", quality=60)["data"]
            if len(base64.b64decode(shot)) > MAX_SCREENSHOT:
                raise ActionError("too_large")
            data = {"image": shot}
        elif name == "open":
            url = _text(arguments.get("url"), 2048)
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
                raise ActionError("invalid")
            page.call("Page.navigate", url=url)
            page.settle()
            data = {}
        else:
            seconds = arguments.get("seconds")
            if type(seconds) not in {int, float} or not 0 < seconds <= 10:
                raise ActionError("invalid")
            time.sleep(min(seconds, max(0, page.remaining() - 0.5)))
            data = {}
        return {"state": "ok", "url": page.url(), **data}
    finally:
        page.close()


def serve(token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, data=None):
            body = json.dumps(data).encode() if data is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def do_POST(self):
            supplied = self.headers.get("X-Keystone-Browser-Control", "").encode()
            if not hmac.compare_digest(supplied, token):
                self.reply(403)
                return
            name = self.path.removeprefix("/actions/")
            if name not in ACTIONS:
                self.reply(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
                seconds = payload["timeout_seconds"]
                arguments = payload.get("arguments", {})
                if type(seconds) is not int or not 1 <= seconds <= 30 or not isinstance(
                    arguments, dict
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                self.reply(422)
                return
            try:
                data = run(name, arguments, time.monotonic() + seconds)
            except ActionError as error:
                data = {"state": error.state}
            except (OSError, TimeoutError, websocket.WebSocketException, ValueError, KeyError):
                data = {"state": "unavailable"}
            self.reply(200, data)

    class Server(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(40)
            return connection, address

    # One action at a time: the server handles requests serially.
    with Server(("127.0.0.1", 6082), Handler) as server:
        server.serve_forever(poll_interval=0.25)
