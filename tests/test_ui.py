from pathlib import Path
import pytest
import requests
from types import SimpleNamespace
from streamlit.testing.v1 import AppTest
from futures.market import FuturesMarket, MarketError
from futures.groq_rotation import GroqRotation
from tests.fixtures import setup, NOW

APP = Path(__file__).resolve().parents[1] / "manual_app.py"


def by_label(elements, label):
    return next(x for x in elements if x.label == label)


def app(monkeypatch, tmp_path):
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(tmp_path / "history.sqlite3"))
    return AppTest.from_file(str(APP), default_timeout=20).run()


def test_initial_screen_secrets_only_and_scan_controls(monkeypatch, tmp_path):
    at = app(monkeypatch, tmp_path)
    assert not at.exception
    assert not [x for x in at.text_input if x.label.startswith("Key ")]
    assert not [x for x in at.text_input if x.type == "password"]
    assert by_label(at.selectbox, "Interval scan (detik)").options == ["30", "45", "60", "90"]
    assert by_label(at.selectbox, "Interval scan (detik)").value == 60
    assert not by_label(at.toggle, "Auto-scan").value
    assert not by_label(at.toggle, "Tampilkan grafik").value
    assert [x.label for x in at.tabs] == ["Analisis", "Evaluasi fill", "Riwayat"]


def test_network_failure_becomes_wait(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise MarketError("HTTP 451")
    monkeypatch.setattr(FuturesMarket, "load", fail)
    at = app(monkeypatch, tmp_path)
    by_label(at.button, "Analisis").click().run()
    assert not at.exception
    assert any("WAIT" in x.value and "451" in x.value for x in at.error)


def test_secrets_configured_without_visible_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(tmp_path / "history.sqlite3"))
    at = AppTest.from_file(str(APP), default_timeout=20)
    at.secrets["GROQ_API_KEY"] = "test-secret-groq-only"
    at.secrets["TELEGRAM_BOT_TOKEN"] = "123:test-secret-telegram-only"
    at.secrets["TELEGRAM_CHAT_ID"] = "-100987"
    at.run()
    assert not at.exception
    captions = " ".join(x.value for x in at.caption)
    assert "1/7 key" in captions and "Telegram: terkonfigurasi" in captions
    assert "test-secret" not in captions
    assert not [x for x in at.text_input if x.type == "password"]


@pytest.mark.parametrize("interval", [30, 45, 60, 90])
def test_auto_scan_interval_wait_silence_and_stop(monkeypatch, tmp_path, interval):
    calls, reviews = [], []
    def load(*args, **kwargs):
        calls.append(1)
        return setup()[0]
    def review(self, result, model):
        reviews.append(result.status)
        return {"ok": True, "decision": "WAIT", "reasons": ["Fixture only"], "cautions": []}
    monkeypatch.setattr(FuturesMarket, "load", load)
    monkeypatch.setattr(GroqRotation, "review", review)
    at = app(monkeypatch, tmp_path)
    by_label(at.selectbox, "Interval scan (detik)").set_value(interval)
    by_label(at.toggle, "Auto-scan").set_value(True).run()
    assert not at.exception
    assert len(calls) == len(reviews) == 1
    assert "Diam" in at.session_state["analysis"]["telegram"]
    # UI reruns before the deadline do not scan or call AI again.
    at.run()
    assert len(calls) == 1
    at.session_state["scan_schedule"].next_due = 0
    at.run()
    assert not at.exception
    assert len(calls) == len(reviews) == 2
    by_label(at.toggle, "Auto-scan").set_value(False).run()
    at.session_state["scan_schedule"].next_due = 0
    at.run()
    assert len(calls) == 2


@pytest.mark.parametrize("crossed_after_ai", [False, True])
def test_full_ui_draft_liquidation_ai_approved_and_input_change(monkeypatch, tmp_path, crossed_after_ai):
    calls = [0]
    messages = []
    def post(self, url, **kwargs):
        assert url == "https://api.telegram.org/bot123:fixture-only/sendMessage"
        messages.append(kwargs["json"])
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})
    monkeypatch.setattr(requests.Session, "post", post)
    def load(*args, **kwargs):
        calls[0] += 1
        snap = setup()[0]
        if crossed_after_ai and calls[0] >= 3:
            snap.open_bar_high = 120
        return snap
    monkeypatch.setattr(FuturesMarket, "load", load)
    monkeypatch.setattr(GroqRotation, "review", lambda *args, **kwargs: {"ok": True, "decision": "LONG", "reasons": ["Fixture only"], "cautions": [], "slot": 1, "model": "TEST"})
    at = app(monkeypatch, tmp_path)
    at.secrets["TELEGRAM_BOT_TOKEN"] = "123:fixture-only"
    at.secrets["TELEGRAM_CHAT_ID"] = "-100123"
    for label in ["Margin isolated, auto-add margin mati", "Data akun sudah saya cocokkan dengan bursa",
                  "Fee terverifikasi dan cadangan sudah saya evaluasi", "Fitur order dan trigger sudah saya verifikasi",
                  "Saya sudah memeriksa CPI, FOMC, dan konferensi pers"]:
        by_label(at.checkbox, label).check()
    by_label(at.number_input, "Fee taker akun (%)").set_value(.04)
    by_label(at.number_input, "Cadangan slippage exit (bps)").set_value(.1)
    by_label(at.number_input, "Cadangan funding total horizon (%)").set_value(.001)
    by_label(at.text_input, "Sumber kalender CPI/FOMC").set_value("synthetic fixture calendar")
    by_label(at.text_input, "Awal jendela tanpa event (UTC)").set_value("2026-10-24 10:00")
    by_label(at.text_input, "Akhir jendela tanpa event (UTC)").set_value("2026-10-24 23:00")
    by_label(at.text_input, "Waktu pemeriksaan kalender (UTC)").set_value("2026-10-24 11:59")
    from datetime import date
    by_label(at.date_input, "Tanggal akun (UTC)").set_value(date(2026, 10, 24))
    by_label(at.button, "Analisis").click().run()
    assert not at.exception
    data = at.session_state["analysis"]
    assert data["result"].status == "WAIT"
    assert not messages
    p = data["result"].candidate
    assert p is not None, data["result"].reasons
    by_label(at.number_input, "Harga likuidasi dari bursa/kalkulator").set_value(p["sl"] - 3*p["atr"])
    by_label(at.text_input, "Sumber dan risk tier").set_value("Fixture exchange tier")
    by_label(at.text_input, "Waktu verifikasi UTC").set_value("2026-10-24 12:00:00")
    by_label(at.checkbox, "Nilai sesuai Q, E, leverage, isolated, dan risk tier draf ini").check()
    by_label(at.button, "Analisis").click().run()
    assert not at.exception
    if crossed_after_ai:
        assert at.session_state["analysis"]["status"] == "WAIT"
        assert not any("SETUP LONG VALID" in x.value for x in at.success)
        assert not messages
        return
    assert at.session_state["analysis"]["status"] == "VALID"
    assert any("SETUP LONG VALID" in x.value for x in at.success)
    assert len(messages) == 1 and "BUY / LONG | INTRADAY" in messages[0]["text"]
    by_label(at.button, "Analisis").click().run()
    assert not at.exception and len(messages) == 1
    by_label(at.number_input, "Ekuitas (USDT)").set_value(2000).run()
    assert not at.exception
    assert any("Pengaturan berubah" in x.value for x in at.info)
    assert not any("SETUP LONG VALID" in x.value for x in at.success)
