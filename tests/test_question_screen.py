import pytest
from textual.widgets import Input, SelectionList, Static, TextArea

from aero.cli.main import ChatTuiApp, UserInputScreen
from aero.cli.question_screen import QuestionAction


class QuestionHarness(ChatTuiApp):
    def __init__(self, fields):
        super().__init__()
        self.dialog = UserInputScreen({"title": "研究设定", "fields": fields})
        self.result = "waiting"

    def on_mount(self):
        self.push_screen(self.dialog, self.finished)

    def finished(self, result):
        self.result = result

    def _set_input_focus_style(self, active):
        self.query_one("#input-box").set_class(active, "input-active")


async def test_one_question_at_a_time_choices_text_and_skip():
    app = QuestionHarness(
        [
            {
                "name": "focus",
                "label": "研究重点",
                "type": "single_choice",
                "options": ["高温", "热岛"],
            },
            {"name": "period", "label": "时段", "type": "text"},
            {"name": "notes", "label": "其他要求", "type": "multiline", "required": False},
        ]
    )
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        assert len(app.dialog.query("#question-editor")) == 1
        assert app.dialog.query_one("#question-editor").has_focus
        assert app.dialog.query_one("#question-dialog").region.height <= 24
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.dialog.index == 1
        editor = app.dialog.query_one(Input)
        editor.value = "2024年7月"
        await pilot.press("enter")
        await pilot.pause()
        assert app.dialog.index == 2
        await pilot.click("#question-skip")
        await pilot.pause()
        assert app.result == {"focus": "热岛", "period": "2024年7月", "notes": None}


async def test_required_validation_numeric_range_and_back_retains_answers():
    app = QuestionHarness(
        [
            {"name": "year", "label": "年份", "type": "number", "min": 2000, "max": 2026},
            {"name": "station", "label": "加入站点", "type": "boolean"},
        ]
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        assert app.dialog.index == 0
        assert "请填写" in str(app.dialog.query_one("#question-error", Static).render())
        app.dialog.query_one(Input).value = "1999"
        await pilot.press("enter")
        assert app.dialog.index == 0
        app.dialog.query_one(Input).value = "2024"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#question-back")
        await pilot.pause()
        assert app.dialog.query_one(Input).value == "2024.0"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.result == {"year": 2024.0, "station": False}


async def test_multiselect_and_multiline_preserve_types_and_cancel():
    app = QuestionHarness(
        [
            {
                "name": "datasets",
                "label": "数据",
                "type": "multiple_choice",
                "options": ["ERA5", "GFS"],
            },
            {"name": "notes", "label": "说明", "type": "multiline"},
        ]
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.dialog.query_one(SelectionList).select("ERA5")
        await pilot.click("#question-next")
        await pilot.pause()
        app.dialog.query_one(TextArea).load_text("第一行\n第二行")
        await pilot.pause(0.4)
        await pilot.click("#question-next")
        await pilot.pause()
        assert app.result == {"datasets": ["ERA5"], "notes": "第一行\n第二行"}
    canceled = QuestionHarness([{"name": "x", "label": "问题", "type": "text"}])
    async with canceled.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert canceled.result == {"x": None}


@pytest.mark.parametrize("size", [(100, 32), (160, 48)])
async def test_dialog_covers_real_chat_input_and_buttons_have_visible_text(size):
    app = QuestionHarness(
        [
            {
                "name": "focus",
                "label": "研究主线",
                "type": "single_choice",
                "options": ["长期气候趋势", "城市化", "高温过程", "预报"],
            },
        ]
    )
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        dialog = app.dialog.query_one("#question-dialog")
        underlying = app.screen_stack[0].query_one("#input-box")
        assert dialog.region.bottom == underlying.region.bottom
        assert dialog.region.x == underlying.region.x
        assert dialog.region.right == underlying.region.right
        assert dialog.region.overlaps(underlying.region)
        assert dialog.region.y > 0
        actions = app.dialog.query_one("#question-actions")
        for button in actions.query(QuestionAction):
            if not button.display:
                continue
            assert button.region.height == 1
            assert button.content_region.height >= 1
            assert actions.region.contains_region(button.region)
            assert dialog.region.contains_region(button.region)
            assert str(button.render())
        assert "确认" in str(app.dialog.query_one("#question-next").render())
        assert len(actions.query(QuestionAction)) == 3


async def test_skip_required_choice_means_no_selection_and_keeps_other_answers():
    app = QuestionHarness(
        [
            {
                "name": "focus",
                "label": "研究主线",
                "type": "single_choice",
                "options": ["高温", "热岛"],
            },
            {"name": "year", "label": "年份", "type": "number"},
        ]
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#question-skip")
        await pilot.pause()
        assert app.dialog.index == 1
        assert app.result == "waiting"
        app.dialog.query_one(Input).value = "2024"
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == {"focus": None, "year": 2024.0}


async def test_multiselect_enter_selects_and_tab_moves_to_confirm():
    app = QuestionHarness(
        [
            {
                "name": "data",
                "label": "数据",
                "type": "multiple_choice",
                "options": ["ERA5", "GFS"],
            },
        ]
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app.dialog.query_one(SelectionList)
        assert editor.has_focus
        await pilot.press("enter")
        await pilot.pause()
        assert editor.selected == ["ERA5"]
        assert app.result == "waiting"
        assert "Enter 选中" in str(app.dialog.query_one("#question-progress").render())
        await pilot.press("tab")
        assert app.dialog.query_one("#question-next").has_focus
        await pilot.press("tab")
        assert app.dialog.query_one("#question-skip").has_focus
        await pilot.press("shift+tab")
        assert app.dialog.query_one("#question-next").has_focus
        await pilot.press("shift+tab")
        assert editor.has_focus
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert app.result == {"data": ["ERA5"]}


async def test_tab_cycle_includes_back_on_later_question():
    app = QuestionHarness(
        [
            {"name": "one", "label": "第一题", "type": "text"},
            {"name": "two", "label": "第二题", "type": "multiline"},
        ]
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        editor = app.dialog.query_one(TextArea)
        assert editor.has_focus
        await pilot.press("tab")
        assert app.dialog.query_one("#question-back").has_focus
        await pilot.press("tab")
        assert app.dialog.query_one("#question-next").has_focus
        await pilot.press("tab")
        assert app.dialog.query_one("#question-skip").has_focus
        await pilot.press("tab")
        assert editor.has_focus
        assert editor.text == ""
        await pilot.press("shift+tab", "enter")
        await pilot.pause()
        assert app.result == {"one": None, "two": None}


async def test_editor_is_default_focus_each_question_and_highlight_tracks_focus():
    app = QuestionHarness(
        [
            {
                "name": "period",
                "label": "时段",
                "type": "single_choice",
                "options": ["1991–2020", "2001–2025"],
            },
            {
                "name": "data",
                "label": "数据",
                "type": "multiple_choice",
                "options": ["ERA5", "GFS"],
            },
        ]
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        confirm = app.dialog.query_one("#question-next")
        skip = app.dialog.query_one("#question-skip")
        assert app.dialog.query_one("#question-editor").has_focus
        assert not confirm.has_focus
        assert confirm.styles.background == skip.styles.background
        await pilot.press("tab")
        await pilot.pause()
        assert confirm.has_focus
        assert confirm.styles.background != skip.styles.background
        await pilot.press("enter")
        await pilot.pause()
        assert app.dialog.index == 1
        assert app.dialog.query_one("#question-editor").has_focus
        assert not confirm.has_focus
        app.dialog.query_one(SelectionList).select("ERA5")
        await pilot.press("tab", "tab", "enter")
        await pilot.pause()
        assert app.result == {"period": "1991–2020", "data": ["ERA5"]}


async def test_text_editor_accepts_typing_immediately_and_after_return():
    app = QuestionHarness(
        [
            {"name": "period", "label": "时段", "type": "text"},
            {"name": "notes", "label": "说明", "type": "multiline"},
        ]
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.dialog.query_one(Input).has_focus
        await pilot.press("2", "0", "2", "4", "enter")
        await pilot.pause()
        assert app.dialog.query_one(TextArea).has_focus
        await pilot.press("a", "enter", "b")
        assert app.dialog.query_one(TextArea).text == "a\nb"
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert app.dialog.index == 0
        assert app.dialog.query_one(Input).has_focus
        assert app.dialog.query_one(Input).value == "2024"
