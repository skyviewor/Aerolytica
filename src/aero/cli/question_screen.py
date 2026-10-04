"""Compact, one-question-at-a-time non-secret input dialog."""

from __future__ import annotations

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Input, OptionList, SelectionList, Static, TextArea

from aero.cli.input_dialog import InputAlignedScreen
from aero.toolbox.user_input import validate_description, validate_values


class QuestionAction(Static):
    """Compact keyboard/click action matching the approval dialog."""

    can_focus = True

    class Pressed(Message):
        def __init__(self, control):
            super().__init__()
            self._control = control

        @property
        def control(self):
            return self._control

    def on_click(self) -> None:
        if not self.disabled:
            self.post_message(self.Pressed(self))

    def on_key(self, event: events.Key) -> None:
        if event.key in {"enter", "space"}:
            event.stop()
            event.prevent_default()
            if not self.disabled:
                self.post_message(self.Pressed(self))


class UserQuestionScreen(InputAlignedScreen[dict | None]):
    DIALOG_SELECTOR = "#question-dialog"

    CSS = """
    UserQuestionScreen { layout: grid; grid-size: 1; align: left bottom; background: transparent; }
    #question-dialog {
        dock: bottom; width: 88%; height: auto; max-height: 24;
        border: solid #5dade2; background: $surface;
        padding: 1 2; margin: 0 2 1 10;
    }
    #question-title { height: auto; text-style: bold; color: $text; }
    #question-instructions { height: auto; max-height: 3; color: $text-muted; overflow-y: auto; }
    #question-content { height: auto; max-height: 16; }
    #question-answer { height: auto; }
    #question-progress, #question-description { height: auto; color: $text-muted; }
    #question-label { height: auto; margin-top: 1; text-style: bold; }
    #question-editor { height: auto; min-height: 3; max-height: 7; }
    TextArea#question-editor { height: 4; }
    #question-error { height: auto; color: $error; }
    #question-actions { height: 1; min-height: 1; margin-top: 1; align: left middle; }
    #question-actions QuestionAction {
        width: 16; height: 1;
        margin-right: 1; padding: 0; border: none;
        content-align: center middle; color: $text; background: $boost;
    }
    #question-actions QuestionAction:focus { background: #1676b8; color: #ffffff; }
    #question-actions QuestionAction:disabled { color: $text-disabled; }
    """
    BINDINGS = [
        Binding("escape", "skip", "跳过", priority=True),
        Binding("tab", "question_focus(1)", "切换控件", priority=True),
        Binding("shift+tab", "question_focus(-1)", "反向切换", priority=True),
    ]

    def __init__(self, description: dict):
        super().__init__()
        self.description = validate_description(description)
        self.index = 0
        self.answers: dict = {}
        self.skipped: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="question-dialog"):
            yield Static(self.description["title"], id="question-title", markup=False)
            yield Static("", id="question-progress", markup=False)
            with VerticalScroll(id="question-content"):
                yield Static(
                    self.description.get("instructions", ""),
                    id="question-instructions",
                    markup=False,
                )
                yield Static("", id="question-label", markup=False)
                yield Static("", id="question-description", markup=False)
                yield Vertical(id="question-answer")
                yield Static("", id="question-error", markup=False)
            with Horizontal(id="question-actions"):
                yield QuestionAction("返回", id="question-back")
                yield QuestionAction("确认", id="question-next")
                yield QuestionAction("跳过", id="question-skip")

    async def on_mount(self) -> None:
        await self.show_question()
        self.setup_input_alignment()

    @property
    def field(self) -> dict:
        return self.description["fields"][self.index]

    def action_question_focus(self, direction: int) -> None:
        controls = [self.query_one("#question-editor")]
        controls.extend(
            button
            for button in self.query_one("#question-actions").query(QuestionAction)
            if not button.disabled and button.display
        )
        focused = self.focused
        index = controls.index(focused) if focused in controls else -1
        controls[(index + direction) % len(controls)].focus()

    async def show_question(self) -> None:
        field = self.field
        enter_hint = "Enter 确认"
        if field["type"] == "multiple_choice":
            enter_hint = "Enter 选中 · 切到确认按钮后 Enter 提交"
        if field["type"] == "multiline":
            enter_hint = "Enter 换行 · 切到确认按钮后 Enter 提交"
        self.query_one("#question-progress", Static).update(
            f"问题 {self.index + 1} / {len(self.description['fields'])}"
            f" · {enter_hint} · Tab / Shift+Tab 切换 · Esc 跳过"
        )
        optional = "（可跳过）" if not field.get("required", True) else ""
        self.query_one("#question-label", Static).update(field["label"] + optional)
        self.query_one("#question-description", Static).update(field.get("description", ""))
        self.query_one("#question-error", Static).update("")
        self.query_one("#question-back", QuestionAction).disabled = self.index == 0
        host = self.query_one("#question-answer", Vertical)
        await host.remove_children()
        kind = field["type"]
        value = self.answers.get(field["name"])
        if kind in {"single_choice", "boolean"}:
            options = field.get("options", ["是", "否"])
            editor = OptionList(*options, id="question-editor")
            if value is not None:
                editor.highlighted = (
                    (0 if value else 1) if kind == "boolean" else options.index(value)
                )
        elif kind == "multiple_choice":
            editor = SelectionList(
                *[(option, option, option in (value or [])) for option in field["options"]],
                id="question-editor",
            )
        elif kind == "multiline":
            editor = TextArea(value or "", id="question-editor", show_line_numbers=False)
        else:
            editor = Input(
                value="" if value is None else str(value),
                id="question-editor",
                placeholder="输入回答，Enter 确认",
                type="number" if kind == "number" else "text",
            )
        await host.mount(editor)
        editor.focus()

    def read_answer(self):
        editor = self.query_one("#question-editor")
        kind = self.field["type"]
        if kind == "multiline":
            return editor.text
        if kind == "multiple_choice":
            return list(editor.selected)
        if kind in {"single_choice", "boolean"}:
            index = editor.highlighted
            if index is None:
                return None
            return index == 0 if kind == "boolean" else self.field["options"][index]
        value = editor.value
        if kind == "number" and value:
            try:
                return float(value)
            except ValueError:
                raise ValueError("请输入有效数字。") from None
        return value

    async def advance(self, *, skip: bool = False) -> None:
        try:
            value = None if skip else self.read_answer()
            if not skip:
                validate_values(
                    {"title": self.description["title"], "fields": [self.field]},
                    {self.field["name"]: value},
                )
            if skip:
                self.skipped.add(self.field["name"])
            else:
                self.skipped.discard(self.field["name"])
            self.answers[self.field["name"]] = value
            if self.index == len(self.description["fields"]) - 1:
                checked = {
                    **self.description,
                    "fields": [
                        {**field, "required": False} if field["name"] in self.skipped else field
                        for field in self.description["fields"]
                    ],
                }
                self.dismiss(validate_values(checked, self.answers))
            else:
                self.index += 1
                await self.show_question()
        except ValueError as exc:
            self.query_one("#question-error", Static).update(str(exc))

    @on(OptionList.OptionSelected, "#question-editor")
    @on(Input.Submitted, "#question-editor")
    @on(QuestionAction.Pressed, "#question-next")
    async def next_question(self) -> None:
        await self.advance()

    @on(QuestionAction.Pressed, "#question-back")
    async def previous_question(self) -> None:
        if self.index:
            try:
                if self.field["name"] not in self.skipped:
                    self.answers[self.field["name"]] = self.read_answer()
            except ValueError:
                pass
            self.index -= 1
            await self.show_question()

    @on(QuestionAction.Pressed, "#question-skip")
    async def action_skip(self) -> None:
        await self.advance(skip=True)
