from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import requests
import pytest
from futures.telegram import TelegramAlerts, format_alert
from tests.fixtures import setup, verified


def data():
    snap, account = setup()
    result = verified(snap, account)
    assert result.status == "VALID"
    return {"snapshot": snap, "result": result, "status": "VALID", "monotonic": 100,
            "ai": {"ok": True, "decision": "LONG", "reasons": ["Fixture"], "cautions": []}}


class Response:
    def __init__(self, status=200, body=None):
        self.status_code, self.body = status, {"ok": True} if body is None else body
    def json(self):
        return self.body


class Session:
    def __init__(self, responses=None):
        self.calls, self.responses = [], list(responses or [])
    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        value = self.responses.pop(0) if self.responses else Response()
        if isinstance(value, Exception):
            raise value
        return value


def notifier(tmp_path, session, clock=lambda: 1000, monotonic=lambda: 100):
    return TelegramAlerts("123:test-token-only", "-100123", tmp_path/"history.sqlite3", session, clock, monotonic)


@pytest.mark.parametrize("status", ["WAIT", "NO_TRADE"])
def test_non_trade_status_never_sends(tmp_path, status):
    d = data()
    d["status"] = status
    s = Session()
    assert "Diam" in notifier(tmp_path, s).send(d)
    assert not s.calls


@pytest.mark.parametrize("bad", ["wrong_direction", "ai_failed", "ai_wait", "stale", "expired", "missing_quote", "failed_check", "no_plan"])
def test_invalid_or_stale_approval_never_sends(tmp_path, bad):
    d = data()
    if bad == "wrong_direction": d["ai"]["decision"] = "SHORT"
    if bad == "ai_failed": d["ai"]["ok"] = False
    if bad == "ai_wait": d["ai"]["decision"] = "WAIT"
    if bad == "stale": d["monotonic"] = 69
    if bad == "expired": d["result"].plan["expires_ms"] = d["snapshot"].now_ms
    if bad == "missing_quote": d["snapshot"].quote_times.pop("book")
    if bad == "failed_check": d["result"].checks[0]["Status"] = "GAGAL"
    if bad == "no_plan": d["result"].plan = None
    s = Session()
    notifier(tmp_path, s).send(d)
    assert not s.calls


def test_success_deduplicated_across_instances_and_quantity_changes(tmp_path):
    d, s = data(), Session()
    assert "terkirim" in notifier(tmp_path, s).send(d)
    d["result"].plan["qty"] += .001
    assert "duplikat" in notifier(tmp_path, s).send(d)
    assert len(s.calls) == 1
    payload = s.calls[0][1]["json"]
    assert payload["chat_id"] == "-100123"
    assert "BUY / LONG | INTRADAY" in payload["text"]
    assert len(payload["text"]) < 4096
    assert "test-token-only" not in payload["text"]
    assert s.calls[0][1]["allow_redirects"] is False
    d["result"].plan["signal_close_ms"] += 900000
    assert "terkirim" in notifier(tmp_path, s).send(d)
    assert len(s.calls) == 2


def test_atomic_reservation_across_two_clients(tmp_path):
    d, s = data(), Session()
    # Initialize schema before contending for the same alert reservation.
    clients = [notifier(tmp_path, s) for _ in range(2)]
    clients[0].connect().close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda c: c.send(deepcopy(d)), clients))
    assert len(s.calls) == 1


@pytest.mark.parametrize("mode,label", [("SCALPING", "SCALPER"), ("INTRADAY", "INTRADAY"), ("SWING", "SWING")])
@pytest.mark.parametrize("direction,side", [("LONG", "BUY / LONG"), ("SHORT", "SELL / SHORT")])
def test_labels_and_trade_fields(mode, label, direction, side):
    d = data()
    d["result"].plan.update(mode=mode, direction=direction)
    text = format_alert(d)
    assert f"{side} | {label}" in text
    for field in ("Trigger entry", "Limit entry", "SL:", "TP1:", "TP2:", "Runner:", "Risiko", "WIB", "MENUNGGU TRIGGER"):
        assert field in text


def test_429_waits_without_loop_then_retries_only_fresh_signal(tmp_path):
    now = [1000]
    s = Session([Response(429, {"ok": False, "parameters": {"retry_after": 90}}), Response()])
    n = notifier(tmp_path, s, clock=lambda: now[0])
    assert "rate limit" in n.send(data())
    now[0] += 30
    assert "cooldown" in n.send(data())
    assert len(s.calls) == 1
    now[0] += 61
    assert "terkirim" in n.send(data())
    assert len(s.calls) == 2


def test_timeout_redacted_and_not_retried(tmp_path):
    s = Session([requests.Timeout("https://api.telegram.org/bot123:test-token-only/sendMessage")])
    message = notifier(tmp_path, s).send(data())
    assert "tidak pasti" in message and "test-token-only" not in message
    notifier(tmp_path, s).send(data())
    assert len(s.calls) == 1


def test_missing_secrets_no_network(tmp_path):
    s = Session()
    n = TelegramAlerts("", "", tmp_path/"test.db", s, monotonic=lambda: 100)
    assert "Secrets" in n.send(data())
    assert not s.calls


def test_verified_short_sends_sell(tmp_path):
    from tests.test_strategy import short_setup
    snap, account = short_setup()
    result = verified(snap, account)
    d = {"snapshot": snap, "result": result, "status": "VALID", "monotonic": 100,
         "ai": {"ok": True, "decision": "SHORT", "reasons": ["Fixture"], "cautions": []}}
    s = Session()
    assert "terkirim" in notifier(tmp_path, s).send(d)
    assert "SELL / SHORT | INTRADAY" in s.calls[0][1]["json"]["text"]
