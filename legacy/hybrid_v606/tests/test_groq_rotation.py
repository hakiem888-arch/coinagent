import json
import requests
import pytest
from futures.models import Result
from futures.groq_rotation import GroqRotation, combine, load_keys, retry_seconds


class Response:
    def __init__(self, status=200, decision="LONG", headers=None, payload=None, finish="stop"):
        self.status_code = status
        self.headers = headers or {}
        self.payload = payload if payload is not None else {"choices": [{"finish_reason": finish, "message": {
            "content": json.dumps({"decision": decision, "reasons": ["Sesuai data."], "cautions": []})}}]}

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0) if self.responses else Response()
        if isinstance(response, Exception):
            raise response
        return response


def test_seven_keys_rotate_on_success_and_wrap():
    now = [1000.]
    session = Session()
    keys = [f"test-key-{i}" for i in range(7)]
    pool = GroqRotation(keys, session, clock=lambda: now[0])
    for i in range(9):
        result = pool.review(Result(status="VALID", direction="LONG"))
        assert result["ok"]
        assert result["slot"] == i % 7 + 1
        assert session.calls[-1][1]["headers"]["Authorization"] == "Bearer " + keys[i % 7]
        now[0] += 3
    assert all("test-key" not in str(x) for x in pool.status())


def test_invalid_key_disabled_then_next_key():
    session = Session([Response(401), Response(200)])
    pool = GroqRotation(["bad-key", "good-key"], session)
    assert pool.review(Result())["slot"] == 2
    assert 0 in pool.disabled


def test_organization_429_stops_without_burning_other_keys():
    now = [1000.]
    session = Session([Response(429, headers={"retry-after": "120"})])
    pool = GroqRotation([f"test-key-{i}" for i in range(7)], session, clock=lambda: now[0])
    assert not pool.review(Result())["ok"]
    now[0] += 60
    assert not pool.review(Result())["ok"]
    assert len(session.calls) == 1
    now[0] += 61
    assert pool.review(Result())["ok"]
    assert len(session.calls) == 2


def test_retry_after_date_and_nan():
    assert retry_seconds({"Retry-After": "Thu, 01 Jan 1970 00:02:00 GMT"}, 60) == 60
    assert retry_seconds({"retry-after": "NaN"}, 0) == 60


@pytest.mark.parametrize("error", [Response(503), requests.Timeout("secret-test-key")])
def test_transient_failure_uses_next_slot(error):
    session = Session([error, Response()])
    pool = GroqRotation(["test-key-1", "test-key-2"], session)
    assert pool.review(Result())["slot"] == 2


def test_retry_count_is_bounded():
    session = Session([Response(503)] * 7)
    pool = GroqRotation([f"test-key-{i}" for i in range(7)], session)
    assert not pool.review(Result())["ok"]
    assert len(session.calls) == 3


def test_model_configuration_error_does_not_rotate_all_keys():
    session = Session([Response(400)])
    pool = GroqRotation(["test-key-1", "test-key-2"], session)
    assert not pool.review(Result())["ok"]
    assert len(session.calls) == 1


@pytest.mark.parametrize("content", ['{"decision":"LONG","reasons":[],"cautions":[]}',
    '{"decision":"LONG","reasons":["x"],"cautions":[],"entry":10}', 'not-json',
    '{"decision":"LONG","reasons":[NaN],"cautions":[]}',
    '{"decision":"BUY","reasons":["x"],"cautions":[]}'])
def test_invalid_json_and_unallowed_numeric_instructions_rejected(content):
    response = Response(payload={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
    pool = GroqRotation(["test-key"], Session([response]))
    assert not pool.review(Result())["ok"]


def test_truncated_response_not_used():
    pool = GroqRotation(["test-key"], Session([Response(finish="length")]))
    assert not pool.review(Result())["ok"]


def test_secrets_redacted_from_provider_content():
    raw = json.dumps({"decision": "WAIT", "reasons": ["test-key-abcdef gsk_fakeexample"], "cautions": []})
    response = Response(payload={"choices": [{"finish_reason": "stop", "message": {"content": raw}}]})
    pool = GroqRotation(["test-key-abcdef"], Session([response]))
    result = pool.review(Result())
    assert "test-key-abcdef" not in str(result) and "gsk_fakeexample" not in str(result)


@pytest.mark.parametrize("baseline", ["WAIT", "NO_TRADE"])
def test_ai_cannot_override_failed_baseline(baseline):
    assert combine(Result(status=baseline, direction="LONG"), {"ok": True, "decision": "LONG"}) == baseline


def test_ai_wrong_direction_or_outage_never_approves():
    r = Result(status="VALID", direction="LONG")
    assert combine(r, {"ok": True, "decision": "SHORT"}) == "WAIT"
    assert combine(r, {"ok": False, "decision": "LONG"}) == "WAIT"
    assert combine(r, None) == "WAIT"
    assert combine(r, {"ok": True, "decision": "NO_TRADE"}) == "NO_TRADE"


def test_credential_formats_and_duplicates():
    env = {"GROQ_API_KEYS": ["a", "b"], "GROQ_API_KEY": "a", "GROQ_API_KEY_3": "c"}
    assert load_keys(env.get) == ["a", "b", "c"]
    with pytest.raises(ValueError):
        load_keys({"GROQ_API_KEYS": list("abcdefgh")}.get)


def test_no_keys_no_network():
    session = Session()
    assert GroqRotation([], session).review(Result())["decision"] == "WAIT"
    assert session.calls == []


def test_original_masterprompt_is_sent_verbatim_in_system_message():
    from pathlib import Path
    rules = (Path(__file__).resolve().parents[1]/"STRATEGY_RULES.txt").read_text()
    session = Session()
    GroqRotation(["test-key"], session).review(Result())
    messages = session.calls[0][1]["json"]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith(rules)
    assert "baseline_status VALID" in messages[0]["content"]


def test_missing_masterprompt_blocks_ai_request(monkeypatch):
    import futures.groq_rotation as module
    def missing():
        raise FileNotFoundError("fixture")
    monkeypatch.setattr(module, "master_system", missing)
    session = Session()
    result = GroqRotation(["test-key"], session).review(Result())
    assert not result["ok"] and not session.calls
