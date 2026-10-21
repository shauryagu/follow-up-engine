import pytest


@pytest.fixture(autouse=True)
def _never_hit_llm_network(monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
