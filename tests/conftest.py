import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import requests


@pytest.fixture(autouse=True)
def no_real_http_in_tests(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must mock HTTP; no live API or Telegram message is allowed")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
