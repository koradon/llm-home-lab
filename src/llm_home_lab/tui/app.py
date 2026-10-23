import argparse
import asyncio
import logging
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Sparkline,
    Static,
)

from llm_home_lab.diagnostics.metrics_parser import ParsedMetrics, parse_metrics_text
from llm_home_lab.tui.client import DiagnosticsClientError, OrchestratorDiagnosticsClient
from llm_home_lab.tui.load_history import update_load_history
from llm_home_lab.tui.rates import compute_token_rates

logger = logging.getLogger(__name__)

# Mirrors BACKEND_FACTORIES' keys (src/llm_home_lab/main.py) — extend when a new backend
# factory is registered there.
_KNOWN_BACKEND_TYPES = ["lmstudio", "llamaserver"]

# Each backend's own standard default port, offered as a base_url suggestion — never enforced.
_DEFAULT_BASE_URLS = {
    "lmstudio": "http://localhost:1234",
    "llamaserver": "http://localhost:8080",
}

_BANNER_MESSAGES = {
    "unauthorized": "not authorized — check API key",
    "connection": "cannot reach orchestrator, retrying",
    "timeout": "orchestrator slow to respond, retrying",
    "server_error": "orchestrator error, retrying",
}
_ERROR_KIND_PRIORITY = ("unauthorized", "connection", "timeout", "server_error")


def _format_avg_metric(avg: float | None) -> str:
    return "—" if avg is None else f"{avg:.0f}"


def _format_avg_min_max(avg: float | None, minimum: int | None, maximum: int | None) -> str:
    if avg is None or minimum is None or maximum is None:
        return "—"
    return f"{avg:.0f} ({minimum}–{maximum})"


def _pick_error(errors: list[DiagnosticsClientError]) -> DiagnosticsClientError:
    for kind in _ERROR_KIND_PRIORITY:
        for error in errors:
            if error.kind == kind:
                return error
    return errors[0]


def _sparkline_widget_id(host_id: str, prefix: str, used_ids: set[str]) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", host_id)
    if not sanitized or not (sanitized[0].isalpha() or sanitized[0] == "_"):
        sanitized = f"h_{sanitized}"
    candidate = f"{prefix}-{sanitized}"
    # Two distinct host_ids can sanitize to the same string (e.g. "node.1" and "node_1" both
    # become "node_1") — disambiguate rather than mount a duplicate widget id, which Textual
    # rejects and, per its Timer._tick, would take down the whole dashboard.
    suffix = 2
    while candidate in used_ids:
        candidate = f"{prefix}-{sanitized}-{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


_NODE_STATUS_STYLES = {"online": "bold green", "offline": "bold red", "unknown": "bold yellow"}


def _styled_node_status(status: object) -> Text:
    style = _NODE_STATUS_STYLES.get(str(status), "")
    return Text(str(status), style=style)


def _styled_failure_count(recent_failures: object) -> Text:
    count = recent_failures if isinstance(recent_failures, int) else 0
    if count == 0:
        return Text("0", style="dim")
    return Text(str(count), style="bold red")


def _styled_external_load(external_load: object) -> Text:
    load = external_load if isinstance(external_load, dict) else {}
    if not load.get("available"):
        return Text("unavailable", style="dim")

    status = str(load.get("status") or "idle")
    total_slots = load.get("total_slots")
    if isinstance(total_slots, int):
        # A slot-pool backend (llama-server): `queued` there is just the busy-slot count, not a
        # backlog, so show what is actually happening instead.
        busy_slots = load.get("busy_slots") or 0
        label = f"{status} ({busy_slots}/{total_slots} slots)"
        return Text(label, style="" if status == "idle" else "bold yellow")

    queued = load.get("queued") or 0
    label = status if not queued else f"{status} ({queued} queued)"
    return Text(label, style="" if status == "idle" else "bold yellow")


def _format_tasks_per_hour(throughput: object, key: str) -> str:
    value = throughput.get(key) if isinstance(throughput, dict) else None
    if not isinstance(value, int | float):
        return "n/a"
    return f"{value:.1f}"


# Session-only sort columns, cycled with "s"; None in the cycle is the persisted manual order.
_SORT_COLUMNS = ("node", "status", "reqs/min", "avg latency", "tokens")


def _sort_missing_last(
    items: list[tuple[str, object]], descending: bool
) -> list[tuple[str, object]]:
    present = [item for item in items if item[1] is not None]
    missing = [item for item in items if item[1] is None]
    present.sort(key=lambda item: cast("str | float", item[1]), reverse=descending)
    return present + missing


class DiagnosticsClient(Protocol):
    async def list_nodes(self) -> dict[str, object]: ...
    async def fetch_metrics_text(self) -> str: ...
    async def register_node(self, fields: dict[str, object]) -> None: ...
    async def update_node(self, host_id: str, fields: dict[str, object]) -> None: ...
    async def deregister_node(self, host_id: str) -> None: ...


class NodeFormScreen(ModalScreen[dict[str, object] | None]):
    """Collects a node's capabilities, either registering a new node or editing an existing one."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    NodeFormScreen {
        align: center middle;
    }
    #edit-dialog {
        width: 70;
        height: auto;
        max-height: 90%;
        border: round $accent;
        padding: 1 2;
        background: $panel;
    }
    #form-body {
        height: auto;
        max-height: 14;
    }
    #edit-error {
        height: auto;
        color: $error;
    }
    #edit-dialog Label {
        height: 1;
    }
    #edit-dialog Input {
        border: none;
        height: 1;
    }
    .mapping-row {
        height: 3;
    }
    .mapping-row Input {
        width: 1fr;
    }
    .mapping-remove {
        min-width: 3;
        width: 3;
    }
    """

    def __init__(
        self, mode: Literal["register", "edit"], host_id: str, node: dict[str, object]
    ) -> None:
        super().__init__()
        self._mode = mode
        self._host_id = host_id
        self._node = node
        self._size_row_seq = 0
        self._alias_row_seq = 0
        # Tracks the last value this screen auto-filled for base_url, so a backend_type change
        # only overwrites it if the operator hasn't typed a real value of their own yet.
        self._suggested_base_url: str | None = None

    def compose(self) -> ComposeResult:
        node = self._node
        allowed_models = cast("list[str] | None", node.get("allowed_models")) or []
        memory_budget_gb = node.get("memory_budget_gb")
        backend_type = cast(str, node.get("backend_type") or _KNOWN_BACKEND_TYPES[0])
        backend_type_options = list(_KNOWN_BACKEND_TYPES)
        if backend_type not in backend_type_options:
            backend_type_options.append(backend_type)

        base_url = str(node.get("base_url", ""))
        if not base_url:
            base_url = _DEFAULT_BASE_URLS.get(backend_type, "")
            self._suggested_base_url = base_url or None

        with Vertical(id="edit-dialog"):
            title = "Register new node" if self._mode == "register" else f"Edit {self._host_id}"
            yield Label(title)
            with VerticalScroll(id="form-body"):
                if self._mode == "register":
                    yield Label("host_id")
                    yield Input(value="", id="host_id")
                yield Label("backend_type")
                yield Select(
                    [(name, name) for name in backend_type_options],
                    value=backend_type,
                    allow_blank=False,
                    id="backend_type",
                )
                yield Label("context_window")
                yield Input(value=str(node.get("context_window", "")), id="context_window")
                yield Label("max_concurrent_requests")
                yield Input(
                    value=str(node.get("max_concurrent_requests", "")),
                    id="max_concurrent_requests",
                )
                yield Label("base_url")
                yield Input(value=base_url, id="base_url")
                yield Label("allowed_models (comma-separated, blank = unrestricted)")
                yield Input(value=", ".join(allowed_models), id="allowed_models")
                yield Label("memory_budget_gb (blank = unset)")
                yield Input(
                    value="" if memory_budget_gb is None else str(memory_budget_gb),
                    id="memory_budget_gb",
                )
                yield Label("model_sizes_gb")
                yield Vertical(id="model-sizes-rows")
                yield Button("+ Add size", id="add-size")
                yield Label("model_aliases (aliases: comma-separated)")
                yield Vertical(id="model-aliases-rows")
                yield Button("+ Add mapping", id="add-mapping")
            yield Static("", id="edit-error")
            with Horizontal():
                yield Button(
                    "Register" if self._mode == "register" else "Save", id="save", variant="primary"
                )
                yield Button("Cancel", id="cancel")

    async def on_mount(self) -> None:
        model_sizes_gb = cast("dict[str, float] | None", self._node.get("model_sizes_gb")) or {}
        for name, size in model_sizes_gb.items():
            await self._add_size_row(name, str(size))

        model_aliases = cast("dict[str, list[str]] | None", self._node.get("model_aliases")) or {}
        for name, aliases in model_aliases.items():
            await self._add_alias_row(name, ", ".join(aliases))

    async def _add_size_row(self, name: str = "", value: str = "") -> None:
        self._size_row_seq += 1
        await self.query_one("#model-sizes-rows", Vertical).mount(
            Horizontal(
                Input(value=name, placeholder="model name", classes="mapping-name"),
                Input(value=value, placeholder="size_gb", classes="mapping-value"),
                Button("✕", classes="mapping-remove"),
                id=f"size-row-{self._size_row_seq}",
                classes="mapping-row",
            )
        )

    async def _add_alias_row(self, name: str = "", value: str = "") -> None:
        self._alias_row_seq += 1
        await self.query_one("#model-aliases-rows", Vertical).mount(
            Horizontal(
                Input(value=name, placeholder="model name", classes="mapping-name"),
                Input(value=value, placeholder="alias1, alias2", classes="mapping-value"),
                Button("✕", classes="mapping-remove"),
                id=f"alias-row-{self._alias_row_seq}",
                classes="mapping-row",
            )
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "backend_type":
            return

        base_url_input = self.query_one("#base_url", Input)
        # Only overwrite an auto-suggested value, never one the operator actually typed.
        if base_url_input.value.strip() and base_url_input.value != self._suggested_base_url:
            return

        backend_type = cast(str, event.value)
        suggestion = _DEFAULT_BASE_URLS.get(backend_type, "")
        base_url_input.value = suggestion
        self._suggested_base_url = suggestion or None

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id == "add-size":
            await self._add_size_row()
            return
        if event.button.id == "add-mapping":
            await self._add_alias_row()
            return
        if event.button.has_class("mapping-remove"):
            row = event.button.parent
            assert isinstance(row, Widget)
            await row.remove()
            return

        fields, error = self._collect_fields()
        if error is not None:
            self.query_one("#edit-error", Static).update(Text(error, style="bold red"))
            return
        self.dismiss(fields)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _collect_fields(self) -> tuple[dict[str, object], str | None]:
        try:
            context_window = int(self.query_one("#context_window", Input).value)
            max_concurrent_requests = int(self.query_one("#max_concurrent_requests", Input).value)
        except ValueError:
            return {}, "context_window and max_concurrent_requests must be whole numbers"

        base_url = self.query_one("#base_url", Input).value.strip()
        if not base_url:
            return {}, "base_url must not be empty"

        fields: dict[str, object] = {
            "backend_type": self.query_one("#backend_type", Select).value,
            "context_window": context_window,
            "max_concurrent_requests": max_concurrent_requests,
            "base_url": base_url,
        }

        if self._mode == "register":
            host_id = self.query_one("#host_id", Input).value.strip()
            if not host_id:
                return {}, "host_id must not be empty"
            fields["host_id"] = host_id

        allowed_models_raw = self.query_one("#allowed_models", Input).value.strip()
        fields["allowed_models"] = (
            [m.strip() for m in allowed_models_raw.split(",") if m.strip()]
            if allowed_models_raw
            else None
        )

        memory_budget_raw = self.query_one("#memory_budget_gb", Input).value.strip()
        if memory_budget_raw:
            try:
                fields["memory_budget_gb"] = float(memory_budget_raw)
            except ValueError:
                return {}, "memory_budget_gb must be a number"
        else:
            fields["memory_budget_gb"] = None

        model_sizes_gb, sizes_error = self._collect_mapping_rows(
            "#model-sizes-rows",
            parse_value=float,
            value_error="model_sizes_gb values must be numbers",
        )
        if sizes_error is not None:
            return {}, sizes_error
        fields["model_sizes_gb"] = model_sizes_gb or None

        model_aliases, aliases_error = self._collect_mapping_rows(
            "#model-aliases-rows",
            parse_value=lambda raw: [a.strip() for a in raw.split(",") if a.strip()],
            value_error="model_aliases values must not be empty",
        )
        if aliases_error is not None:
            return {}, aliases_error
        fields["model_aliases"] = model_aliases or None

        return fields, None

    def _collect_mapping_rows(
        self, container_id: str, parse_value: Callable[[str], object], value_error: str
    ) -> tuple[dict[str, object], str | None]:
        result: dict[str, object] = {}
        seen: set[str] = set()
        for row in self.query_one(container_id, Vertical).query(".mapping-row"):
            name = row.query_one(".mapping-name", Input).value.strip()
            if not name:
                continue
            if name in seen:
                return {}, f'duplicate model name "{name}"'
            seen.add(name)

            raw_value = row.query_one(".mapping-value", Input).value.strip()
            if not raw_value:
                continue
            try:
                result[name] = parse_value(raw_value)
            except ValueError:
                return {}, value_error
        return result, None


class RemoveNodeConfirmScreen(ModalScreen[bool]):
    """Confirms deregistration before it's sent, since it takes effect immediately."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    RemoveNodeConfirmScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 60;
        height: auto;
        border: round $error;
        padding: 1 2;
        background: $panel;
    }
    """

    def __init__(self, host_id: str) -> None:
        super().__init__()
        self._host_id = host_id

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(f"Remove {self._host_id}?")
            yield Static(
                "This deregisters the node from the orchestrator immediately. In-flight "
                "requests are not affected; new requests will no longer be routed here."
            )
            with Horizontal():
                yield Button("Remove", id="confirm", variant="error")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class DashboardApp(App[None]):
    TITLE = "llm-home-lab — Operator Dashboard"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("e", "edit_node", "Edit node"),
        ("n", "new_node", "New node"),
        ("x", "remove_node", "Remove node"),
        ("s", "cycle_sort", "Sort column"),
        ("r", "toggle_sort_direction", "Sort direction"),
        ("o", "clear_sort", "Manual order"),
        ("u", "move_node_up", "Move up"),
        ("d", "move_node_down", "Move down"),
    ]
    CSS = """
    DataTable, #load-sparklines {
        border: round $accent;
        margin: 0 1;
        height: auto;
    }
    #banner {
        height: auto;
        padding: 0 1;
    }
    #nodes-toolbar {
        height: auto;
        margin: 0 1;
    }
    #nodes-toolbar Button {
        margin-right: 1;
    }
    Sparkline {
        height: 3;
        margin: 0 1;
    }
    .node-load-group {
        height: auto;
    }
    """

    def __init__(
        self,
        client: DiagnosticsClient,
        interval_s: float = 2.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._interval_s = interval_s
        self._clock = clock or (lambda: datetime.now(UTC))
        self._load_history: dict[str, list[float]] = {}
        self._sparkline_ids: dict[str, str] = {}
        self._node_group_ids: dict[str, str] = {}
        self._poll_in_progress = False
        self._node_row_order: list[str] = []
        self._sort_column: str | None = None
        self._sort_descending = False
        self._nodes_by_id: dict[str, dict[str, object]] = {}
        self._last_completions_total: dict[str, int] = {}
        self._last_completions_total_at: datetime | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="banner")
        with Horizontal(id="nodes-toolbar"):
            yield Button("+ New Node", id="new-node-button")
            yield Button("✕ Remove Node", id="remove-node-button", variant="error")
        yield DataTable(id="nodes-table", zebra_stripes=True)
        yield Vertical(id="load-sparklines")
        yield Footer()

    def on_mount(self) -> None:
        nodes_table = self.query_one("#nodes-table", DataTable)
        nodes_table.add_columns(
            "host_id",
            "status",
            "errors",
            "slots",
            "tasks/h",
            "tasks/h/slot",
            "backend_type",
            "in_flight/max",
            "last_seen",
            "reqs",
            "reqs/min",
            "avg_lat_ms",
            "prompt_tok",
            "compl_tok",
        )
        nodes_table.border_title = "Nodes"

        self.query_one("#load-sparklines", Vertical).border_title = "Node Load"

        self.set_interval(self._interval_s, self.poll)

    async def poll(self) -> None:
        # A slow or overlapping tick must never re-enter rendering: two concurrent passes
        # racing to mount the same not-yet-tracked node's sparkline group raises NoMatches,
        # and an unhandled exception here takes down the whole app (Textual's Timer._tick
        # always exits the app on a callback exception).
        if self._poll_in_progress:
            return
        self._poll_in_progress = True
        try:
            # Host status comes from the orchestrator's background health poller
            # (ORCHESTRATOR_HEALTH_POLL_INTERVAL_S); with it disabled, hosts stay "unknown"
            # until something else calls /health/ready.
            nodes, metrics_text = await asyncio.gather(
                self._client.list_nodes(),
                self._client.fetch_metrics_text(),
                return_exceptions=True,
            )
            errors = [
                result
                for result in (nodes, metrics_text)
                if isinstance(result, DiagnosticsClientError)
            ]
            if errors:
                self._show_banner(_BANNER_MESSAGES.get(_pick_error(errors).kind, errors[0].kind))
                return

            self._show_banner("")
            now = self._clock()
            nodes_dict = cast("dict[str, object]", nodes)
            metrics_text_str = cast(str, metrics_text)
            self._render_nodes(nodes_dict, metrics_text_str, now)
            await self._render_load_sparklines(nodes_dict)
        except Exception:
            logger.exception("dashboard render failed for this poll cycle")
            self._show_banner("dashboard render error, retrying")
        finally:
            self._poll_in_progress = False

    def _show_banner(self, message: str) -> None:
        text = Text(message, style="bold red") if message else Text("")
        self.query_one("#banner", Static).update(text)

    def _render_nodes(self, nodes: dict[str, object], metrics_text: str, now: datetime) -> None:
        table = self.query_one("#nodes-table", DataTable)
        selected_host_id = (
            self._node_row_order[table.cursor_row]
            if table.row_count and table.cursor_row < len(self._node_row_order)
            else None
        )
        table.clear()
        self._node_row_order = []
        self._nodes_by_id = {}
        parsed = parse_metrics_text(metrics_text)
        completions_rate_per_s = compute_token_rates(
            previous=self._last_completions_total,
            previous_at=self._last_completions_total_at,
            current=parsed.host_completions_total,
            now=now,
        )
        self._last_completions_total = dict(parsed.host_completions_total)
        self._last_completions_total_at = now

        hosts = cast("list[dict[str, object]]", nodes.get("nodes", []))
        for host in self._display_order(hosts, parsed, completions_rate_per_s):
            host_id = cast(str, host["host_id"])
            health = cast("dict[str, object]", host.get("health") or {})
            rate_per_s = completions_rate_per_s.get(host_id)
            table.add_row(
                host_id,
                _styled_node_status(host["status"]),
                _styled_failure_count(health.get("recent_failures")),
                _styled_external_load(host.get("external_load")),
                _format_tasks_per_hour(host.get("throughput"), "tasks_per_hour"),
                _format_tasks_per_hour(host.get("throughput"), "tasks_per_hour_per_slot"),
                host["backend_type"],
                f"{host['in_flight']}/{host['max_concurrent_requests']}",
                host["last_seen"],
                str(parsed.host_completions_total.get(host_id, 0)),
                "—" if rate_per_s is None else f"{rate_per_s * 60:.1f}/min",
                _format_avg_metric(parsed.host_latency_ms_avg.get(host_id)),
                _format_avg_min_max(
                    parsed.host_prompt_tokens_avg.get(host_id),
                    parsed.host_prompt_tokens_min.get(host_id),
                    parsed.host_prompt_tokens_max.get(host_id),
                ),
                _format_avg_min_max(
                    parsed.host_completion_tokens_avg.get(host_id),
                    parsed.host_completion_tokens_min.get(host_id),
                    parsed.host_completion_tokens_max.get(host_id),
                ),
            )
            self._node_row_order.append(host_id)
            self._nodes_by_id[host_id] = host

        if selected_host_id in self._node_row_order:
            table.move_cursor(row=self._node_row_order.index(selected_host_id))
        self._update_nodes_title()

    def _display_order(
        self,
        hosts: list[dict[str, object]],
        parsed: ParsedMetrics,
        rates_per_s: dict[str, float | None],
    ) -> list[dict[str, object]]:
        # /v1/nodes already returns the persisted manual order; a column sort is a session-only
        # view layered on top of it.
        if self._sort_column is None:
            return hosts

        by_id = {cast(str, host["host_id"]): host for host in hosts}
        column = self._sort_column
        keyed: list[tuple[str, object]] = []
        for host_id, host in by_id.items():
            value: object
            if column == "node":
                value = host_id.lower()
            elif column == "status":
                value = str(host["status"])
            elif column == "reqs/min":
                value = rates_per_s.get(host_id)
            elif column == "avg latency":
                value = parsed.host_latency_ms_avg.get(host_id)
            else:
                prompt = parsed.host_prompt_tokens_avg.get(host_id)
                completion = parsed.host_completion_tokens_avg.get(host_id)
                value = (
                    None
                    if prompt is None and completion is None
                    else (prompt or 0) + (completion or 0)
                )
            keyed.append((host_id, value))
        return [by_id[host_id] for host_id, _ in _sort_missing_last(keyed, self._sort_descending)]

    def _update_nodes_title(self) -> None:
        title = "Nodes"
        if self._sort_column is not None:
            arrow = "↓" if self._sort_descending else "↑"
            title = f"Nodes — sorted by {self._sort_column} {arrow}"
        self.query_one("#nodes-table", DataTable).border_title = title

    async def _rerender(self) -> None:
        # Re-fetch rather than re-render cached data so rates and the sparklines stay consistent.
        await self.poll()

    async def action_cycle_sort(self) -> None:
        if self._sort_column is None:
            self._sort_column = _SORT_COLUMNS[0]
        else:
            index = _SORT_COLUMNS.index(self._sort_column) + 1
            self._sort_column = _SORT_COLUMNS[index] if index < len(_SORT_COLUMNS) else None
        await self._rerender()

    async def action_toggle_sort_direction(self) -> None:
        self._sort_descending = not self._sort_descending
        await self._rerender()

    async def action_clear_sort(self) -> None:
        self._sort_column = None
        self._sort_descending = False
        await self._rerender()

    async def action_move_node_up(self) -> None:
        await self._move_selected_node(-1)

    async def action_move_node_down(self) -> None:
        await self._move_selected_node(1)

    async def _move_selected_node(self, step: int) -> None:
        table = self.query_one("#nodes-table", DataTable)
        # Moving only means something in the manual order; a column sort is a throwaway view.
        if self._sort_column is not None or table.row_count == 0:
            return
        index = table.cursor_row
        target = index + step
        if not 0 <= target < len(self._node_row_order):
            return

        order = list(self._node_row_order)
        order[index], order[target] = order[target], order[index]
        try:
            # Write the whole order, not just the swapped pair, so unpositioned nodes get
            # explicit positions and the persisted order matches what the operator sees.
            for position, host_id in enumerate(order):
                if self._nodes_by_id[host_id].get("position") != position:
                    await self._client.update_node(host_id, {"position": position})
        except DiagnosticsClientError as exc:
            self._show_banner(_BANNER_MESSAGES.get(exc.kind, exc.kind))
            return
        # Seed the cursor so _render_nodes keeps it on the moved node.
        self._node_row_order = order
        table.move_cursor(row=target)
        await self.poll()

    def action_edit_node(self) -> None:
        table = self.query_one("#nodes-table", DataTable)
        if table.row_count == 0:
            return
        host_id = self._node_row_order[table.cursor_row]
        node = self._nodes_by_id[host_id]

        async def handle_result(fields: dict[str, object] | None) -> None:
            if fields is None:
                return
            try:
                await self._client.update_node(host_id, fields)
            except DiagnosticsClientError as exc:
                self._show_banner(_BANNER_MESSAGES.get(exc.kind, exc.kind))
                return
            await self.poll()

        self.push_screen(NodeFormScreen(mode="edit", host_id=host_id, node=node), handle_result)

    def action_new_node(self) -> None:
        async def handle_result(fields: dict[str, object] | None) -> None:
            if fields is None:
                return
            try:
                await self._client.register_node(fields)
            except DiagnosticsClientError as exc:
                self._show_banner(_BANNER_MESSAGES.get(exc.kind, exc.kind))
                return
            await self.poll()

        self.push_screen(NodeFormScreen(mode="register", host_id="", node={}), handle_result)

    def action_remove_node(self) -> None:
        table = self.query_one("#nodes-table", DataTable)
        if table.row_count == 0:
            return
        host_id = self._node_row_order[table.cursor_row]

        async def handle_result(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                await self._client.deregister_node(host_id)
            except DiagnosticsClientError as exc:
                self._show_banner(_BANNER_MESSAGES.get(exc.kind, exc.kind))
                return
            await self.poll()

        self.push_screen(RemoveNodeConfirmScreen(host_id), handle_result)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-node-button":
            self.action_new_node()
        elif event.button.id == "remove-node-button":
            self.action_remove_node()

    async def _render_load_sparklines(self, nodes: dict[str, object]) -> None:
        ratios: dict[str, float] = {}
        for host in cast("list[dict[str, object]]", nodes.get("nodes", [])):
            host_id = cast(str, host["host_id"])
            max_concurrent = cast(int, host["max_concurrent_requests"]) or 1
            ratios[host_id] = cast(int, host["in_flight"]) / max_concurrent

        self._load_history = update_load_history(self._load_history, ratios)

        container = self.query_one("#load-sparklines", Vertical)
        for stale_host_id in [hid for hid in self._node_group_ids if hid not in ratios]:
            stale_group_id = self._node_group_ids.pop(stale_host_id)
            self._sparkline_ids.pop(stale_host_id, None)
            await self.query_one(f"#{stale_group_id}").remove()

        used_ids = set(self._node_group_ids.values()) | set(self._sparkline_ids.values())
        for host_id in ratios:
            if host_id not in self._node_group_ids:
                load_id = _sparkline_widget_id(host_id, "load", used_ids)
                group_id = _sparkline_widget_id(host_id, "group", used_ids)
                self._sparkline_ids[host_id] = load_id
                self._node_group_ids[host_id] = group_id
                await container.mount(
                    Vertical(
                        Label(host_id),
                        Sparkline(id=load_id, min_color="green", max_color="green"),
                        id=group_id,
                        classes="node-load-group",
                    )
                )
            self.query_one(f"#{self._sparkline_ids[host_id]}", Sparkline).data = self._load_history[
                host_id
            ]

        # Keep the groups in the same order as the Nodes table rows.
        displayed = [hid for hid in self._node_row_order if hid in self._node_group_ids]
        mounted = [child.id for child in container.children]
        wanted = [self._node_group_ids[hid] for hid in displayed]
        if mounted != wanted:
            for index, group_id in enumerate(wanted):
                container.move_child(self.query_one(f"#{group_id}"), before=index)


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="TUI operator dashboard")
    parser.add_argument(
        "--base-url", default=os.environ.get("ORCHESTRATOR_BASE_URL", "http://localhost:8080")
    )
    parser.add_argument("--api-key", default=os.environ.get("ORCHESTRATOR_API_KEY"))
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args(argv)

    if not args.api_key:
        print("error: an API key is required (--api-key or ORCHESTRATOR_API_KEY)", file=sys.stderr)
        raise SystemExit(1)

    client = OrchestratorDiagnosticsClient(base_url=args.base_url, api_key=args.api_key)
    app = DashboardApp(client=client, interval_s=args.interval)
    app.run()


if __name__ == "__main__":
    run()
