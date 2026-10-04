"""Shared geometry for dialogs that cover the chat input."""

from __future__ import annotations

from typing import ClassVar, TypeVar

from textual.screen import ModalScreen

Result = TypeVar("Result")


class InputAlignedScreen(ModalScreen[Result]):
    DIALOG_SELECTOR: ClassVar[str]

    def setup_input_alignment(self) -> None:
        self.styles.layout = "grid"
        self.query_one(self.DIALOG_SELECTOR).styles.dock = "bottom"
        self.call_after_refresh(self.align_with_input)

    def align_with_input(self) -> None:
        if len(self.app.screen_stack) < 2:
            return
        inputs = self.app.screen_stack[-2].query("#input-box")
        if not inputs:
            return
        region = inputs.first().region
        if region.width:
            dialog = self.query_one(self.DIALOG_SELECTOR)
            dialog.styles.width = region.width
            dialog.styles.margin = (
                0,
                max(0, self.size.width - region.right),
                max(0, self.size.height - region.bottom),
                region.x,
            )

    def on_resize(self) -> None:
        self.call_after_refresh(self.align_with_input)
