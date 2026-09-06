"""Select behavior that remains usable inside vertically scrollable screens."""

from __future__ import annotations

from collections.abc import Hashable
from typing import TypeVar

from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Select
from textual.widgets._option_list import OptionList
from textual.widgets._select import SelectCurrent, SelectOverlay

SelectValue = TypeVar("SelectValue", bound=Hashable)


class _OrchestratorSelectCurrent(SelectCurrent):
    """Forward mouse activation directly to the adapted parent Select."""

    FOCUS_ON_CLICK = False

    def _on_click(self, event: events.Click) -> None:  # type: ignore[override]
        event.stop()
        event.prevent_default()
        parent = self.parent
        if isinstance(parent, OrchestratorSelect):
            if parent.expanded:
                parent.action_hide_overlay()
            else:
                parent.action_show_overlay()


class _OrchestratorSelectOverlay(SelectOverlay):
    """Close and select without delegating back to Textual's scroll-visible handlers."""

    def action_dismiss(self) -> None:
        parent = self.parent
        if isinstance(parent, OrchestratorSelect):
            parent.action_hide_overlay()
        else:
            super().action_dismiss()

    def _on_blur(self, _event: events.Blur) -> None:
        if hasattr(self, "_search_query"):
            self._search_query = ""
        search_reset_timer = getattr(self, "_search_reset_timer", None)
        if search_reset_timer is not None:
            search_reset_timer.reset()
            search_reset_timer.stop()
        parent = self.parent
        if isinstance(parent, OrchestratorSelect):
            parent.expanded = False
            self.suppress_click()
        else:
            super()._on_blur(_event)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        parent = self.parent
        if isinstance(parent, OrchestratorSelect):
            parent._select_option(event.option_index)
        else:
            super().on_option_list_option_selected(event)


class OrchestratorSelect(Select[SelectValue]):
    """A ``Select`` that reserves visible space for its overlay before opening.

    Textual renders a Select overlay in an overlay layer, but a ``VerticalScroll``
    still clips that layer to its viewport. Opening near the viewport's lower edge
    therefore needs a small, synchronous-in-effect scroll adjustment. This widget
    makes that adjustment only after the click has completed, so focusing an
    unfocused Select cannot turn the first click into a focus-only click.
    """

    MAX_OVERLAY_HEIGHT = 12
    # Textual normally focuses the clicked Select on mouse-down. Delaying that
    # focus until after click dispatch avoids it replacing the overlay focus.
    FOCUS_ON_CLICK = False

    def compose(self) -> ComposeResult:
        """Compose Select internals that delegate mouse events to this adaptation."""

        yield _OrchestratorSelectCurrent(self.prompt)
        overlay = _OrchestratorSelectOverlay()
        if hasattr(Select, "compact"):
            overlay = overlay.data_bind(compact=Select.compact)
        yield overlay

    async def _on_click(self, event: events.Click) -> None:
        """Handle clicks that land on the Select border rather than its current child."""

        if event.widget is self:
            event.stop()
            event.prevent_default()
            if self.expanded:
                self.action_hide_overlay()
            else:
                self.action_show_overlay()
        else:
            await super()._on_click(event)

    def action_show_overlay(self) -> None:
        """Open the overlay after making its usable portion visible."""

        self._prepare_overlay_viewport()
        super().action_show_overlay()
        self.call_after_refresh(self._reveal_overlay)

    def action_hide_overlay(self) -> None:
        """Close the overlay and return focus without scrolling its container."""

        self.expanded = False
        self.focus(scroll_visible=False)

    def _select_option(self, option_index: int) -> None:
        """Apply a choice and restore Select focus without a layout jump."""

        value = self._options[option_index][1]
        if value != self.value:
            self.value = value
        self.action_hide_overlay()

    def _prepare_overlay_viewport(self) -> None:
        """Reserve the vertical space available to the overlay in its scroll parent."""

        scroll_parent = self._scroll_parent()
        if scroll_parent is None:
            return

        viewport = scroll_parent.content_region
        overlay_height = min(self.MAX_OVERLAY_HEIGHT, max(1, len(self._options)))
        available_below = max(0, viewport.bottom - self.region.bottom)

        # On a short terminal, keep the Select at the viewport top and constrain
        # the overlay rather than scrolling past it or creating a horizontal jump.
        if self.region.height + overlay_height > viewport.height:
            scroll_parent.scroll_to_widget(self, animate=False, top=True, immediate=True)
            overlay_height = max(1, viewport.height - self.region.height)
        elif available_below < overlay_height:
            scroll_parent.scroll_relative(
                y=overlay_height - available_below, animate=False, immediate=True
            )

        self.query_one(SelectOverlay).styles.max_height = overlay_height

    def _reveal_overlay(self) -> None:
        """Correct any clipping that remains after Textual lays out the overlay."""

        scroll_parent = self._scroll_parent()
        if scroll_parent is None or not self.expanded:
            return
        overlay = self.query_one(SelectOverlay)
        overflow = overlay.region.bottom - scroll_parent.content_region.bottom
        if overflow > 0:
            scroll_parent.scroll_relative(y=overflow, animate=False, immediate=True)

    def _scroll_parent(self) -> VerticalScroll | None:
        """Return the closest vertical scroll container, if this Select has one."""

        return next(
            (ancestor for ancestor in self.ancestors if isinstance(ancestor, VerticalScroll)),
            None,
        )


def is_orchestrator_select_widget(widget: Widget) -> bool:
    """Whether a focus event belongs to an ``OrchestratorSelect`` or its internals."""

    return isinstance(widget, OrchestratorSelect) or any(
        isinstance(ancestor, OrchestratorSelect) for ancestor in widget.ancestors
    )
