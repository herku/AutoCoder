"""Live dashboard server. SSE-based, localhost-only.

Used by loop.run() when --serve is set. Telemetry publishes events onto an
EventBus; the HTTP server serves a static dashboard and streams events to
connected browsers.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

_RING_SIZE = 500
_SSE_KEEPALIVE_SECONDS = 15.0
_SUBSCRIBER_QUEUE_SIZE = 2000


class EventBus:
    """In-process pub/sub for telemetry events with ring-buffer replay."""

    def __init__(self, ring_size: int = _RING_SIZE) -> None:
        self._ring: deque[dict[str, Any]] = deque(maxlen=ring_size)
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        event = {"type": event_type, "ts": time.time(), **data}
        with self._lock:
            self._ring.append(event)
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        with self._lock:
            for event in list(self._ring):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    break
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._ring)


_DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>AutoCoder Dashboard</title>
<style>
body { font: 13px/1.4 -apple-system, BlinkMacSystemFont, Segoe UI, monospace; margin: 0; background: #0b0d12; color: #d0d4dc; }
header { padding: 12px 18px; background: #141822; border-bottom: 1px solid #1e232e; display: flex; gap: 18px; align-items: center; flex-wrap: wrap; }
header h1 { margin: 0; font-size: 15px; font-weight: 600; color: #fff; }
header .stat { font-variant-numeric: tabular-nums; color: #8b95a8; }
header .stat b { color: #e8ecf2; font-weight: 600; }
#filters { padding: 8px 18px; background: #11141b; border-bottom: 1px solid #1e232e; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
#filters label { color: #8b95a8; font-size: 12px; user-select: none; }
#filters input[type=text] { background: #1b2030; border: 1px solid #2a3142; color: #e8ecf2; padding: 4px 8px; font: inherit; border-radius: 3px; min-width: 200px; }
#filters input[type=checkbox] { margin-right: 4px; vertical-align: middle; }
#log { padding: 0; margin: 0; }
.row { padding: 4px 18px; border-bottom: 1px solid #14171f; display: grid; grid-template-columns: 92px 90px 110px 1fr; gap: 12px; align-items: baseline; }
.row .t { color: #5b6478; font-variant-numeric: tabular-nums; }
.row .tag { font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.03em; }
.row .issue { color: #85a5ff; font-variant-numeric: tabular-nums; }
.row .body { color: #c5cbd6; }
.row[data-type=issue_start] .tag { color: #6ee7b7; }
.row[data-type=issue_end] .tag { color: #8ad7ff; }
.row[data-type=phase_end] .tag { color: #d6a4ff; }
.row[data-type=verify] .tag { color: #fcb47d; }
.row[data-type=failure] .tag { color: #ff7e8a; }
.row[data-type=idle] .tag { color: #ffb347; }
.row[data-type=review] .tag { color: #ffc877; }
.row[data-type=ci] .tag { color: #85a5ff; }
.ok { color: #6ee7b7; }
.fail { color: #ff7e8a; }
.dim { color: #5b6478; }
</style></head><body>
<header>
<h1>AutoCoder</h1>
<div class="stat">Issues: <b id="s-issues">0</b></div>
<div class="stat">Success: <b id="s-success" class="ok">0</b></div>
<div class="stat">Cost: <b id="s-cost">$0.00</b></div>
<div class="stat">Events: <b id="s-events">0</b></div>
<div class="stat" id="s-status">&bull; waiting...</div>
</header>
<section id="filters">
<label><input type="checkbox" id="f-follow" checked>Follow</label>
<input type="text" id="f-search" placeholder="Search events..." autocomplete="off">
<label><input type="checkbox" class="f-type" value="phase_end" checked>phase</label>
<label><input type="checkbox" class="f-type" value="issue_start" checked>issue_start</label>
<label><input type="checkbox" class="f-type" value="issue_end" checked>issue_end</label>
<label><input type="checkbox" class="f-type" value="verify" checked>verify</label>
<label><input type="checkbox" class="f-type" value="failure" checked>failure</label>
<label><input type="checkbox" class="f-type" value="idle" checked>idle</label>
<label><input type="checkbox" class="f-type" value="review" checked>review</label>
<label><input type="checkbox" class="f-type" value="ci" checked>ci</label>
</section>
<main id="log"></main>
<script>
const logEl = document.getElementById("log");
const stats = { issues: new Set(), success: 0, cost: 0, events: 0 };
const state = { follow: true, search: "", types: new Set(["phase_end","issue_start","issue_end","verify","failure","idle","review","ci"]) };

function fmtT(ts) {
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 8) + "." + String(d.getMilliseconds()).padStart(3, "0");
}
function fmtDur(ms) {
  if (ms >= 60000) return (ms / 60000).toFixed(1) + "m";
  if (ms >= 1000) return (ms / 1000).toFixed(1) + "s";
  return ms + "ms";
}
function issueTag(n) { return n ? "#" + n : ""; }

function renderRow(e) {
  let body = "";
  if (e.type === "issue_start") body = `<span>${e.title || ""}</span> <span class="dim">attempt ${e.attempt}</span>`;
  else if (e.type === "issue_end") body = `outcome=<b class="${e.outcome==='success'?'ok':'fail'}">${e.outcome}</b> <span class="dim">$${(e.cost_usd||0).toFixed(4)}</span>`;
  else if (e.type === "phase_end") body = `${e.phase} <span class="dim">${fmtDur(e.duration_ms)} · $${(e.cost_usd||0).toFixed(4)} · ${e.tokens_in||0}/${e.tokens_out||0} tok</span>`;
  else if (e.type === "verify") body = `${e.stage} <b class="${e.passed?'ok':'fail'}">${e.passed?'PASS':'FAIL'}</b>`;
  else if (e.type === "failure") body = `<b class="fail">${e.category}</b>`;
  else if (e.type === "idle") body = `<span class="fail">${e.reason}</span>`;
  else if (e.type === "review") body = `${e.severity} ${e.file||""}: <span class="dim">${(e.description||"").slice(0,120)}</span>`;
  else if (e.type === "ci") body = `attempt ${e.attempt} <b class="${e.passed?'ok':'fail'}">${e.passed?'PASS':'FAIL'}</b>`;
  else body = JSON.stringify(e);

  const row = document.createElement("div");
  row.className = "row";
  row.dataset.type = e.type;
  row.innerHTML = `<span class="t">${fmtT(e.ts)}</span><span class="tag">${e.type}</span><span class="issue">${issueTag(e.issue)}</span><span class="body">${body}</span>`;
  return row;
}

function shouldShow(e) {
  if (!state.types.has(e.type)) return false;
  if (state.search && !JSON.stringify(e).toLowerCase().includes(state.search.toLowerCase())) return false;
  return true;
}

function accumulate(e) {
  stats.events++;
  if (e.type === "issue_start" && e.issue) stats.issues.add(e.issue);
  if (e.type === "issue_end" && e.outcome === "success") stats.success++;
  if (typeof e.cost_usd === "number") stats.cost += e.cost_usd;
  document.getElementById("s-issues").textContent = stats.issues.size;
  document.getElementById("s-success").textContent = stats.success;
  document.getElementById("s-cost").textContent = "$" + stats.cost.toFixed(4);
  document.getElementById("s-events").textContent = stats.events;
  document.getElementById("s-status").textContent = "● live";
  document.getElementById("s-status").style.color = "#6ee7b7";
}

function onEvent(e) {
  accumulate(e);
  if (!shouldShow(e)) return;
  const row = renderRow(e);
  logEl.appendChild(row);
  if (state.follow) window.scrollTo(0, document.body.scrollHeight);
}

document.getElementById("f-follow").addEventListener("change", ev => { state.follow = ev.target.checked; });
document.getElementById("f-search").addEventListener("input", ev => { state.search = ev.target.value; rerender(); });
document.querySelectorAll(".f-type").forEach(cb => cb.addEventListener("change", ev => {
  if (ev.target.checked) state.types.add(ev.target.value); else state.types.delete(ev.target.value);
  rerender();
}));
let buffer = [];
function rerender() {
  logEl.innerHTML = "";
  buffer.filter(shouldShow).forEach(e => logEl.appendChild(renderRow(e)));
  if (state.follow) window.scrollTo(0, document.body.scrollHeight);
}

const src = new EventSource("/events");
src.onmessage = function (msg) {
  try {
    const e = JSON.parse(msg.data);
    buffer.push(e);
    if (buffer.length > 2000) buffer = buffer.slice(-1500);
    onEvent(e);
  } catch (_) {}
};
src.onerror = function () {
  document.getElementById("s-status").textContent = "● disconnected";
  document.getElementById("s-status").style.color = "#ff7e8a";
};
</script></body></html>
"""


def _make_handler(bus: EventBus):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # silence access log

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/" or self.path == "/index.html":
                self._write_bytes(200, "text/html; charset=utf-8", _DASHBOARD_HTML.encode("utf-8"))
                return
            if self.path == "/events":
                self._stream_events()
                return
            if self.path == "/api/state":
                payload = json.dumps({"events": bus.snapshot()}).encode("utf-8")
                self._write_bytes(200, "application/json", payload)
                return
            self._write_bytes(404, "text/plain", b"not found")

        def _write_bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _stream_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = bus.subscribe()
            try:
                while True:
                    try:
                        event = q.get(timeout=_SSE_KEEPALIVE_SECONDS)
                        payload = f"data: {json.dumps(event)}\n\n".encode("utf-8")
                        self.wfile.write(payload)
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                bus.unsubscribe(q)

    return Handler


class DashboardServer:
    """Localhost HTTP + SSE dashboard. start() returns bound port."""

    def __init__(self, bus: EventBus, port: int = 8765, host: str = "127.0.0.1") -> None:
        self._bus = bus
        self._port = port
        self._host = host
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> int:
        handler = _make_handler(self._bus)
        self._httpd = ThreadingHTTPServer((self._host, self._port), handler)
        actual_port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return actual_port

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"
