from aero.toolbox import download_progress


def test_download_ticks_are_throttled_but_completion_is_immediate(monkeypatch):
    messages = []
    clock = [0.0]
    monkeypatch.setattr(download_progress.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(download_progress, "emit_progress", messages.append)
    report = download_progress.download_progress_reporter()
    for tick in range(1000):
        clock[0] = tick / 100
        report(tick, 1000)
    assert len(messages) == 2
    report(1000, 1000)
    assert len(messages) == 3
    assert "100%" in messages[-1]
    report(1000, 1000)
    assert len(messages) == 3


def test_concurrent_downloads_have_independent_reporting_windows(monkeypatch):
    messages = []
    monkeypatch.setattr(download_progress.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(download_progress, "emit_progress", messages.append)
    first = download_progress.download_progress_reporter()
    second = download_progress.download_progress_reporter()
    first(0, 100)
    second(0, 100)
    assert len(messages) == 2
    assert messages[0].split()[0] != messages[1].split()[0]
