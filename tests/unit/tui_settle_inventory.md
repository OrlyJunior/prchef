# Risky TUI settle patterns

This is a categorized review aid, not a claim that every `settle_tui` call has
been individually audited. Test names avoid generated line numbers going stale.

| Pattern | Representative tests | Assumption / synchronization |
| --- | --- | --- |
| Descendant workers | `test_execution_detail_actions_and_diagnostics_stay_bounded_in_narrow_terminal`, `test_execution_detail_actions_reflow_without_diagnostic_overflow`, `test_execution_detail_appends_only_new_diagnostics_and_preserves_ui_state` | Log width work belongs to child widgets. These waits use `wait_for_tui(..., descendants=True)` plus content/layout predicates. Other Log consumers need the same scope considered before geometry assertions. |
| Timers and reconciliation | `test_stop_control_is_offered_for_every_active_status`, `test_workflow_runner_reconciles_live_batch_row_without_remounting`, `test_closed_batch_detail_reconciles_visible_row_without_remounting` | A drained queue does not imply a future timer has fired. The stop fixture now agrees with persisted status and explicitly reconciles. Other timer tests still need explicit refreshes or final-state predicates. |
| Animations / deferred layout | `test_workflow_runner_detailed_logs_expand_the_main_scroll_region_and_collapse_cleanly`, `test_batch_rows_fit_content_and_last_row_remains_scrollable` | Message/worker completion alone does not prove final geometry after expansion. Assert content bounds/scroll ranges and use bounded state waits when animation or later layout is involved. |
| Focus scrolling | `test_select_focus_scrolls_the_control_into_view_on_small_terminals`, `test_execution_detail_cancel_is_visible_and_keyboard_reachable_with_long_diagnostics` | Focus can schedule a later scroll/layout. Wait for the focused control's visible region or the intended scroll state before geometry assertions. |
| Queued selection events | `test_workflow_runner_shows_runtime_sources_for_each_role_without_changing_payload`, role-local preset tests | The regression queues older/manual/current values and verifies that stale events neither apply an older preset nor restore defaults. Ordinary preset tests still rely on drained input messages. |
| Completion-triggered refresh | `test_workflow_runner_selects_available_tasks_and_starts_batch_in_list_order`, `test_workflow_runner_batch_finished_keeps_final_rows_and_summary_visible` | A completion label alone is insufficient. The order regression observes the still-running batch worker during refresh, checks it was not cancelled, and joins batch/refresh workers. |
