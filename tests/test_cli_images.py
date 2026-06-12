import asyncio

from PIL import Image
from textual.app import App, ComposeResult

from aero.cli.main import InlineImageAttachment, AeroApp
from aero.cli.image_widget import terminal_half_block_preview, terminal_image_preview
from aero.core.config import AeroConfig


def test_inline_image_paths_and_attachment_indexes(tmp_path, monkeypatch):
    figures_dir = tmp_path / "figures"
    figures_dir.mkdir()
    image_path = figures_dir / "map.png"
    image_path.write_bytes(b"not a real image")
    monkeypatch.chdir(tmp_path)

    app = AeroApp(AeroConfig(), persist_config=False)

    paths = app._resolve_inline_image_paths("结果图：![map](figures/map.png)")

    assert paths == [image_path.resolve()]
    assert app._register_image_attachment(paths[0]) == 1
    assert app._register_image_attachment(paths[0]) == 1
    assert app._image_attachments == [image_path.resolve()]


def test_terminal_preview_is_cached_until_image_changes(tmp_path):
    image_path = tmp_path / "map.png"
    Image.new("RGB", (12, 8), (20, 40, 180)).save(image_path)

    first = terminal_half_block_preview(image_path)
    second = terminal_half_block_preview(image_path)

    assert first is second
    assert first.plain
    assert first.spans

    Image.new("RGB", (14, 8), (220, 80, 40)).save(image_path)

    changed = terminal_half_block_preview(image_path)
    assert changed is not first


def test_terminal_protocol_preview_preserves_normal_images_and_limits_large_ones(tmp_path):
    normal_path = tmp_path / "normal.png"
    large_path = tmp_path / "large.png"
    Image.new("RGB", (762, 485), (20, 40, 180)).save(normal_path)
    Image.new("RGB", (2420, 1563), (220, 80, 40)).save(large_path)

    normal = terminal_image_preview(normal_path)
    large = terminal_image_preview(large_path)

    assert normal.resolve() == normal_path.resolve()
    with Image.open(large) as large_image:
        assert large_image.width <= 1200
        assert large_image.height <= 800
    assert terminal_image_preview(normal_path) is normal


def test_inline_image_attachment_can_collapse_and_expand(tmp_path):
    image_path = tmp_path / "map.png"
    Image.new("RGB", (120, 80), (20, 40, 180)).save(image_path)

    class ImageApp(App):
        CSS = AeroApp.CSS

        def compose(self) -> ComposeResult:
            yield InlineImageAttachment(image_path, 1)

    async def check_layout():
        app = ImageApp()
        async with app.run_test(size=(120, 60)) as pilot:
            attachment = app.query_one(InlineImageAttachment)
            expanded_height = attachment.size.height

            attachment.set_collapsed(True)
            await pilot.pause()
            collapsed_height = attachment.size.height

            attachment.set_collapsed(False)
            await pilot.pause()

            assert expanded_height > 1
            assert collapsed_height == 1
            assert attachment.size.height == expanded_height

    asyncio.run(check_layout())
