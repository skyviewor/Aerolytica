"""Alibaba Cloud Model Studio web search client."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

WEB_SEARCH_MCP_URL = "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp"
WEB_SEARCH_CONSOLE_URL = "https://bailian.console.aliyun.com/cn-beijing/"
WEB_SEARCH_GUIDE_URL = "https://help.aliyun.com/zh/model-studio/web-search-for-coding-plan"
ZHIPU_WEB_SEARCH_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"


_URL_RE = re.compile(r"https?://[^\s<>'\"\])}]+")
_QUERY_FIELDS = ("query", "q", "keyword", "keywords", "search_query")
_LIMIT_FIELDS = ("count", "limit", "top_k", "max_results", "num_results")
_FRESHNESS_FIELDS = ("freshness", "freshness_days", "time_range", "date_range")
_DOMAIN_FIELDS = ("domains", "domain", "include_domains", "site")


async def search_bailian_web(
    api_key: str,
    query: str,
    *,
    limit: int = 8,
    freshness_days: int | None = None,
    domains: list[str] | None = None,
) -> dict[str, Any]:
    """Search the web through Bailian WebSearch MCP and normalize its response."""
    query = query.strip()
    if not query:
        raise ValueError("搜索内容不能为空")

    raw = await _call_search_mcp(
        api_key.strip(),
        query,
        limit=max(1, min(limit, 10)),
        freshness_days=freshness_days,
        domains=_clean_domains(domains or []),
    )
    return _normalize_search_response(raw, limit=max(1, min(limit, 10)))


async def check_bailian_web(api_key: str) -> str:
    """Verify the Bailian WebSearch MCP service without running a query."""
    headers = {"Authorization": f"Bearer {api_key.strip()}"}
    async with streamablehttp_client(WEB_SEARCH_MCP_URL, headers=headers) as streams:
        read, write, _ = streams
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool = _select_search_tool((await session.list_tools()).tools)
            return tool.name


async def search_zhipu_web(
    api_key: str,
    query: str,
    *,
    limit: int = 8,
    freshness_days: int | None = None,
    domains: list[str] | None = None,
    base_url: str = ZHIPU_WEB_SEARCH_URL,
) -> dict[str, Any]:
    payload = {
        "search_query": query.strip(),
        "search_engine": "search_std",
        "search_intent": False,
        "count": max(1, min(limit, 50)),
        "content_size": "medium",
    }
    if freshness_days:
        payload["search_recency_filter"] = (
            "day"
            if freshness_days <= 1
            else "week"
            if freshness_days <= 7
            else "month"
            if freshness_days <= 30
            else "year"
        )
    if domains:
        payload["search_domain_filter"] = domains[0]
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            base_url, headers={"Authorization": f"Bearer {api_key}"}, json=payload
        )
        _raise_search_error(response, "智谱 AI")
    body = response.json()
    raw = {"structured": {"results": body.get("search_result", [])}, "text": "", "is_error": False}
    return _normalize_search_response(raw, limit=limit)


async def _call_search_mcp(
    api_key: str,
    query: str,
    *,
    limit: int,
    freshness_days: int | None,
    domains: list[str],
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    async with streamablehttp_client(WEB_SEARCH_MCP_URL, headers=headers) as streams:
        read, write, _ = streams
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            tool = _select_search_tool(listed.tools)
            arguments = _build_arguments(
                tool.inputSchema or {},
                query,
                limit=limit,
                freshness_days=freshness_days,
                domains=domains,
            )
            result = await session.call_tool(tool.name, arguments)
            text_blocks = [
                block.text
                for block in result.content
                if isinstance(getattr(block, "text", None), str)
            ]
            return {
                "is_error": bool(result.isError),
                "text": "\n".join(text_blocks).strip(),
                "structured": getattr(result, "structuredContent", None),
            }


def _raise_search_error(response: httpx.Response, provider: str) -> None:
    """Raise an exception retaining the provider's actionable error message."""
    if response.is_success:
        return
    detail = ""
    try:
        payload = response.json()
        detail = json.dumps(payload, ensure_ascii=False)
    except (ValueError, json.JSONDecodeError):
        detail = response.text
    detail = detail.strip()[:2000]
    raise RuntimeError(f"{provider} HTTP {response.status_code}: {detail}")


def _select_search_tool(tools: list[Any]) -> Any:
    candidates = [tool for tool in tools if "search" in tool.name.lower()]
    if not candidates:
        raise RuntimeError("联网搜索服务未返回可用的搜索能力")
    return min(candidates, key=lambda tool: ("web" not in tool.name.lower(), len(tool.name)))


def _build_arguments(
    schema: dict[str, Any],
    query: str,
    *,
    limit: int,
    freshness_days: int | None,
    domains: list[str],
) -> dict[str, Any]:
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    arguments: dict[str, Any] = {}

    query_field = _first_field(properties, _QUERY_FIELDS)
    if query_field is None:
        query_field = next(
            (name for name in required if (properties.get(name) or {}).get("type") == "string"),
            None,
        )
    if query_field is None:
        raise RuntimeError("联网搜索服务的查询参数格式暂不受支持")

    if domains and not _first_field(properties, _DOMAIN_FIELDS):
        query = f"{query} " + " OR ".join(f"site:{domain}" for domain in domains)
    if freshness_days and not _first_field(properties, _FRESHNESS_FIELDS):
        query = f"{query} 最近{freshness_days}天"
    arguments[query_field] = query

    limit_field = _first_field(properties, _LIMIT_FIELDS)
    if limit_field:
        arguments[limit_field] = limit

    freshness_field = _first_field(properties, _FRESHNESS_FIELDS)
    if freshness_field and freshness_days:
        arguments[freshness_field] = _schema_value_for_days(
            properties.get(freshness_field) or {}, freshness_days
        )

    domain_field = _first_field(properties, _DOMAIN_FIELDS)
    if domain_field and domains:
        field_schema = properties.get(domain_field) or {}
        arguments[domain_field] = domains if field_schema.get("type") == "array" else domains[0]

    return arguments


def _first_field(properties: dict[str, Any], candidates: tuple[str, ...]) -> str | None:
    return next((name for name in candidates if name in properties), None)


def _schema_value_for_days(schema: dict[str, Any], days: int) -> int | str:
    enum = schema.get("enum") or []
    aliases = (
        (1, ("day", "1d", "24h")),
        (7, ("week", "7d")),
        (30, ("month", "30d")),
        (365, ("year", "365d")),
    )
    if enum:
        nearest = min(aliases, key=lambda item: abs(item[0] - days))[1]
        return next((value for value in nearest if value in enum), str(days))
    return days if schema.get("type") == "integer" else str(days)


def _clean_domains(domains: list[str]) -> list[str]:
    cleaned: list[str] = []
    for domain in domains:
        value = domain.strip().lower()
        if "://" in value:
            value = urlsplit(value).netloc
        value = value.split("/")[0].split(":")[0]
        if value and re.fullmatch(r"[a-z0-9.-]+", value) and value not in cleaned:
            cleaned.append(value)
    return cleaned[:5]


def _normalize_search_response(raw: dict[str, Any], *, limit: int) -> dict[str, Any]:
    if raw.get("is_error"):
        raise RuntimeError(str(raw.get("text") or "联网搜索服务返回错误"))

    payloads: list[Any] = []
    if raw.get("structured") is not None:
        payloads.append(raw["structured"])
    text = str(raw.get("text") or "").strip()
    if text:
        try:
            payloads.append(json.loads(text))
        except json.JSONDecodeError:
            payloads.append(text)

    results: list[dict[str, str]] = []
    references: list[str] = []
    for payload in payloads:
        _collect_results(payload, results, references)
    for url in _URL_RE.findall(text):
        cleaned = _clean_url(url)
        if cleaned and cleaned not in references:
            references.append(cleaned)

    deduped: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in results:
        url = item.get("url", "")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        if item.get("title") or item.get("snippet") or url:
            deduped.append(item)
        if len(deduped) >= limit:
            break

    return {
        "found": bool(deduped or text),
        "results": deduped,
        "content": text[:8000] if not deduped else "",
        "references": references[:limit],
    }


def _collect_results(value: Any, results: list[dict[str, str]], references: list[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_results(item, results, references)
        return
    if not isinstance(value, dict):
        return

    url = _clean_url(str(value.get("url") or value.get("link") or value.get("source_url") or ""))
    title = str(value.get("title") or value.get("name") or "").strip()
    snippet = str(
        value.get("snippet")
        or value.get("summary")
        or value.get("description")
        or value.get("content")
        or ""
    ).strip()
    published_at = str(
        value.get("published_at") or value.get("publish_time") or value.get("date") or ""
    ).strip()
    site_name = str(value.get("site_name") or value.get("source") or "").strip()
    if url or (title and snippet):
        result = {"title": title[:500], "url": url, "snippet": snippet[:2000]}
        if site_name and not site_name.startswith(("http://", "https://")):
            result["site_name"] = site_name[:200]
        if published_at:
            result["published_at"] = published_at[:100]
        results.append(result)
        if url and url not in references:
            references.append(url)

    for item in value.values():
        if isinstance(item, (dict, list)):
            _collect_results(item, results, references)


def _clean_url(url: str) -> str:
    url = url.strip().rstrip(".,;，。；")
    if not url.startswith(("http://", "https://")):
        return ""
    parsed = urlsplit(url)
    if not parsed.netloc:
        return ""
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            quote(parsed.path, safe="/%:@"),
            quote(parsed.query, safe="=&%:@,+/?"),
            quote(parsed.fragment, safe="%:@,+/?"),
        )
    )
