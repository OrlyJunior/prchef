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

    def on_mount(self) -> None:
        """Notify a repository Select when its current-value control is ready."""

        parent = self.parent
        if isinstance(parent, RepositorySelect):
            parent._on_repository_child_mount()

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

    def on_mount(self, event: events.Mount) -> None:  # type: ignore[override]
        """Notify a repository Select once its overlay is queryable."""

        parent = self.parent
        if isinstance(parent, RepositorySelect):
            parent._on_repository_overlay_mount()
            event.prevent_default()

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
    OVERLAY_FRAME_HEIGHT = 2
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
        # ``SelectOverlay`` has a top and bottom border in addition to its
        # option rows.  Its height limit applies to the entire widget, so
        # reserve those two cells as well.  Without them a short menu such as
        # Planner / Implementer / Reviewer is limited to three cells and only
        # one of its choices is visible.
        overlay_height = min(
            self.MAX_OVERLAY_HEIGHT,
            self.OVERLAY_FRAME_HEIGHT + max(1, len(self._options)),
        )
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
        self._pending_repository_options: list[tuple[Any, str]] | None = None
        self._pending_selected_value: Any = Select.BLANK
        super().__init__([], prompt=prompt, **kwargs)

    def set_options(
        self,
        options: Iterable[tuple[Any, str]],
        *,
        allow_blank: bool | None = None,
    ) -> None:
        """Set options without touching the overlay before it is mounted."""

        option_list = list(options)
        overlay = self._mounted_overlay()
        if overlay is None:
            effective_allow_blank = (
                self._allow_blank if allow_blank is None else allow_blank
            )
            selected_value: Any = Select.BLANK
            if not effective_allow_blank and option_list:
                selected_value = option_list[0][1]
            self._defer_options(
                option_list,
                selected_value,
                allow_blank=allow_blank,
            )
            return

        self._clear_pending_options()
        super().set_options(option_list, allow_blank=allow_blank)

    def _mounted_overlay(self) -> SelectOverlay | None:
        """Return the mounted overlay without querying an incomplete DOM."""

        if not self.is_mounted:
            return None
        current = next(
            (child for child in self.children if isinstance(child, SelectCurrent)),
            None,
        )
        for child in self.children:
            if (
                isinstance(child, SelectOverlay)
                and child.is_mounted
                and current is not None
                and current.is_mounted
                and bool(current.children)
                and all(grandchild.is_mounted for grandchild in current.children)
            ):
                return child
        return None

    def _defer_options(
        self,
        options: list[tuple[Any, str]],
        selected_value: Any,
        *,
        allow_blank: bool | None,
    ) -> None:
        """Keep Select state ready for its mount handler without rendering yet."""

        if allow_blank is not None:
            self._allow_blank = allow_blank
        self._setup_variables_for_options(options)
        self._value = selected_value
        self._reactive_value = selected_value
        self._pending_repository_options = options
        self._pending_selected_value = selected_value
        if self.is_mounted:
            self.call_after_refresh(self._preserve_deferred_selection)

    def _clear_pending_options(self) -> None:
        self._pending_repository_options = None
        self._pending_selected_value = Select.BLANK

    def _flush_pending_options(self) -> None:
        """Render options saved during the Select/overlay mount window."""

        if self._pending_repository_options is None or self._mounted_overlay() is None:
            return

        selected_value = self._pending_selected_value
        self._clear_pending_options()
        self._setup_options_renderables()
        if selected_value in self._legal_values:
            # The deferred value was seeded directly to avoid a pre-mount
            # Changed event. Reset only the reactive storage, then use
            # Textual's normal initializer to update both internal controls.
            self._reactive_value = Select.BLANK
            self._init_selected_option(selected_value)
            self.call_later(self._restore_selection, selected_value)

    def _preserve_deferred_selection(self) -> None:
        """Keep the requested value across Textual's remaining mount work."""

        if self._pending_repository_options is None:
            return
        selected_value = self._pending_selected_value
        if selected_value in self._legal_values:
            self._value = selected_value
            self._reactive_value = selected_value

    def _on_mount(self, event: events.Mount) -> None:
        """Let Textual render deferred options once the overlay is mounted."""

        # Select._on_mount immediately queries SelectOverlay, but Textual
        # dispatches this widget's mount message before mounting its children.
        # Prevent that inherited default action; the overlay notifies us from
        # its own mount handler instead.
        event.prevent_default()

    def _on_repository_overlay_mount(self) -> None:
        """Initialize Select internals after the overlay has mounted."""

        self.call_later(self._initialize_repository_options)

    def _on_repository_child_mount(self) -> None:
        """Retry initialization after either internal Select child mounts."""

        self.call_later(self._initialize_repository_options)

    def _initialize_repository_options(self) -> None:
        """Initialize options after the overlay's mount callback has completed."""

        if self._mounted_overlay() is None:
            return
        if self._pending_repository_options is not None:
            self._flush_pending_options()
        else:
            self._setup_options_renderables()
            self._init_selected_option(self._value)

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

        # A screen's ``on_mount`` can run after this Select is composed but
        # before its overlay has completed mounting.  Rendering options in
        # that window asks Textual to mutate an OptionList that is still
        # mounting.  Seed the parent Select instead; its own mount handler
        # will render these options and apply this selected value once the
        # overlay is ready.
        if self._mounted_overlay() is None:
            self._defer_options(options, value, allow_blank=False)
            return value

        self._clear_pending_options()
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
