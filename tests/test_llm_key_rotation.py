"""A second API key (<KEY_ENV>2) gets its own daily quota, so it is tried on the same model
before dropping to the fallback model."""

import src.llm as llm


class _Fake:
    def __init__(self, model, key):
        self.model, self.key, self.fallbacks = model, key, None

    def with_fallbacks(self, rest):
        self.fallbacks = rest
        return self


def _chain(monkeypatch, env):
    for name in ("GOOGLE_API_KEY", "GOOGLE_API_KEY2"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(llm, "_build", lambda cfg, model, temp, streaming, key=None: _Fake(model, key))
    cfg = {"provider": "gemini", "api_key_env": "GOOGLE_API_KEY", "fallback_model": "fb"}
    head = llm._with_fallback(cfg, "primary", 0.1, False)
    return [(head.model, head.key)] + [(f.model, f.key) for f in head.fallbacks]


def test_second_key_is_tried_on_the_primary_model_before_the_fallback_model(monkeypatch):
    assert _chain(monkeypatch, {"GOOGLE_API_KEY": "k1", "GOOGLE_API_KEY2": "k2"}) == [
        ("primary", "k1"), ("primary", "k2"), ("fb", "k1"), ("fb", "k2")]


def test_without_a_second_key_the_chain_is_unchanged(monkeypatch):
    assert _chain(monkeypatch, {"GOOGLE_API_KEY": "k1"}) == [("primary", "k1"), ("fb", "k1")]


def test_no_fallback_model_and_one_key_returns_the_bare_model(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY2", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "k1")
    monkeypatch.setattr(llm, "_build", lambda cfg, model, temp, streaming, key=None: _Fake(model, key))
    out = llm._with_fallback({"provider": "gemini", "api_key_env": "GOOGLE_API_KEY"}, "primary", 0.1, False)
    assert out.fallbacks is None and (out.model, out.key) == ("primary", "k1")


def test_token_tracker_counts_calls_per_model():
    llm.reset_token_tracking()
    resp = type("R", (), {"llm_output": {"model_name": "gemini-3.5-flash-lite"}, "generations": []})()
    llm._token_tracker.on_llm_end(resp)
    llm._token_tracker.on_llm_end(resp)
    assert llm.get_token_usage()["models"] == {"gemini-3.5-flash-lite": 2}
    llm.reset_token_tracking()
    assert llm.get_token_usage()["models"] == {}
