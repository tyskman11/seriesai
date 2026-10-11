from seriesai.backends.llamacpp import LlamaCppBackend


def test_extract_events_returns_validated_local_candidates(monkeypatch):
    backend = LlamaCppBackend("http://127.0.0.1:11435")
    monkeypatch.setattr(backend, "_request", lambda *args, **kwargs: {"response": '{"events":[{"event_type":"conflict","description":"A and B argue"}]}'})
    assert backend.extract_events("A and B argue") == [{"event_type": "conflict", "description": "A and B argue"}]


def test_check_continuity_returns_reviewable_candidates(monkeypatch):
    backend = LlamaCppBackend("http://127.0.0.1:11435")
    monkeypatch.setattr(backend, "_request", lambda *args, **kwargs: {"response": '{"issues":[{"severity":"warning","description":"Date missing","evidence":"outline"}]}'})
    assert backend.check_continuity({"outline": {}})[0]["severity"] == "warning"
