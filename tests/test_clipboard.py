from log_search.clipboard import copy_text


class App:
    clipboard = None

    def copy_to_clipboard(self, text):
        self.clipboard = text


async def test_native_clipboard_receives_content_only_via_stdin(tmp_path, monkeypatch):
    executable = tmp_path / "wl-copy"
    capture = tmp_path / "copied"
    executable.write_text("#!/bin/sh\ncat > \"$COPY_TEST_FILE\"\n")
    executable.chmod(0o755)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    monkeypatch.setenv("COPY_TEST_FILE", str(capture))
    monkeypatch.setenv("PATH", str(tmp_path) + ":/usr/bin:/bin")
    app = App()
    text = "日志\n$(touch /tmp/should-not-exist) 'quoted'"
    assert await copy_text(app, text) == "已复制到系统剪贴板"
    assert capture.read_text() == text
    assert app.clipboard is None


async def test_missing_native_clipboard_uses_explicit_terminal_request(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    app = App()
    assert "OSC52" in await copy_text(app, "copied text")
    assert app.clipboard == "copied text"


async def test_failed_native_clipboard_does_not_claim_success(tmp_path, monkeypatch):
    executable = tmp_path / "wl-copy"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o755)
    monkeypatch.setenv("WAYLAND_DISPLAY", "unavailable-session")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    app = App()
    result = await copy_text(app, "log text")
    assert "OSC52" in result and "已复制到系统" not in result
    assert app.clipboard == "log text"
