"""Per-user-turn search limits and exact-query replay, independent of provider."""

import asyncio
import copy
import json
import re
import uuid
from collections.abc import Awaitable, Callable

from aero.core.debug_log import debug_log

SEARCH_TOOLS = frozenset({"web_search", "bailian_web_search", "search_web"})
RESEARCH = re.compile(
    r"调研|综述|多项比较|多来源|全面分析|深入研究|对比|比较|\bresearch\b|\breview\b|\bcompare\b",
    re.I,
)


class SearchBudget:
    def __init__(self, limit: int = 2):
        self.limit = limit
        self.results: dict[str, dict] = {}
        self.reused = 0
        self._lock = asyncio.Lock()

    @property
    def remaining(self) -> int:
        return max(0, self.limit - len(self.results))

    async def execute(
        self, arguments: dict, call_id: str, execute: Callable[[], Awaitable[dict]]
    ) -> dict:
        normalized = dict(arguments)
        for key in ("query", "search_query"):
            if isinstance(normalized.get(key), str):
                normalized[key] = " ".join(normalized[key].split())
        cache_key = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
        async with self._lock:
            if cache_key in self.results:
                self.reused += 1
                result = copy.deepcopy(self.results[cache_key])
                result["search_reused"] = True
                reason = "duplicate_query"
            elif not self.remaining:
                result = {
                    "status": "blocked",
                    "is_error": True,
                    "content": (
                        "本轮搜索预算已用完。请根据已有证据回答并说明未核实的部分，"
                        "不要重试或换工具绕过。"
                    ),
                }
                reason = "budget_exhausted"
            else:
                # Claim before awaiting; errors and uncertain results consume this query slot too.
                self.results[cache_key] = {
                    "status": "unknown",
                    "is_error": True,
                    "content": "上次搜索结果未知，请勿自动重试。",
                    "search_call_id": call_id,
                }
                try:
                    result = await execute()
                except Exception as exc:
                    result = {"status": "unknown", "is_error": True, "content": str(exc)}
                result = dict(
                    result, search_call_id=call_id or uuid.uuid4().hex, search_reused=False
                )
                result.setdefault("status", "failed" if result.get("error") else "success")
                result.setdefault("source", result.get("provider", ""))
                self.results[cache_key] = copy.deepcopy(result)
                reason = result.get("status", "completed")
            result.update(search_remaining=self.remaining, search_limit=self.limit)
            debug_log(
                "agent.search_budget",
                queries=len(self.results),
                reused=self.reused,
                remaining=self.remaining,
                stop_reason=reason,
            )
            return result
