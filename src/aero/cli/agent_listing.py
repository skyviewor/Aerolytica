"""Readable, terminal-width-aware listing of local Agent registrations."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

STATUS = {
    "online": ("在线（待命）", "green"),
    "busy": ("在线（执行中）", "cyan"),
    "offline": ("离线", "dim"),
    "unknown": ("状态未知", "yellow"),
    "unavailable": ("云端不可见", "yellow"),
}


def activity_time(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "—"
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(ZoneInfo("Asia/Shanghai"))
        return timestamp.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "—"


def plain(value: Any) -> str:
    return str(value or "—").replace("\n", " ").replace("\r", " ").replace("\t", " ")


def render_agent_list(agents: list[dict[str, Any]], console: Console | None = None) -> None:
    console = console or Console()
    console.print(f"本机注册的智能体 · {len(agents)} 个", style="bold", markup=False)
    if console.width < 110:
        for index, agent in enumerate(agents, 1):
            console.rule(Text(f"{index}. {plain(agent.get('name'))}"), align="left")
            label, style = STATUS.get(agent.get("status"), STATUS["unknown"])
            console.print(Text("运行状态：") + Text(label, style=style))
            console.print(Text(f"智能体 ID：{plain(agent.get('agent_id'))}"))
            console.print(Text(f"最后活动：{activity_time(agent.get('last_seen_at'))}"))
            console.print(Text(f"描述：{plain(agent.get('description'))}"))
    else:
        table = Table(box=box.SIMPLE, expand=True, show_edge=False)
        table.add_column("名称", min_width=14, max_width=32, no_wrap=True, overflow="ellipsis")
        table.add_column("运行状态", min_width=14, no_wrap=True)
        table.add_column("智能体 ID", min_width=17, no_wrap=True)
        table.add_column("最后活动", min_width=19, no_wrap=True)
        table.add_column("描述", max_width=30, no_wrap=True, overflow="ellipsis")
        for agent in agents:
            label, style = STATUS.get(agent.get("status"), STATUS["unknown"])
            agent_id = plain(agent.get("agent_id"))
            short_id = agent_id if len(agent_id) <= 20 else f"{agent_id[:10]}…{agent_id[-6:]}"
            table.add_row(
                Text(plain(agent.get("name"))),
                Text(label, style=style),
                Text(short_id),
                Text(activity_time(agent.get("last_seen_at"))),
                Text(plain(agent.get("description"))),
            )
        console.print(table)
        console.print("时间为北京时间；完整 ID：aero agent status 名称", style="dim", markup=False)
    if any(agent.get("status") == "unavailable" for agent in agents):
        console.print(
            "云端不可见：云端不存在或当前账号无权查看；本机记录仍保留。", style="dim", markup=False
        )
