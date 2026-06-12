"""Tests for automatic clipboard copying after text selection."""

from types import SimpleNamespace

from aero.cli.main import AeroApp


def test_copy_text_to_clipboard_uses_pbcopy_on_macos(monkeypatch):
    calls = []
    app = SimpleNamespace()

    monkeypatch.setattr("aero.cli.main.sys.platform", "darwin")
    monkeypatch.setattr(
        "aero.cli.main.subprocess.run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    AeroApp._copy_text_to_clipboard(app, "selected text")

    assert calls == [
        (
            ["pbcopy"],
            {"input": "selected text", "text": True, "check": True},
        )
    ]


def test_text_selection_is_copied_and_notified():
    copied = []
    notifications = []
    event = SimpleNamespace(stop=lambda: copied.append("stopped"))
    app = SimpleNamespace(
        screen=SimpleNamespace(get_selected_text=lambda: "selected text"),
        _copy_text_to_clipboard=lambda text: copied.append(text),
        notify=lambda message, **kwargs: notifications.append((message, kwargs)),
    )

    AeroApp.on_text_selected(app, event)

    assert copied == ["stopped", "selected text"]
    assert notifications == [("已复制选中文字", {"timeout": 1.5})]


def test_empty_text_selection_is_ignored():
    copied = []
    app = SimpleNamespace(
        screen=SimpleNamespace(get_selected_text=lambda: ""),
        _copy_text_to_clipboard=copied.append,
    )

    AeroApp.on_text_selected(app, SimpleNamespace(stop=lambda: None))

    assert copied == []
