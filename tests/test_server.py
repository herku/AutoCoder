"""Tests for the dashboard EventBus and DashboardServer."""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request

import pytest

from autocoder.server import DashboardServer, EventBus


def test_bus_publishes_and_subscriber_receives():
    bus = EventBus()
    q = bus.subscribe()
    bus.publish("phase_end", {"issue": 1, "phase": "implement", "cost_usd": 0.01})
    event = q.get(timeout=1.0)
    assert event["type"] == "phase_end"
    assert event["issue"] == 1
    assert event["phase"] == "implement"
    assert "ts" in event


def test_bus_replays_ring_on_late_subscribe():
    bus = EventBus(ring_size=3)
    for i in range(5):
        bus.publish("phase_end", {"issue": i})
    q = bus.subscribe()
    replayed = []
    for _ in range(3):
        replayed.append(q.get(timeout=1.0)["issue"])
    # Only the last 3 (ring_size) should survive
    assert replayed == [2, 3, 4]


def test_bus_ring_limits_retention():
    bus = EventBus(ring_size=2)
    for i in range(10):
        bus.publish("phase_end", {"issue": i})
    snap = bus.snapshot()
    assert [e["issue"] for e in snap] == [8, 9]


def test_bus_unsubscribe_stops_delivery():
    bus = EventBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.publish("phase_end", {"issue": 1})
    with pytest.raises(queue.Empty):
        q.get_nowait()


def test_bus_publish_never_blocks_when_subscriber_full():
    bus = EventBus()
    q = bus.subscribe()
    # Fill subscriber queue past capacity via direct put_nowait to simulate slow consumer
    # (publish drops silently when full; should not raise)
    for i in range(10):
        bus.publish("phase_end", {"issue": i})
    # Subscriber should have received some events without exception
    got = 0
    while True:
        try:
            q.get_nowait()
            got += 1
        except queue.Empty:
            break
    assert got >= 1


def test_bus_multiple_subscribers_each_see_events():
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    bus.publish("verify", {"issue": 7, "stage": "lint", "passed": True})
    e1 = q1.get(timeout=1.0)
    e2 = q2.get(timeout=1.0)
    assert e1["issue"] == 7 and e2["issue"] == 7


def _find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    bus = EventBus()
    srv = DashboardServer(bus, port=_find_free_port())
    port = srv.start()
    yield bus, srv, port
    srv.stop()


def test_server_serves_dashboard_html(server):
    _, _, port = server
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as resp:
        assert resp.status == 200
        body = resp.read().decode("utf-8")
    assert "<!doctype html>" in body.lower()
    assert "AutoCoder" in body
    assert "/events" in body


def test_server_state_endpoint_returns_snapshot(server):
    bus, _, port = server
    bus.publish("issue_start", {"issue": 1, "attempt": 1, "title": "foo"})
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as resp:
        data = json.loads(resp.read())
    assert "events" in data
    assert any(e.get("issue") == 1 for e in data["events"])


def test_server_sse_stream_delivers_events(server):
    bus, _, port = server

    received: list[dict] = []
    stop = threading.Event()

    def consume() -> None:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/events")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            # read lines until stop
            while not stop.is_set():
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8").rstrip()
                if text.startswith("data: "):
                    received.append(json.loads(text[6:]))
                    if len(received) >= 2:
                        break

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    time.sleep(0.2)  # let subscriber attach
    bus.publish("phase_end", {"issue": 1, "phase": "implement", "cost_usd": 0.1, "duration_ms": 10})
    bus.publish("verify", {"issue": 1, "stage": "lint", "passed": True, "duration_ms": 100})
    t.join(timeout=3.0)
    stop.set()

    assert len(received) >= 2
    assert received[0]["type"] == "phase_end"
    assert received[1]["type"] == "verify"


def test_server_returns_404_for_unknown_path(server):
    _, _, port = server
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=2)
        raised = False
    except urllib.error.HTTPError as e:
        assert e.code == 404
        raised = True
    assert raised
