import httpx
import pytest
from PIL import Image

from aero.core.config import AeroConfig


@pytest.mark.asyncio
async def test_vision_client_does_not_accept_brotli_response(tmp_path):
    from aero.agent.vision_client import VisionClient
    from aero.core.config import VisionConfig

    image = tmp_path / "plot.png"
    image.write_bytes(b"fake image bytes")
    received_headers = {}

    def handler(request):
        received_headers.update(request.headers)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "图片分析结果"}}]},
        )

    client = VisionClient(VisionConfig(api_key="sk-test", base_url="https://example.test"))
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.analyze([str(image)], "分析这张图") == "图片分析结果"
    finally:
        await client.close()

    assert received_headers["accept-encoding"] == "identity"


def test_prepare_image_for_vision_creates_smaller_copy_without_changing_source(
    tmp_path, monkeypatch
):
    from aero.toolbox.tools.images import prepare_image_for_vision

    monkeypatch.chdir(tmp_path)
    source = tmp_path / "figures" / "large.png"
    source.parent.mkdir()
    Image.effect_noise((3200, 2200), 100).convert("RGB").save(source)
    original_bytes = source.read_bytes()

    result = prepare_image_for_vision("figures/large.png", max_dimension=1200, target_size_kb=300)

    assert result["status"] == "success"
    output = tmp_path / result["output_path"]
    assert output.exists()
    assert result["output_dimensions"] == [1200, 825]
    assert result["output_size_bytes"] < result["original_size_bytes"]
    assert result["output_size_bytes"] <= 300 * 1024
    assert source.read_bytes() == original_bytes


def test_check_vision_model_config_not_configured_describes_safe_setup(tmp_path, monkeypatch):
    from aero.toolbox import builtin_tools

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "empty-secrets.yaml"))
    config_path = tmp_path / "aero.yaml"
    AeroConfig.create_default().save(config_path)
    monkeypatch.chdir(tmp_path)

    result = builtin_tools.check_vision_model_config()

    assert result["status"] == "not_configured"
    assert result["configured"] is False
    assert result["provider"] == "bailian"
    assert result["mode"] == "unconfigured"
    assert "安全的配置界面" in result["message"]


def test_check_vision_model_config_configured_uses_vision_config(tmp_path, monkeypatch):
    from aero.core.config import save_vision_api_key
    from aero.toolbox import builtin_tools

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config_path = tmp_path / "aero.yaml"
    config = AeroConfig.create_default()
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
    assert result["mode"] == "separate"
    assert "视觉模型已配置" in result["message"]


@pytest.mark.asyncio
async def test_analyze_image_not_configured_requests_safe_visual_setup(tmp_path, monkeypatch):
    from aero.toolbox import builtin_tools

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "empty-secrets.yaml"))
    config_path = tmp_path / "aero.yaml"
    AeroConfig.create_default().save(config_path)
    monkeypatch.chdir(tmp_path)

    result = await builtin_tools.analyze_image(
        image_paths=["data/example.png"],
        prompt="分析这张图",
    )

    assert result["status"] == "not_configured"
    assert "安全配置界面" in result["message"]
    assert result["setup_required"] == "vision"


@pytest.mark.asyncio
async def test_analyze_image_reports_blank_exception_type(tmp_path, monkeypatch):
    from aero.core.config import save_vision_api_key
    from aero.toolbox import builtin_tools
    from aero.toolbox.tools import vision

    image = tmp_path / "figures" / "plot.png"
    image.parent.mkdir()
    image.write_bytes(b"fake image bytes")

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config_path = tmp_path / "aero.yaml"
    config = AeroConfig.create_default()
    config.vision.model = "qwen-vl-max"
    config.save(config_path)
    save_vision_api_key("sk-vision-test")
    monkeypatch.chdir(tmp_path)

    class BlankVisionClient:
        last_usage = None

        async def analyze(self, image_paths, prompt, detail):
            raise RuntimeError()

        async def close(self):
            pass

    monkeypatch.setattr(
        vision,
        "_ensure_vision_client",
        lambda: (BlankVisionClient(), config, config.vision),
    )

    result = await builtin_tools.analyze_image([str(image)], "分析这张图")

    assert result["status"] == "error"
    assert result["reason"] == "RuntimeError"
    assert result["error_type"] == "RuntimeError"
    assert "图片分析失败：RuntimeError" in result["message"]
    assert result["model"] == "qwen-vl-max"
    assert result["image_paths"] == [str(image)]


@pytest.mark.asyncio
async def test_analyze_image_reports_vision_http_details(tmp_path, monkeypatch):
    from aero.agent.vision_client import VisionAnalysisError
    from aero.core.config import save_vision_api_key
    from aero.toolbox import builtin_tools
    from aero.toolbox.tools import vision

    image = tmp_path / "figures" / "plot.png"
    image.parent.mkdir()
    image.write_bytes(b"fake image bytes")

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config_path = tmp_path / "aero.yaml"
    config = AeroConfig.create_default()
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

        async def close(self):
            pass

    monkeypatch.setattr(
        vision,
        "_ensure_vision_client",
        lambda: (ErrorVisionClient(), config, config.vision),
    )

    result = await builtin_tools.analyze_image([str(image)], "分析这张图")

    assert result["status"] == "error"
    assert result["error_type"] == "http_error"
    assert result["status_code"] == 400
    assert result["response_excerpt"] == '{"message":"invalid image"}'
    assert "服务返回摘要" in result["message"]


@pytest.mark.asyncio
async def test_vision_client_http_error_includes_response_excerpt(tmp_path):
    from aero.agent.vision_client import VisionAnalysisError, VisionClient
    from aero.core.config import VisionConfig

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
    from aero.toolbox import builtin_tools

    AeroConfig.create_default().save(tmp_path / "aero.yaml")
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
    from aero.toolbox import builtin_tools

    AeroConfig.create_default().save(tmp_path / "aero.yaml")
    monkeypatch.chdir(tmp_path)

    result = await builtin_tools.list_figures()

    assert result["status"] == "success"
    assert result["file_count"] == 0
    assert (tmp_path / "figures").is_dir()
