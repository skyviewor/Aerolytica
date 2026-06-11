import httpx
import pytest

from meteora.core.config import MeteoraConfig


def test_check_vision_model_config_not_configured_guides_bailian(tmp_path, monkeypatch):
    from meteora.toolbox import builtin_tools

    monkeypatch.setenv("METEORA_SECRETS_PATH", str(tmp_path / "empty-secrets.yaml"))
    config_path = tmp_path / "meteora.yaml"
    MeteoraConfig.create_default().save(config_path)
    monkeypatch.chdir(tmp_path)

    result = builtin_tools.check_vision_model_config()

    assert result["status"] == "not_configured"
    assert result["configured"] is False
    assert result["provider"] == "bailian"
    assert "阿里云百炼 Qwen" in result["message"]
    assert "DeepSeek" not in result["message"]
    assert "bailian.console.aliyun.com" in result["api_key_url"]


def test_check_vision_model_config_configured_uses_vision_config(tmp_path, monkeypatch):
    from meteora.core.config import save_vision_api_key
    from meteora.toolbox import builtin_tools

    monkeypatch.setenv("METEORA_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config_path = tmp_path / "meteora.yaml"
    config = MeteoraConfig.create_default()
    config.vision.provider = "bailian"
    config.vision.model = "qwen-vl-max"
    config.save(config_path)
    save_vision_api_key("sk-vision-test")
    monkeypatch.chdir(tmp_path)

    result = builtin_tools.check_vision_model_config()

    assert result["status"] == "configured"
    assert result["configured"] is True
    assert result["provider"] == "bailian"
    assert result["model"] == "qwen-vl-max"
    assert result["api_key_configured"] is True
    assert "主聊天模型" in result["message"]


@pytest.mark.asyncio
async def test_analyze_image_not_configured_guides_bailian_api_key_setup(tmp_path, monkeypatch):
    from meteora.toolbox import builtin_tools

    monkeypatch.setenv("METEORA_SECRETS_PATH", str(tmp_path / "empty-secrets.yaml"))
    config_path = tmp_path / "meteora.yaml"
    MeteoraConfig.create_default().save(config_path)
    monkeypatch.chdir(tmp_path)

    result = await builtin_tools.analyze_image(
        image_paths=["data/example.png"],
        prompt="分析这张图",
    )

    api_key_url = "https://bailian.console.aliyun.com/cn-beijing?tab=model#/api-key"
    assert result["status"] == "not_configured"
    assert f"[阿里云百炼 API Key]({api_key_url})" in result["message"]
    assert f"请复制这个地址打开：{api_key_url}" in result["message"]
    assert "登录后从左侧菜单栏进入 API Key 模块" in result["message"]
    assert "点击「创建 API Key」" in result["message"]
    assert "从左侧菜单栏进入 API Key 模块" in result["setup_steps"]


@pytest.mark.asyncio
async def test_analyze_image_reports_blank_exception_type(tmp_path, monkeypatch):
    from meteora.core.config import save_vision_api_key
    from meteora.toolbox import builtin_tools
    from meteora.toolbox.tools import vision

    image = tmp_path / "figures" / "plot.png"
    image.parent.mkdir()
    image.write_bytes(b"fake image bytes")

    monkeypatch.setenv("METEORA_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config_path = tmp_path / "meteora.yaml"
    config = MeteoraConfig.create_default()
    config.vision.model = "qwen-vl-max"
    config.save(config_path)
    save_vision_api_key("sk-vision-test")
    monkeypatch.chdir(tmp_path)

    class BlankVisionClient:
        last_usage = None

        async def analyze(self, image_paths, prompt, detail):
            raise RuntimeError()

    monkeypatch.setattr(vision, "_ensure_vision_client", lambda: (BlankVisionClient(), config))

    result = await builtin_tools.analyze_image([str(image)], "分析这张图")

    assert result["status"] == "error"
    assert result["reason"] == "RuntimeError"
    assert result["error_type"] == "RuntimeError"
    assert "图片分析失败：RuntimeError" in result["message"]
    assert result["model"] == "qwen-vl-max"
    assert result["image_paths"] == [str(image)]


@pytest.mark.asyncio
async def test_analyze_image_reports_vision_http_details(tmp_path, monkeypatch):
    from meteora.agent.vision_client import VisionAnalysisError
    from meteora.core.config import save_vision_api_key
    from meteora.toolbox import builtin_tools
    from meteora.toolbox.tools import vision

    image = tmp_path / "figures" / "plot.png"
    image.parent.mkdir()
    image.write_bytes(b"fake image bytes")

    monkeypatch.setenv("METEORA_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config_path = tmp_path / "meteora.yaml"
    config = MeteoraConfig.create_default()
    config.vision.model = "qwen-vl-max"
    config.save(config_path)
    save_vision_api_key("sk-vision-test")
    monkeypatch.chdir(tmp_path)

    class ErrorVisionClient:
        last_usage = None

        async def analyze(self, image_paths, prompt, detail):
            raise VisionAnalysisError(
                "视觉模型请求失败（HTTP 400）。",
                error_type="http_error",
                status_code=400,
                response_excerpt='{"message":"invalid image"}',
            )

    monkeypatch.setattr(vision, "_ensure_vision_client", lambda: (ErrorVisionClient(), config))

    result = await builtin_tools.analyze_image([str(image)], "分析这张图")

    assert result["status"] == "error"
    assert result["error_type"] == "http_error"
    assert result["status_code"] == 400
    assert result["response_excerpt"] == '{"message":"invalid image"}'
    assert "服务返回摘要" in result["message"]


@pytest.mark.asyncio
async def test_vision_client_http_error_includes_response_excerpt(tmp_path):
    from meteora.agent.vision_client import VisionAnalysisError, VisionClient
    from meteora.core.config import VisionConfig

    image = tmp_path / "plot.png"
    image.write_bytes(b"fake image bytes")

    def handler(request):
        return httpx.Response(400, json={"message": "invalid image payload"})

    config = VisionConfig(
        provider="bailian",
        model="qwen-vl-max",
        api_key="sk-test",
        base_url="https://example.test",
    )
    client = VisionClient(config)
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(VisionAnalysisError) as exc_info:
        await client.analyze([str(image)], "分析这张图")

    exc = exc_info.value
    assert exc.error_type == "http_error"
    assert exc.status_code == 400
    assert "invalid image payload" in exc.response_excerpt
    await client.close()


@pytest.mark.asyncio
async def test_list_figures_only_reads_figures_directory(tmp_path, monkeypatch):
    from meteora.toolbox import builtin_tools

    MeteoraConfig.create_default().save(tmp_path / "meteora.yaml")
    figures = tmp_path / "figures"
    data = tmp_path / "data"
    figures.mkdir()
    data.mkdir()
    (figures / "plot.png").write_bytes(b"not really png")
    (figures / "notes.txt").write_text("ignore me")
    (data / "old_plot.png").write_bytes(b"ignore data image")
    monkeypatch.chdir(tmp_path)

    result = await builtin_tools.list_figures()

    assert result["status"] == "success"
    assert result["relative_directory"] == "figures"
    assert result["file_count"] == 1
    assert result["files"][0]["name"] == "plot.png"
    assert result["files"][0]["relative_path"] == "figures/plot.png"


@pytest.mark.asyncio
async def test_list_figures_creates_directory(tmp_path, monkeypatch):
    from meteora.toolbox import builtin_tools

    MeteoraConfig.create_default().save(tmp_path / "meteora.yaml")
    monkeypatch.chdir(tmp_path)

    result = await builtin_tools.list_figures()

    assert result["status"] == "success"
    assert result["file_count"] == 0
    assert (tmp_path / "figures").is_dir()
