import pytest


@pytest.fixture(autouse=True)
def isolate_operation_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def legacy_entry_mode(request, monkeypatch):
    """Existing parser/UI tests explicitly exercise the retained entry mode."""
    if request.path.name in ("test_scan_modes.py", "test_scan_settings.py", "test_native_lines.py"):
        return
    from log_search import worker
    original = worker.validate

    def validate(value):
        if isinstance(value, dict) and value.get("action", "search") == "search":
            value.setdefault("result_mode", "entries")
            value.setdefault("filter_file_dates", False)
            value.setdefault("concurrency", 1)
        return original(value)

    monkeypatch.setattr(worker, "validate", validate)
    from log_search.tui import LogSearchApp
    init = LogSearchApp.__init__

    def initialize(self, *args, **kwargs):
        init(self, *args, **kwargs)
        self.settings.update(result_mode="entries", filter_file_dates=False, concurrency=1)

    monkeypatch.setattr(LogSearchApp, "__init__", initialize)
