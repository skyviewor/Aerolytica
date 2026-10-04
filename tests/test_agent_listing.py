from io import StringIO

import pytest
from rich.cells import cell_len
from rich.console import Console

from aero.cli.agent_listing import activity_time, render_agent_list


@pytest.mark.parametrize("width", [60, 80, 110, 160])
def test_listing_fits_terminal_with_chinese_and_long_values(width):
    stream = StringIO()
    console = Console(file=stream, width=width, color_system=None)
    render_agent_list(
        [
            {
                "name": "中文智能体" * 10,
                "agent_id": "agent_" + "a" * 32,
                "status": "online",
                "description": "测试备注" * 30,
                "last_seen_at": "2026-10-03T13:14:11.874948+08:00",
            }
        ],
        console,
    )
    output = stream.getvalue()
    assert all(cell_len(line) <= width for line in output.splitlines())
    assert "在线（待命）" in output
    assert "13:14:11" in output and ".874948" not in output
    assert "\t" not in output


def test_names_are_not_interpreted_as_terminal_markup():
    stream = StringIO()
    render_agent_list(
        [{"name": "[red]agent[/red]", "status": "offline"}],
        Console(file=stream, width=80, color_system=None),
    )
    assert "[red]agent[/red]" in stream.getvalue()


def test_activity_time_is_beijing_time_and_handles_missing():
    assert activity_time("2026-10-03T05:14:11Z") == "2026-10-03 13:14:11"
    assert activity_time(None) == "—"
    assert activity_time("invalid") == "—"
