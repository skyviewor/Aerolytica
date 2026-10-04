"""All input overlays share the real input box's geometry."""

import pytest

from aero.cli.main import (
    ChatTuiApp,
    ConfirmScreen,
    ExecutionApprovalScreen,
    UserInputScreen,
)


class DialogHarness(ChatTuiApp):
    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        self.result = None

    def on_mount(self):
        self.push_screen(self.dialog, self.finished)

    def finished(self, result):
        self.result = result

    def _set_input_focus_style(self, active):
        self.query_one("#input-box").set_class(active, "input-active")


@pytest.mark.parametrize("kind", ["approval", "plan", "question"])
@pytest.mark.parametrize("size", [(100, 32), (220, 48)])
async def test_overlays_align_to_input_on_mount_and_resize(kind, size):
    if kind == "approval":
        dialog = ConfirmScreen("执行命令：mkdir -p test")
    elif kind == "plan":
        dialog = ExecutionApprovalScreen()
    else:
        dialog = UserInputScreen(
            {
                "title": "选择",
                "fields": [
                    {
                        "name": "data",
                        "label": "数据",
                        "type": "single_choice",
                        "options": ["ERA5", "GFS"],
                    },
                ],
            }
        )
    app = DialogHarness(dialog)
    async with app.run_test(size=size) as pilot:
        for new_size in [None, (180, 42), (110, 36)]:
            if new_size:
                await pilot.resize_terminal(*new_size)
            await pilot.pause()
            overlay = dialog.query_one(dialog.DIALOG_SELECTOR)
            input_box = app.screen_stack[0].query_one("#input-box")
            assert overlay.region.x == input_box.region.x
            assert overlay.region.right == input_box.region.right
            assert overlay.region.width == input_box.region.width
            assert overlay.region.bottom == input_box.region.bottom
            assert overlay.region.overlaps(input_box.region)
        await pilot.press("enter")
        await pilot.pause()
        expected = {"approval": "allow", "plan": "approve", "question": {"data": "ERA5"}}
        assert app.result == expected[kind]
