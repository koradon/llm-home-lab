import argparse
import asyncio
import logging
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Sparkline, Static

from llm_home_lab.diagnostics.metrics_parser import parse_metrics_text
from llm_home_lab.tui.client import DiagnosticsClientError, OrchestratorDiagnosticsClient
from llm_home_lab.tui.load_history import update_load_history
from llm_home_lab.tui.rates import compute_token_rates

logger = logging.getLogger(__name__)

_BANNER_MESSAGES = {
    "unauthorized": "not authorized — check API key",
    "connection": "cannot reach orchestrator, retrying",
    "server_error": "orchestrator error, retrying",
}
_ERROR_KIND_PRIORITY = ("unauthorized", "connection", "server_error")


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


_SEVERITY_STYLES = {"critical": "bold red", "warning": "bold yellow"}


def _styled_severity(severity: object) -> Text:
    style = _SEVERITY_STYLES.get(str(severity), "")
    return Text(str(severity), style=style)


_NODE_STATUS_STYLES = {"online": "bold green", "offline": "bold red", "unknown": "bold yellow"}


def _styled_node_status(status: object) -> Text:
    style = _NODE_STATUS_STYLES.get(str(status), "")
    return Text(str(status), style=style)


def _styled_external_load(external_load: object) -> Text:
    load = external_load if isinstance(external_load, dict) else {}
    if not load.get("available"):
        return Text("unavailable", style="dim")

    status = str(load.get("status") or "idle")
    queued = load.get("queued") or 0
    label = status if not queued else f"{status} ({queued} queued)"
    return Text(label, style="" if status == "idle" else "bold yellow")


def _external_load_value(external_load: object) -> float:
    # `queued` alone is almost always 0 for a single caller hitting a node directly (it only
    # counts requests waiting behind another), so a busy status must contribute even with no
    # backlog — otherwise a genuinely busy node charts as a flat, empty line.
    load = external_load if isinstance(external_load, dict) else {}
    if not load.get("available"):
        return 0.0
    status = load.get("status")
    busy = 0.0 if status in (None, "idle") else 1.0
    queued = load.get("queued") or 0
    return busy + (float(queued) if isinstance(queued, int | float) else 0.0)


class DiagnosticsClient(Protocol):
    async def list_nodes(self) -> dict[str, object]: ...
    async def list_alerts(self) -> dict[str, object]: ...
    async def fetch_metrics_text(self) -> str: ...
    async def trigger_health_check(self) -> None: ...
    async def update_node(self, host_id: str, fields: dict[str, object]) -> None: ...


class NodeEditScreen(ModalScreen[dict[str, object] | None]):
    """Prompts for a registered node's editable parameters, prefilled from its current state."""

    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    NodeEditScreen {
        align: center middle;
    }
    #edit-dialog {
        width: 60;
        height: auto;
        border: round $accent;
        padding: 1 2;
        background: $panel;
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
    """

    def __init__(self, host_id: str, node: dict[str, object]) -> None:
        super().__init__()
        self._host_id = host_id
        self._node = node

    def compose(self) -> ComposeResult:
        node = self._node
        allowed_models = cast("list[str] | None", node.get("allowed_models")) or []
        memory_budget_gb = node.get("memory_budget_gb")
        with Vertical(id="edit-dialog"):
            yield Label(f"Edit {self._host_id}")
            yield Label("context_window")
            yield Input(value=str(node.get("context_window", "")), id="context_window")
            yield Label("max_concurrent_requests")
            yield Input(
                value=str(node.get("max_concurrent_requests", "")),
                id="max_concurrent_requests",
            )
            yield Label("base_url")
            yield Input(value=str(node.get("base_url", "")), id="base_url")
            yield Label("allowed_models (comma-separated, blank = unrestricted)")
            yield Input(value=", ".join(allowed_models), id="allowed_models")
            yield Label("memory_budget_gb (blank = unset)")
            yield Input(
                value="" if memory_budget_gb is None else str(memory_budget_gb),
                id="memory_budget_gb",
            )
            yield Static("", id="edit-error")
            with Horizontal():
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
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
            "context_window": context_window,
            "max_concurrent_requests": max_concurrent_requests,
            "base_url": base_url,
        }

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

        return fields, None


class DashboardApp(App[None]):
    TITLE = "llm-home-lab — Operator Dashboard"
    BINDINGS = [("q", "quit", "Quit"), ("e", "edit_node", "Edit node")]
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
    Sparkline {
        height: 3;
        margin: 0 1;
    }
    .node-load-group {
        height: auto;
    }
    .spark-row {
        height: 3;
    }
    .spark-label {
        width: 5;
        content-align: left middle;
        padding: 0 1;
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
        self._last_token_usage: dict[str, int] = {}
        self._last_token_usage_at: datetime | None = None
        self._load_history: dict[str, list[float]] = {}
        self._sparkline_ids: dict[str, str] = {}
        self._ext_load_history: dict[str, list[float]] = {}
        self._ext_load_sparkline_ids: dict[str, str] = {}
        self._node_group_ids: dict[str, str] = {}
        self._poll_in_progress = False
        self._node_row_order: list[str] = []
        self._nodes_by_id: dict[str, dict[str, object]] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="banner")
        yield DataTable(id="nodes-table", zebra_stripes=True)
        yield DataTable(id="alerts-table", zebra_stripes=True)
        yield DataTable(id="queue-tokens-table", zebra_stripes=True)
        yield Vertical(id="load-sparklines")
        yield Footer()

    def on_mount(self) -> None:
        nodes_table = self.query_one("#nodes-table", DataTable)
        nodes_table.add_columns(
            "host_id", "status", "ext_load", "backend_type", "in_flight/max", "last_seen"
        )
        nodes_table.border_title = "Nodes"

        alerts_table = self.query_one("#alerts-table", DataTable)
        alerts_table.add_columns("rule_name", "severity", "value/threshold", "runbook_url")
        alerts_table.border_title = "Alerts"

        queue_table = self.query_one("#queue-tokens-table", DataTable)
        queue_table.add_columns("metric", "value")
        queue_table.border_title = "Queue & Tokens"

        self.query_one(
            "#load-sparklines", Vertical
        ).border_title = "Node Load — green: own, orange: external"

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
            # /v1/nodes only reports a status other than "unknown" for hosts that have a
            # recorded probe, and nothing records one except a call to /health/ready — the
            # dashboard triggers it itself here so status reflects reality instead of staying
            # "unknown" forever. Its outcome is deliberately excluded from `errors` below: a
            # transport failure here shouldn't blank out nodes/alerts/tokens that loaded fine.
            nodes, alerts, metrics_text, _health_check = await asyncio.gather(
                self._client.list_nodes(),
                self._client.list_alerts(),
                self._client.fetch_metrics_text(),
                self._client.trigger_health_check(),
                return_exceptions=True,
            )
            errors = [
                result
                for result in (nodes, alerts, metrics_text)
                if isinstance(result, DiagnosticsClientError)
            ]
            if errors:
                self._show_banner(_BANNER_MESSAGES.get(_pick_error(errors).kind, errors[0].kind))
                return

            self._show_banner("")
            nodes_dict = cast("dict[str, object]", nodes)
            self._render_nodes(nodes_dict)
            self._render_alerts(cast("dict[str, object]", alerts))
            self._render_queue_tokens(cast(str, metrics_text))
            await self._render_load_sparklines(nodes_dict)
        except Exception:
            logger.exception("dashboard render failed for this poll cycle")
            self._show_banner("dashboard render error, retrying")
        finally:
            self._poll_in_progress = False

    def _show_banner(self, message: str) -> None:
        text = Text(message, style="bold red") if message else Text("")
        self.query_one("#banner", Static).update(text)

    def _render_nodes(self, nodes: dict[str, object]) -> None:
        table = self.query_one("#nodes-table", DataTable)
        table.clear()
        self._node_row_order = []
        self._nodes_by_id = {}
        for host in cast("list[dict[str, object]]", nodes.get("nodes", [])):
            host_id = cast(str, host["host_id"])
            table.add_row(
                host_id,
                _styled_node_status(host["status"]),
                _styled_external_load(host.get("external_load")),
                host["backend_type"],
                f"{host['in_flight']}/{host['max_concurrent_requests']}",
                host["last_seen"],
            )
            self._node_row_order.append(host_id)
            self._nodes_by_id[host_id] = host

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

        self.push_screen(NodeEditScreen(host_id, node), handle_result)

    def _render_alerts(self, alerts: dict[str, object]) -> None:
        table = self.query_one("#alerts-table", DataTable)
        table.clear()
        for alert in cast("list[dict[str, object]]", alerts.get("alerts", [])):
            table.add_row(
                alert["rule_name"],
                _styled_severity(alert["severity"]),
                f"{alert['value']}/{alert['threshold_value']}",
                alert["runbook_url"],
            )

    def _render_queue_tokens(self, metrics_text: str) -> None:
        table = self.query_one("#queue-tokens-table", DataTable)
        table.clear()
        parsed = parse_metrics_text(metrics_text)
        now = self._clock()
        rates = compute_token_rates(
            previous=self._last_token_usage,
            previous_at=self._last_token_usage_at,
            current=parsed.token_usage_total,
            now=now,
        )
        self._last_token_usage = dict(parsed.token_usage_total)
        self._last_token_usage_at = now

        queue_depth = "unavailable" if parsed.queue_depth is None else str(parsed.queue_depth)
        table.add_row("queue_depth", queue_depth)

        p95 = "unavailable" if parsed.p95_latency_ms is None else f"{parsed.p95_latency_ms:.0f}"
        table.add_row("p95_latency_ms", p95)

        for host_id, total in parsed.token_usage_total.items():
            table.add_row(f"tokens[{host_id}]", str(total))
            rate = rates.get(host_id)
            table.add_row(f"tokens/s[{host_id}]", "-" if rate is None else f"{rate:.1f}/s")

    async def _render_load_sparklines(self, nodes: dict[str, object]) -> None:
        ratios: dict[str, float] = {}
        ext_loads: dict[str, float] = {}
        for host in cast("list[dict[str, object]]", nodes.get("nodes", [])):
            host_id = cast(str, host["host_id"])
            max_concurrent = cast(int, host["max_concurrent_requests"]) or 1
            ratios[host_id] = cast(int, host["in_flight"]) / max_concurrent
            ext_loads[host_id] = _external_load_value(host.get("external_load"))

        self._load_history = update_load_history(self._load_history, ratios)
        self._ext_load_history = update_load_history(self._ext_load_history, ext_loads)

        container = self.query_one("#load-sparklines", Vertical)
        for stale_host_id in [hid for hid in self._node_group_ids if hid not in ratios]:
            stale_group_id = self._node_group_ids.pop(stale_host_id)
            self._sparkline_ids.pop(stale_host_id, None)
            self._ext_load_sparkline_ids.pop(stale_host_id, None)
            await self.query_one(f"#{stale_group_id}").remove()

        used_ids = (
            set(self._node_group_ids.values())
            | set(self._sparkline_ids.values())
            | set(self._ext_load_sparkline_ids.values())
        )
        for host_id in ratios:
            if host_id not in self._node_group_ids:
                own_id = _sparkline_widget_id(host_id, "load", used_ids)
                ext_id = _sparkline_widget_id(host_id, "extload", used_ids)
                group_id = _sparkline_widget_id(host_id, "group", used_ids)
                self._sparkline_ids[host_id] = own_id
                self._ext_load_sparkline_ids[host_id] = ext_id
                self._node_group_ids[host_id] = group_id
                await container.mount(
                    Vertical(
                        Label(host_id),
                        Horizontal(
                            Label("own", classes="spark-label"),
                            Sparkline(id=own_id, min_color="green", max_color="green"),
                            classes="spark-row",
                        ),
                        Horizontal(
                            Label("ext", classes="spark-label"),
                            Sparkline(id=ext_id, min_color="orange", max_color="orange"),
                            classes="spark-row",
                        ),
                        id=group_id,
                        classes="node-load-group",
                    )
                )
            self.query_one(f"#{self._sparkline_ids[host_id]}", Sparkline).data = self._load_history[
                host_id
            ]
            self.query_one(
                f"#{self._ext_load_sparkline_ids[host_id]}", Sparkline
            ).data = self._ext_load_history[host_id]


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
