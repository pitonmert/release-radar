import runpy

from release_radar import service


def test_module_entrypoint_starts_service(monkeypatch):
    started = []
    monkeypatch.setattr(service, "main", lambda: started.append(True))
    runpy.run_module("release_radar", run_name="__main__")
    assert started == [True]
