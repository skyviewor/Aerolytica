import pytest

from aero.core.config import AeroConfig
from aero.data.web_search import _build_arguments, _normalize_search_response
from aero.toolbox.tools import web_search


@pytest.fixture(autouse=True)
def _isolate_user_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))


def test_build_arguments_adapts_to_mcp_schema():
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "count": {"type": "integer"},
            "freshness": {"type": "integer"},
            "domains": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["query"],
    }

    arguments = _build_arguments(
        schema,
        "最近一次西太平洋台风",
        limit=6,
        freshness_days=7,
        domains=["cma.gov.cn"],
    )

    assert arguments == {
        "query": "最近一次西太平洋台风",
        "count": 6,
        "freshness": 7,
        "domains": ["cma.gov.cn"],
    }


def test_build_arguments_falls_back_to_search_operators():
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }

    arguments = _build_arguments(
        schema,
        "台风名称",
        limit=8,
        freshness_days=30,
        domains=["cma.gov.cn", "jma.go.jp"],
    )

    assert arguments["q"] == ("台风名称 site:cma.gov.cn OR site:jma.go.jp 最近30天")


def test_normalize_search_response_returns_structured_results_and_encoded_urls():
    raw = {
        "is_error": False,
        "text": "",
        "structured": {
            "results": [
                {
                    "title": "台风公报",
                    "url": "https://example.com/search?q=western pacific",
                    "summary": "最新台风信息",
                    "publish_time": "2026-07-18",
                }
            ]
        },
    }

    result = _normalize_search_response(raw, limit=8)

    assert result["found"] is True
    assert result["results"][0]["title"] == "台风公报"
    assert result["results"][0]["snippet"] == "最新台风信息"
    assert result["references"] == ["https://example.com/search?q=western%20pacific"]


@pytest.mark.asyncio
async def test_search_web_reuses_vision_key_without_returning_it(monkeypatch):
    config = AeroConfig.create_default()
    config.vision.api_key = "sk-vision-secret"
    monkeypatch.setattr(web_search, "find_config", lambda: config)
    captured = {}

    async def fake_search(api_key, query, **kwargs):
        captured.update(api_key=api_key, query=query, kwargs=kwargs)
        return {
            "found": True,
            "results": [
                {
                    "title": "中央气象台台风公报",
                    "url": "https://www.nmc.cn/publish/typhoon/warning.html",
                    "snippet": "台风信息",
                }
            ],
            "content": "",
            "references": ["https://www.nmc.cn/publish/typhoon/warning.html"],
        }

    monkeypatch.setattr(web_search, "search_bailian_web", fake_search)

    result = await web_search.search_web(
        "最近一次西太平洋台风",
        freshness_days=30,
        domains=["nmc.cn"],
    )

    assert captured["api_key"] == "sk-vision-secret"
    assert captured["query"] == "最近一次西太平洋台风"
    assert result["provider"] == "阿里云百炼联网搜索"
    assert result["api_key_configured"] is True
    assert result["credential_reused"] is True
    assert "sk-vision-secret" not in repr(result)


@pytest.mark.asyncio
async def test_successful_web_search_records_session_service_cost(monkeypatch):
    from aero.data.pricing import TokenTracker, use_token_tracker

    config = AeroConfig.create_default()
    config.web_search.provider = "zhipu"
    config.web_search.api_key = "sk-zhipu-secret"
    monkeypatch.setattr(web_search, "find_config", lambda: config)

    async def fake_search(*args, **kwargs):
        return {"found": True, "results": [], "content": "result", "references": []}

    monkeypatch.setattr(web_search, "search_zhipu_web", fake_search)
    tracker = TokenTracker()

    with use_token_tracker(tracker):
        await web_search.search_web("杭州天气")

    assert tracker.service_cost == 0.01
    assert tracker.to_dict()["service_usage"]["web_search:zhipu"]["calls"] == 1


@pytest.mark.asyncio
async def test_search_web_uses_its_own_credential_before_vision(monkeypatch):
    config = AeroConfig.create_default()
    config.web_search.api_key = "sk-search-secret"
    config.vision.api_key = "sk-vision-secret"
    monkeypatch.setattr(web_search, "find_config", lambda: config)
    captured = {}

    async def fake_search(api_key, query, **kwargs):
        captured["api_key"] = api_key
        return {"found": True, "results": [], "content": "", "references": []}

    monkeypatch.setattr(web_search, "search_bailian_web", fake_search)

    result = await web_search.search_web("杭州天气")

    assert captured["api_key"] == "sk-search-secret"
    assert result["credential_reused"] is False
    assert result["credential_source"] == "联网搜索 API Key"


@pytest.mark.asyncio
async def test_search_web_guides_setup_when_vision_key_is_missing(monkeypatch):
    config = AeroConfig.create_default()
    config.vision.api_key = ""
    monkeypatch.setattr(web_search, "find_config", lambda: config)

    result = await web_search.search_web("杭州天气")

    assert result["found"] is False
    assert result["api_key_configured"] is False
    assert result["credential_reused"] is False
    assert "百炼 API Key" in result["error"]
    assert "开通“联网搜索 MCP”" in result["message"]
    assert len(result["references"]) == 2


@pytest.mark.asyncio
async def test_search_web_detects_bailian_model_key_waiting_for_reuse_authorization(
    monkeypatch,
):
    """A primary Bailian key must not be mistaken for an absent credential."""
    config = AeroConfig.create_default()
    config.llm.provider = "bailian"
    config.llm.model = "qwen3.7-plus"
    config.llm.set_active_api_key("sk-bailian-primary")
    monkeypatch.setattr(web_search, "find_config", lambda: config)

    result = await web_search.search_web("最近一次西太平洋台风")

    assert result["found"] is False
    assert result["reusable_model_api_key_detected"] is True
    assert result["reuse_available"] is True
    assert result["authorization_required"] is True
    assert "无需重新创建或输入" in result["message"]
    assert "智谱 AI 搜索" in result["message"]
    assert result["provider_options"] == ["bailian", "zhipu"]
    assert "sk-bailian-primary" not in repr(result)


@pytest.mark.asyncio
async def test_check_web_search_status_reports_missing_credential(monkeypatch):
    config = AeroConfig.create_default()
    monkeypatch.setattr(web_search, "find_config", lambda: config)

    result = await web_search.check_web_search_status()

    assert result["available"] is False
    assert result["api_key_configured"] is False
    assert "暂不可用" in result["error"]


@pytest.mark.asyncio
async def test_check_web_search_status_verifies_live_service(monkeypatch):
    config = AeroConfig.create_default()
    config.web_search.api_key = "sk-search-secret"
    monkeypatch.setattr(web_search, "find_config", lambda: config)
    captured = {}

    async def fake_check(api_key):
        captured["api_key"] = api_key
        return "web_search"

    monkeypatch.setattr(web_search, "check_bailian_web", fake_check)

    result = await web_search.check_web_search_status()

    assert result["available"] is True
    assert result["service_tool"] == "web_search"
    assert captured["api_key"] == "sk-search-secret"
    assert "sk-search-secret" not in repr(result)


@pytest.mark.asyncio
async def test_check_web_search_status_does_not_overclaim_direct_provider(monkeypatch):
    config = AeroConfig.create_default()
    config.web_search.provider = "zhipu"
    config.web_search.api_key = "sk-zhipu-secret"
    monkeypatch.setattr(web_search, "find_config", lambda: config)

    result = await web_search.check_web_search_status()

    assert result["available"] is False
    assert result["status_unknown"] is True
    assert "余额" in result["message"]
    assert "智谱 AI 开放平台" in result["action_required"]
    assert "sk-zhipu-secret" not in repr(result)


@pytest.mark.asyncio
async def test_search_web_explains_unavailable_mcp_service(monkeypatch):
    config = AeroConfig.create_default()
    config.vision.api_key = "sk-test"
    monkeypatch.setattr(web_search, "find_config", lambda: config)

    async def fail(*args, **kwargs):
        raise ExceptionGroup("MCP request failed", [RuntimeError("Session terminated")])

    monkeypatch.setattr(web_search, "search_bailian_web", fail)

    result = await web_search.search_web("杭州天气")

    assert result["found"] is False
    assert result["api_key_configured"] is True
    assert result["credential_reused"] is True
    assert "无需重新提供或配置 Key" in result["error"]
    assert "启用 WebSearch" in result["action_required"]
    assert len(result["references"]) == 2


@pytest.mark.asyncio
async def test_search_web_explains_bailian_balance_and_mcp_requirements(monkeypatch):
    config = AeroConfig.create_default()
    config.web_search.api_key = "sk-bailian-secret"
    monkeypatch.setattr(web_search, "find_config", lambda: config)

    async def fail(*args, **kwargs):
        raise RuntimeError("HTTP 402 insufficient_quota")

    monkeypatch.setattr(web_search, "search_bailian_web", fail)

    result = await web_search.search_web("杭州天气")

    assert result["found"] is False
    assert "账户余额不足" in result["error"]
    assert "WebSearch MCP" in result["error"]
    assert "账户余额" in result["action_required"]
    assert "MCP 广场" in result["action_required"]


@pytest.mark.asyncio
async def test_search_web_explains_provider_balance_or_quota_failure(monkeypatch):
    config = AeroConfig.create_default()
    config.web_search.provider = "zhipu"
    config.web_search.api_key = "sk-zhipu-secret"
    monkeypatch.setattr(web_search, "find_config", lambda: config)

    async def fail(*args, **kwargs):
        raise RuntimeError("智谱 AI HTTP 402: insufficient_quota")

    monkeypatch.setattr(web_search, "search_zhipu_web", fail)

    result = await web_search.search_web("最近一次西太平洋台风")

    assert result["found"] is False
    assert "余额不足" in result["error"]
    assert "账户余额" in result["action_required"]
    assert "智谱 AI 开放平台" in result["action_required"]
    assert "sk-zhipu-secret" not in repr(result)


def test_search_web_is_registered_without_confirmation():
    from aero.toolbox.registry import get_registry

    spec = get_registry().get("search_web")

    assert spec is not None
    assert spec.requires_confirmation is False
    assert "最新信息" in spec.description


def test_web_search_status_is_registered_without_confirmation():
    from aero.toolbox.registry import get_registry

    spec = get_registry().get("check_web_search_status")

    assert spec is not None
    assert spec.requires_confirmation is False


def test_bailian_native_search_capability_is_removed():
    import aero.data.web_search as web_search

    assert not hasattr(web_search, "bailian_native_search_supported")
