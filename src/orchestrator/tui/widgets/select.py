"""Select behavior that remains usable inside vertically scrollable screens."""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from typing import Any, TypeVar

from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Select
from textual.widgets._option_list import OptionList
from textual.widgets._select import SelectCurrent, SelectOverlay

from orchestrator.config.project_config import ConfiguredRepository

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

    def set_options(
        self,
        options: Iterable[tuple[Any, SelectValue]],
        *,
        allow_blank: bool | None = None,
    ) -> None:
        """Replace options and, when needed, update blank-selection behavior.

        Textual requires an initially empty Select to permit a blank value.
        Screens which load options after mounting can opt out once they have at
        least one real value, so the overlay contains only configured choices.
        """

        # ``Select.set_options`` rebuilds both its legal-value set and the
        # overlay entries from ``_allow_blank``.  Update that flag before
        # delegating, so those structures are rebuilt together rather than
        # leaving the prompt's synthetic blank option in the overlay.
        if allow_blank is not None:
            self._allow_blank = allow_blank
        super().set_options(list(options))

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


class RepositorySelect(OrchestratorSelect[str]):
    """A Select whose choices are the configured repository keys.

    Repository controls are mounted with no choices because configuration is
    resolved by the controller at runtime. Keeping that transition here makes
    the value shown in the collapsed control and the mounted overlay derive
    from the same key/value collection on every screen.
    """

    def __init__(self, *, prompt: str = "Configured repository", **kwargs: Any) -> None:
        super().__init__([], prompt=prompt, **kwargs)

    def set_repositories(
        self,
        repositories: Iterable[ConfiguredRepository],
        *,
        selected_repository: str | None = None,
    ) -> str | None:
        """Load configured repository keys and retain a still-valid selection.

        A configured key is both the visible label and the Select value. Empty
        keys are ignored rather than becoming unlabelled rows in the overlay.
        """

        options = [
            (repository.key, repository.key)
            for repository in repositories
            if repository.key.strip()
        ]
        if not options:
            self.set_options([], allow_blank=True)
            return None

        values = {value for _, value in options}
        current = self.value if isinstance(self.value, str) else None
        value = (
            selected_repository
            if selected_repository in values
            else current
            if current in values
            else options[0][1]
        )

        # Textual's public set_options() always initializes a non-blank Select
        # to its first entry. Seed the desired legal value before initializing
        # it so a config reload never flashes a different repository or emits a
        # transient change event for it.
        self._allow_blank = False
        self._setup_variables_for_options(options)
        self._setup_options_renderables()
        self._init_selected_option(value)
        # A Select mounted without options first publishes its blank value.
        # Restore the configured selection after that initial mount work has
        # completed, otherwise a delayed blank update can win the race.
        self.call_later(self._restore_selection, value)
        return value

    def _restore_selection(self, value: str) -> None:
        """Reapply a valid configured repository after initial mount events."""

        if value in self._legal_values and self.value != value:
            self.value = value


def is_orchestrator_select_widget(widget: Widget) -> bool:
    """Whether a focus event belongs to an ``OrchestratorSelect`` or its internals."""

    return isinstance(widget, OrchestratorSelect) or any(
        isinstance(ancestor, OrchestratorSelect) for ancestor in widget.ancestors
    )
