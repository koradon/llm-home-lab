import asyncio
import re
from datetime import UTC, datetime, timedelta

from textual.widgets import Button, DataTable, Input, Label, Select, Sparkline, Static

from llm_home_lab.tui.app import DashboardApp, NodeFormScreen, RemoveNodeConfirmScreen, _load_label
from llm_home_lab.tui.client import DiagnosticsClientError
from llm_home_lab.tui.load_chart import GREEN, ORANGE, RED

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeClient:
    def __init__(self, nodes=None, metrics_text=None, error=None):
        self._nodes = nodes or {"nodes": []}
        self._metrics_text = metrics_text or ""
        self._error = error
        self.register_node_calls = []
        self.update_node_calls = []
        self.deregister_node_calls = []

    async def register_node(self, fields):
        self.register_node_calls.append(fields)

    async def update_node(self, host_id, fields):
        self.update_node_calls.append((host_id, fields))

    async def deregister_node(self, host_id):
        self.deregister_node_calls.append(host_id)

    async def list_nodes(self):
        if self._error:
            raise self._error
        return self._nodes

    async def fetch_metrics_text(self):
        if self._error:
            raise self._error
        return self._metrics_text


async def test_successful_poll_shows_only_nodes_and_one_load_series_per_node():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=4)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        await pilot.pause()

        table = app.query_one("#nodes-table", DataTable)
        assert table.row_count == 1
        assert len(app.query(DataTable)) == 1
        assert not app.query("#alerts-table, #queue-tokens-table")
        assert "ext_load" not in [str(column.label) for column in table.ordered_columns]
        assert len(app.query(Sparkline)) == 1
        assert list(app.query_one(Sparkline).data) == [1.0]
        panel = app.query_one("#load-sparklines")
        assert panel.border_title == "Node Load"
        assert panel.region.bottom <= 39
        assert str(app.query_one("#banner", Static).render()) == ""


async def test_connection_failure_shows_connection_banner_and_keeps_running():
    client = _FakeClient(error=DiagnosticsClientError("connection", "refused"))
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        banner = app.query_one("#banner", Static)
        assert "cannot reach orchestrator" in str(banner.render())


async def test_unauthorized_failure_shows_distinct_auth_banner():
    client = _FakeClient(error=DiagnosticsClientError("unauthorized", "status 401"))
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        banner = app.query_one("#banner", Static)
        assert "not authorized" in str(banner.render())


async def test_banner_clears_after_a_successful_poll_following_a_failure():
    client = _FakeClient(error=DiagnosticsClientError("connection", "refused"))
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()
        client._error = None
        await app.poll()

        banner = app.query_one("#banner", Static)
        assert str(banner.render()) == ""


def _node(
    host_id,
    in_flight=0,
    max_concurrent_requests=4,
    status="online",
    recent_failures=None,
    lease=None,
):
    node = {
        "host_id": host_id,
        "backend_type": "lmstudio",
        "in_flight": in_flight,
        "max_concurrent_requests": max_concurrent_requests,
        "last_seen": "2026-07-19T00:00:00+00:00",
        "status": status,
    }
    if recent_failures is not None:
        node["health"] = {"recent_failures": recent_failures}
    if lease is not None:
        node["lease"] = lease
    return node


def _lease(holder, until, *, expiring_soon=False):
    return {
        "holder": holder,
        "expires_at": until,
        "hard_expires_at": until,
        "expiring_soon": expiring_soon,
    }


async def test_a_sparkline_is_mounted_for_each_node_with_its_load_ratio():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=2, max_concurrent_requests=4)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        load_sparkline = app.query_one("#load-host-a", Sparkline)
        assert list(load_sparkline.data) == [0.5]


async def test_a_second_poll_appends_to_the_same_nodes_sparkline():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=2, max_concurrent_requests=4)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()
        client._nodes = {"nodes": [_node("host-a", in_flight=1, max_concurrent_requests=4)]}
        await app.poll()

        load_sparkline = app.query_one("#load-host-a", Sparkline)
        assert list(load_sparkline.data) == [0.5, 0.25]


async def test_each_nodes_load_group_stays_compact_instead_of_stretching_to_fill_the_panel():
    client = _FakeClient(
        nodes={"nodes": [_node("host-a", in_flight=0), _node("host-b", in_flight=0)]}
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.pause()

        group = app.query_one("#group-host-a")
        assert group.size.height == 5


async def test_a_node_that_disappears_has_its_sparkline_removed():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=2, max_concurrent_requests=4)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()
        client._nodes = {"nodes": []}
        await app.poll()

        assert len(app.query(Sparkline)) == 0


async def test_colliding_sanitized_host_ids_get_distinct_sparklines():
    client = _FakeClient(
        nodes={
            "nodes": [
                _node("node.1", in_flight=1, max_concurrent_requests=4),
                _node("node_1", in_flight=2, max_concurrent_requests=4),
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        sparklines = app.query(Sparkline)
        assert len(sparklines) == 2
        ids = {s.id for s in sparklines}
        assert len(ids) == 2


async def test_overlapping_polls_do_not_crash_the_app():
    client = _FakeClient(
        nodes={
            "nodes": [
                _node("host-a", in_flight=1),
                _node("host-b", in_flight=2),
                _node("host-c", in_flight=0),
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await asyncio.gather(app.poll(), app.poll())

        assert len(app.query(Sparkline)) == 3


def _status_span_style(cell):
    # The styled status word is the first span; the counter after it stays plain text.
    return str(cell.spans[0].style) if cell.spans else ""


def _styled_parts(cell):
    # (text, style) for the status word and the counter; "" means unstyled default colour.
    word, counter = cell.plain.split(" ")
    styles = {cell.plain[span.start : span.end]: str(span.style) for span in cell.spans}
    return (word, styles.get(word, "")), (counter, styles.get(counter, ""))


async def test_the_counter_colours_follow_load_and_availability():
    client = _FakeClient(
        nodes={
            "nodes": [
                _node("busy", in_flight=8, max_concurrent_requests=8),
                _node("idle", in_flight=0, max_concurrent_requests=8),
                _node("off", in_flight=0, max_concurrent_requests=5, status="offline"),
                _node("unk", in_flight=2, max_concurrent_requests=4, status="unknown"),
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        cells = [
            table.get_cell(table.coordinate_to_cell_key((row, 0))[0], table.ordered_columns[1].key)
            for row in range(4)
        ]
        assert [_styled_parts(cell) for cell in cells] == [
            (("online", "bold green"), ("8/8", "bold yellow")),
            (("online", "bold green"), ("0/8", "")),
            (("offline", "bold red"), ("0/5", "dim")),
            (("unknown", "bold yellow"), ("2/4", "dim")),
        ]


def _span_styles(cell):
    return {cell.plain[span.start : span.end]: str(span.style) for span in cell.spans}


def _enable_screenshot_colour(monkeypatch) -> None:
    # NO_COLOR and TERM=dumb flatten export_screenshot to grey, so a colour assertion
    # never sees the palette the chart and the lease status use.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")


def _svg_word_fills(svg: str) -> dict[str, set[str]]:
    rules = dict(re.findall(r"\.(terminal-\d+-r\d+)\s*\{\s*fill:\s*(#[0-9A-Fa-f]{6})", svg))
    fills: dict[str, set[str]] = {}
    for match in re.finditer(r'<text class="([^"]+)"[^>]*>([^<]+)</text>', svg):
        word = match.group(2).replace("&#160;", " ").strip()
        if word not in {"online", "offline", "reserved", "expiring"}:
            continue
        colour = rules.get(match.group(1).split()[0])
        assert colour is not None
        fills.setdefault(word, set()).add(colour)
    return fills


async def test_a_reserved_node_is_coloured_apart_from_online_and_offline(monkeypatch):
    _enable_screenshot_colour(monkeypatch)
    until = "2026-10-25T13:00:00+00:00"
    client = _FakeClient(
        nodes={
            "nodes": [
                _node("normal", in_flight=0),
                _node("held", in_flight=1, lease=_lease("ada", until)),
                _node("soon", in_flight=1, lease=_lease("bea", until, expiring_soon=True)),
                _node("down", in_flight=0, status="offline"),
                _node(
                    "down-held",
                    in_flight=0,
                    status="offline",
                    lease=_lease("cy", until),
                ),
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(160, 40)) as pilot:
        await app.poll()
        await pilot.pause()

        table = app.query_one("#nodes-table", DataTable)
        cells = [
            table.get_cell(table.coordinate_to_cell_key((row, 0))[0], table.ordered_columns[1].key)
            for row in range(5)
        ]
        assert cells[0].plain == "online 0/4"
        assert "reserved ada until 2026-10-25T13:00:00+00:00" in cells[1].plain
        assert "expiring bea until 2026-10-25T13:00:00+00:00" in cells[2].plain
        assert cells[3].plain == "offline 0/4"
        assert cells[4].plain.startswith("offline reserved cy until")

        styles = [_span_styles(cell) for cell in cells]
        assert styles[0]["online"] == "bold green"
        assert "c000ff" in styles[1]["reserved"].lower()
        assert "ff8800" in styles[2]["expiring"].lower()
        assert styles[3]["offline"] == "bold red"
        assert styles[4]["offline"] == "bold red"
        assert "c000ff" in styles[4]["reserved"].lower()
        svg = app.export_screenshot()

    fills = _svg_word_fills(svg)
    assert fills["reserved"] == {"#c000ff"}
    assert fills["expiring"] == {"#ff8800"}
    assert fills["online"] and fills["offline"]
    assert fills["online"].isdisjoint(fills["offline"])
    assert fills["offline"].isdisjoint(fills["reserved"])
    assert fills["reserved"].isdisjoint(fills["expiring"])
    assert fills["online"].isdisjoint(fills["reserved"])


async def test_the_status_cell_shows_load_counter_next_to_the_status():
    client = _FakeClient(
        nodes={
            "nodes": [
                _node("host-a", in_flight=8, max_concurrent_requests=8),
                _node("host-b", in_flight=0, max_concurrent_requests=5, status="offline"),
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        labels = [str(column.label) for column in table.ordered_columns]
        cells = [
            str(
                table.get_cell(
                    table.coordinate_to_cell_key((row, 0))[0], table.ordered_columns[1].key
                )
            )
            for row in range(2)
        ]
        assert cells == ["online 8/8", "offline 0/5"]
        assert "in_flight/max" not in labels
        assert "slots" not in labels


async def test_an_offline_node_status_is_styled_red_and_stays_listed():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=0, status="offline")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[1].key
        status_cell = table.get_cell(row_key, column_key)
        assert table.row_count == 1
        assert str(status_cell) == "offline 0/4"
        assert "red" in _status_span_style(status_cell)


async def test_an_unknown_node_status_is_styled_distinctly_from_online():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=0, status="unknown")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[1].key
        status_cell = table.get_cell(row_key, column_key)
        assert "yellow" in _status_span_style(status_cell)


async def test_an_online_node_status_has_no_alarming_style():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=0, status="online")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[1].key
        status_cell = table.get_cell(row_key, column_key)
        assert "red" not in _status_span_style(status_cell)
        assert "yellow" not in _status_span_style(status_cell)


async def test_a_node_with_recent_failures_shows_a_red_highlighted_count():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=0, recent_failures=2)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[2].key
        errors_cell = table.get_cell(row_key, column_key)
        assert str(errors_cell) == "2"
        assert "red" in errors_cell.style


async def test_a_node_with_no_recent_failures_shows_zero_with_no_alarming_style():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=0, recent_failures=0)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[2].key
        errors_cell = table.get_cell(row_key, column_key)
        assert str(errors_cell) == "0"
        assert "red" not in errors_cell.style


async def test_a_node_with_a_missing_health_field_shows_zero_errors():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=0)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[2].key
        errors_cell = table.get_cell(row_key, column_key)
        assert str(errors_cell) == "0"


async def test_a_node_with_no_recorded_completions_shows_zero_reqs_and_unavailable_stats():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=0)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        columns = table.ordered_columns
        assert str(table.get_cell(row_key, columns[7].key)) == "0"
        assert str(table.get_cell(row_key, columns[8].key)) == "—"
        assert str(table.get_cell(row_key, columns[9].key)) == "—"
        assert str(table.get_cell(row_key, columns[10].key)) == "—"
        assert str(table.get_cell(row_key, columns[11].key)) == "—"


async def test_a_node_with_completion_stats_reports_them_in_the_nodes_table():
    client = _FakeClient(
        nodes={"nodes": [_node("host-a", in_flight=0)]},
        metrics_text=(
            'llm_home_lab_host_completions_total{host_id="host-a"} 12\n'
            'llm_home_lab_host_latency_ms_avg{host_id="host-a"} 245.0\n'
            'llm_home_lab_host_prompt_tokens_avg{host_id="host-a"} 200.0\n'
            'llm_home_lab_host_prompt_tokens_min{host_id="host-a"} 50\n'
            'llm_home_lab_host_prompt_tokens_max{host_id="host-a"} 400\n'
            'llm_home_lab_host_completion_tokens_avg{host_id="host-a"} 20.0\n'
            'llm_home_lab_host_completion_tokens_min{host_id="host-a"} 5\n'
            'llm_home_lab_host_completion_tokens_max{host_id="host-a"} 40\n'
        ),
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        columns = table.ordered_columns
        assert str(table.get_cell(row_key, columns[7].key)) == "12"
        assert str(table.get_cell(row_key, columns[9].key)) == "245"
        assert str(table.get_cell(row_key, columns[10].key)) == "200 (50–400)"
        assert str(table.get_cell(row_key, columns[11].key)) == "20 (5–40)"


async def test_reqs_per_min_is_computed_between_two_polls():
    clock_values = iter([T0, T0 + timedelta(seconds=30)])
    client = _FakeClient(
        nodes={"nodes": [_node("host-a", in_flight=0)]},
        metrics_text='llm_home_lab_host_completions_total{host_id="host-a"} 10\n',
    )
    app = DashboardApp(client=client, interval_s=100.0, clock=lambda: next(clock_values))

    async with app.run_test():
        await app.poll()
        client._metrics_text = 'llm_home_lab_host_completions_total{host_id="host-a"} 40\n'
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[8].key
        assert str(table.get_cell(row_key, column_key)) == "60.0/min"


async def test_throughput_columns_show_tasks_per_hour_and_per_slot():
    client = _FakeClient(
        nodes={
            "nodes": [
                {
                    **_node("host-a", in_flight=0, max_concurrent_requests=8),
                    "throughput": {
                        "window_s": 3600.0,
                        "tasks_per_hour": 24.0,
                        "tasks_per_hour_per_slot": 3.0,
                    },
                }
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        columns = table.ordered_columns
        assert str(table.get_cell(row_key, columns[3].key)) == "24.0"
        assert str(table.get_cell(row_key, columns[4].key)) == "3.0"


async def test_throughput_per_slot_shows_na_when_the_slot_count_is_unknown():
    client = _FakeClient(
        nodes={
            "nodes": [
                {
                    **_node("host-a", in_flight=0, max_concurrent_requests=0),
                    "throughput": {
                        "window_s": 3600.0,
                        "tasks_per_hour": 0.0,
                        "tasks_per_hour_per_slot": None,
                    },
                }
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        columns = table.ordered_columns
        assert str(table.get_cell(row_key, columns[3].key)) == "0.0"
        assert str(table.get_cell(row_key, columns[4].key)) == "n/a"


async def test_throughput_columns_show_na_when_the_payload_has_no_throughput():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=0)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        columns = table.ordered_columns
        assert str(table.get_cell(row_key, columns[3].key)) == "n/a"
        assert str(table.get_cell(row_key, columns[4].key)) == "n/a"


def _full_node(host_id, **overrides):
    node = {
        "host_id": host_id,
        "backend_type": "lmstudio",
        "context_window": 8192,
        "base_url": "http://localhost:1234",
        "allowed_models": None,
        "memory_budget_gb": None,
        "in_flight": 0,
        "max_concurrent_requests": 4,
        "last_seen": "2026-07-19T00:00:00+00:00",
        "status": "online",
        "external_load": None,
    }
    node.update(overrides)
    return node


async def test_pressing_e_opens_an_edit_modal_prefilled_with_the_selected_nodes_values():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a", max_concurrent_requests=4)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        assert isinstance(app.screen, NodeFormScreen)
        assert app.screen.query_one("#context_window", Input).value == "8192"
        assert app.screen.query_one("#max_concurrent_requests", Input).value == "4"
        assert app.screen.query_one("#base_url", Input).value == "http://localhost:1234"


async def test_saving_the_edit_modal_sends_the_updated_fields_and_refreshes():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a", max_concurrent_requests=4)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#max_concurrent_requests", Input).value = "8"

        await pilot.click("#save")
        await pilot.pause()

        assert client.update_node_calls == [
            (
                "host-a",
                {
                    "backend_type": "lmstudio",
                    "context_window": 8192,
                    "max_concurrent_requests": 8,
                    "base_url": "http://localhost:1234",
                    "allowed_models": None,
                    "memory_budget_gb": None,
                    "model_sizes_gb": None,
                    "model_aliases": None,
                    "model_limits": None,
                },
            )
        ]
        assert not isinstance(app.screen, NodeFormScreen)


async def test_editing_a_node_with_an_unrecognized_backend_type_preserves_it_on_save():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a", backend_type="ollama")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        assert app.screen.query_one("#backend_type", Select).value == "ollama"

        await pilot.click("#save")
        await pilot.pause()

        assert client.update_node_calls == [
            (
                "host-a",
                {
                    "backend_type": "ollama",
                    "context_window": 8192,
                    "max_concurrent_requests": 4,
                    "base_url": "http://localhost:1234",
                    "allowed_models": None,
                    "memory_budget_gb": None,
                    "model_sizes_gb": None,
                    "model_aliases": None,
                    "model_limits": None,
                },
            )
        ]


async def test_cancelling_the_edit_modal_sends_no_update():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        await pilot.click("#cancel")
        await pilot.pause()

        assert client.update_node_calls == []
        assert not isinstance(app.screen, NodeFormScreen)


async def test_pressing_e_with_no_nodes_registered_does_nothing():
    client = _FakeClient(nodes={"nodes": []})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()

        await pilot.press("e")
        await pilot.pause()

        assert not isinstance(app.screen, NodeFormScreen)


async def test_editing_allowed_models_sends_a_parsed_comma_separated_list():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a", allowed_models=["model-a"])]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()
        assert app.screen.query_one("#allowed_models", Input).value == "model-a"
        app.screen.query_one("#allowed_models", Input).value = "model-a, model-b"

        await pilot.click("#save")
        await pilot.pause()

        assert client.update_node_calls[0][1]["allowed_models"] == ["model-a", "model-b"]


async def test_an_invalid_numeric_field_keeps_the_modal_open_with_an_error():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#max_concurrent_requests", Input).value = "not-a-number"

        await pilot.click("#save")
        await pilot.pause()

        assert isinstance(app.screen, NodeFormScreen)
        assert client.update_node_calls == []
        error = app.screen.query_one("#edit-error", Static)
        assert "whole numbers" in str(error.render())


async def test_editing_prefills_model_sizes_and_aliases_as_separate_rows():
    client = _FakeClient(
        nodes={
            "nodes": [
                _full_node(
                    "host-a",
                    model_sizes_gb={"model-a": 12.5},
                    model_aliases={"model-a": ["alias-1", "alias-2"]},
                )
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        sizes = app.screen.query_one("#model-sizes-rows")
        aliases = app.screen.query_one("#model-aliases-rows")
        assert [w.value for w in sizes.query(".mapping-name")] == ["model-a"]
        assert [w.value for w in sizes.query(".mapping-value")] == ["12.5"]
        assert [w.value for w in aliases.query(".mapping-name")] == ["model-a"]
        assert [w.value for w in aliases.query(".mapping-value")] == ["alias-1, alias-2"]


async def test_saving_the_edit_modal_sends_model_sizes_and_aliases_as_dicts():
    client = _FakeClient(
        nodes={
            "nodes": [
                _full_node(
                    "host-a",
                    model_sizes_gb={"model-a": 12.5},
                    model_aliases={"model-a": ["alias-1", "alias-2"]},
                )
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        await pilot.click("#save")
        await pilot.pause()

        _, fields = client.update_node_calls[0]
        assert fields["model_sizes_gb"] == {"model-a": 12.5}
        assert fields["model_aliases"] == {"model-a": ["alias-1", "alias-2"]}


async def test_adding_a_new_size_row_and_saving_includes_it():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        app.screen.query_one("#add-size", Button).press()
        await pilot.pause()
        sizes = app.screen.query_one("#model-sizes-rows")
        list(sizes.query(".mapping-name"))[0].value = "model-x"
        list(sizes.query(".mapping-value"))[0].value = "4"

        await pilot.click("#save")
        await pilot.pause()

        assert client.update_node_calls[0][1]["model_sizes_gb"] == {"model-x": 4.0}


async def test_removing_a_mapping_row_excludes_it_from_the_saved_dict():
    client = _FakeClient(
        nodes={"nodes": [_full_node("host-a", model_sizes_gb={"model-a": 12.5, "model-b": 8.0})]}
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        first_remove_button = list(
            app.screen.query_one("#model-sizes-rows").query(".mapping-remove")
        )[0]
        first_remove_button.press()
        await pilot.pause()

        await pilot.click("#save")
        await pilot.pause()

        assert client.update_node_calls[0][1]["model_sizes_gb"] == {"model-b": 8.0}


async def test_duplicate_model_name_in_mapping_rows_is_rejected():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        app.screen.query_one("#add-size", Button).press()
        app.screen.query_one("#add-size", Button).press()
        await pilot.pause()
        sizes = app.screen.query_one("#model-sizes-rows")
        names = list(sizes.query(".mapping-name"))
        values = list(sizes.query(".mapping-value"))
        names[0].value = "model-a"
        values[0].value = "1"
        names[1].value = "model-a"
        values[1].value = "2"

        await pilot.click("#save")
        await pilot.pause()

        assert isinstance(app.screen, NodeFormScreen)
        assert client.update_node_calls == []
        error = app.screen.query_one("#edit-error", Static)
        assert "duplicate" in str(error.render())


async def test_clicking_new_node_button_opens_a_blank_register_modal():
    client = _FakeClient(nodes={"nodes": []})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.click("#new-node-button")
        await pilot.pause()

        assert isinstance(app.screen, NodeFormScreen)
        assert app.screen.query_one("#host_id", Input).value == ""
        assert app.screen.query_one("#backend_type", Select).value == "lmstudio"


async def test_a_blank_register_modal_suggests_the_default_backend_types_base_url():
    client = _FakeClient(nodes={"nodes": []})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.click("#new-node-button")
        await pilot.pause()

        assert app.screen.query_one("#backend_type", Select).value == "lmstudio"
        assert app.screen.query_one("#base_url", Input).value == "http://localhost:1234"


async def test_changing_backend_type_updates_the_suggested_base_url():
    client = _FakeClient(nodes={"nodes": []})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.click("#new-node-button")
        await pilot.pause()

        app.screen.query_one("#backend_type", Select).value = "llamaserver"
        await pilot.pause()

        assert app.screen.query_one("#base_url", Input).value == "http://localhost:8080"


async def test_changing_backend_type_does_not_overwrite_a_typed_base_url():
    client = _FakeClient(nodes={"nodes": []})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.click("#new-node-button")
        await pilot.pause()

        app.screen.query_one("#base_url", Input).value = "http://gpu-box.home:9000"
        app.screen.query_one("#backend_type", Select).value = "llamaserver"
        await pilot.pause()

        assert app.screen.query_one("#base_url", Input).value == "http://gpu-box.home:9000"


async def test_editing_an_existing_node_does_not_overwrite_its_real_base_url():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        app.screen.query_one("#backend_type", Select).value = "llamaserver"
        await pilot.pause()

        assert app.screen.query_one("#base_url", Input).value == "http://localhost:1234"


async def test_pressing_n_opens_the_register_modal():
    client = _FakeClient(nodes={"nodes": []})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("n")
        await pilot.pause()

        assert isinstance(app.screen, NodeFormScreen)


async def test_registering_a_new_node_sends_the_collected_fields():
    client = _FakeClient(nodes={"nodes": []})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("n")
        await pilot.pause()

        app.screen.query_one("#host_id", Input).value = "new-host"
        app.screen.query_one("#context_window", Input).value = "8192"
        app.screen.query_one("#max_concurrent_requests", Input).value = "4"
        app.screen.query_one("#base_url", Input).value = "http://new:1234"

        await pilot.click("#save")
        await pilot.pause()

        assert client.register_node_calls == [
            {
                "backend_type": "lmstudio",
                "context_window": 8192,
                "max_concurrent_requests": 4,
                "base_url": "http://new:1234",
                "host_id": "new-host",
                "allowed_models": None,
                "memory_budget_gb": None,
                "model_sizes_gb": None,
                "model_aliases": None,
                "model_limits": None,
            }
        ]
        assert not isinstance(app.screen, NodeFormScreen)


async def test_registering_without_a_host_id_shows_an_error():
    client = _FakeClient(nodes={"nodes": []})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("n")
        await pilot.pause()

        app.screen.query_one("#context_window", Input).value = "8192"
        app.screen.query_one("#max_concurrent_requests", Input).value = "4"
        app.screen.query_one("#base_url", Input).value = "http://new:1234"

        await pilot.click("#save")
        await pilot.pause()

        assert isinstance(app.screen, NodeFormScreen)
        assert client.register_node_calls == []
        error = app.screen.query_one("#edit-error", Static)
        assert "host_id" in str(error.render())


async def test_pressing_x_opens_a_remove_confirm_for_the_selected_node():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("x")
        await pilot.pause()

        assert isinstance(app.screen, RemoveNodeConfirmScreen)


async def test_cancelling_the_remove_confirm_sends_no_deregister():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("x")
        await pilot.pause()

        await pilot.click("#cancel")
        await pilot.pause()

        assert client.deregister_node_calls == []
        assert not isinstance(app.screen, RemoveNodeConfirmScreen)


async def test_confirming_the_remove_dialog_deregisters_the_selected_node_and_refreshes():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        await pilot.press("x")
        await pilot.pause()

        await pilot.click("#confirm")
        await pilot.pause()

        assert client.deregister_node_calls == ["host-a"]
        assert not isinstance(app.screen, RemoveNodeConfirmScreen)


async def test_pressing_x_with_no_nodes_registered_does_nothing():
    client = _FakeClient(nodes={"nodes": []})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()

        await pilot.press("x")
        await pilot.pause()

        assert not isinstance(app.screen, RemoveNodeConfirmScreen)


async def test_clicking_remove_node_button_operates_on_the_selected_row():
    client = _FakeClient(nodes={"nodes": [_full_node("host-a"), _full_node("host-b")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()
        table = app.query_one("#nodes-table", DataTable)
        table.move_cursor(row=1)
        await pilot.click("#remove-node-button")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()

        assert client.deregister_node_calls == ["host-b"]


class _OrderingFakeClient(_FakeClient):
    """Mimics the orchestrator: PATCHed positions are stored and drive /v1/nodes ordering."""

    async def update_node(self, host_id, fields):
        await super().update_node(host_id, fields)
        for node in self._nodes["nodes"]:
            if node["host_id"] == host_id:
                node["position"] = fields["position"]

    async def list_nodes(self):
        nodes = self._nodes["nodes"]
        ordered = sorted(nodes, key=lambda n: (n.get("position") is None, n.get("position") or 0))
        return {"nodes": ordered}


def _table_host_ids(app):
    table = app.query_one("#nodes-table", DataTable)
    return [str(table.get_row_at(i)[0]) for i in range(table.row_count)]


def _sparkline_labels(app):
    return [
        str(label.render()).split(" · ")[0]
        for label in app.query("#load-sparklines .node-load-group Label")
    ]


def _three_nodes():
    return {
        "nodes": [
            _node("host-b", in_flight=0, status="offline"),
            _node("host-c", in_flight=0, status="online"),
            _node("host-a", in_flight=0, status="unknown"),
        ]
    }


async def test_the_default_view_keeps_the_order_the_server_returned():
    app = DashboardApp(client=_FakeClient(nodes=_three_nodes()), interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        await pilot.pause()

        assert _table_host_ids(app) == ["host-b", "host-c", "host-a"]


async def test_pressing_s_cycles_the_sort_column_and_r_toggles_the_direction():
    app = DashboardApp(client=_FakeClient(nodes=_three_nodes()), interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        await pilot.press("s")
        await pilot.pause()
        by_node = _table_host_ids(app)
        await pilot.press("r")
        await pilot.pause()
        by_node_desc = _table_host_ids(app)
        await pilot.press("s")
        await pilot.pause()
        by_status_desc = _table_host_ids(app)

        assert by_node == ["host-a", "host-b", "host-c"]
        assert by_node_desc == ["host-c", "host-b", "host-a"]
        assert by_status_desc == ["host-a", "host-c", "host-b"]
        title = app.query_one("#nodes-table", DataTable).border_title
        assert "status" in title and "↓" in title


async def test_cycling_past_the_last_sort_column_returns_to_the_manual_order():
    app = DashboardApp(client=_FakeClient(nodes=_three_nodes()), interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        for _ in range(5):
            await pilot.press("s")
        await pilot.pause()
        assert "tokens" in app.query_one("#nodes-table", DataTable).border_title

        await pilot.press("s")
        await pilot.pause()

        assert _table_host_ids(app) == ["host-b", "host-c", "host-a"]
        assert app.query_one("#nodes-table", DataTable).border_title == "Nodes"


async def test_pressing_o_clears_the_sort_and_restores_the_manual_order():
    app = DashboardApp(client=_FakeClient(nodes=_three_nodes()), interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        await pilot.press("s", "r")
        await pilot.pause()

        await pilot.press("o")
        await pilot.pause()

        assert _table_host_ids(app) == ["host-b", "host-c", "host-a"]


async def test_sorting_by_reqs_per_min_puts_the_busiest_first_and_unknown_rates_last():
    clock_values = iter([T0, T0 + timedelta(seconds=60)])
    client = _FakeClient(
        nodes={"nodes": [_node("slow", 0), _node("fast", 0), _node("idle", 0)]},
        metrics_text=(
            'llm_home_lab_host_completions_total{host_id="slow"} 0\n'
            'llm_home_lab_host_completions_total{host_id="fast"} 0\n'
        ),
    )
    app = DashboardApp(client=client, interval_s=100.0, clock=lambda: next(clock_values))

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        client._metrics_text = (
            'llm_home_lab_host_completions_total{host_id="slow"} 1\n'
            'llm_home_lab_host_completions_total{host_id="fast"} 9\n'
        )
        app._sort_column = "reqs/min"
        app._sort_descending = True
        await app.poll()
        await pilot.pause()

        assert _table_host_ids(app)[:2] == ["fast", "slow"]


async def test_sorting_by_avg_latency_orders_by_the_parsed_average():
    client = _FakeClient(
        nodes={"nodes": [_node("a", 0), _node("b", 0)]},
        metrics_text=(
            'llm_home_lab_host_latency_ms_avg{host_id="a"} 900\n'
            'llm_home_lab_host_latency_ms_avg{host_id="b"} 100\n'
        ),
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        app._sort_column = "avg latency"
        await app.poll()
        await pilot.pause()

        assert _table_host_ids(app) == ["b", "a"]


async def test_the_load_sparklines_follow_the_displayed_order():
    app = DashboardApp(client=_FakeClient(nodes=_three_nodes()), interval_s=100.0)

    async with app.run_test(size=(120, 60)) as pilot:
        await app.poll()
        await pilot.pause()
        assert _sparkline_labels(app) == ["host-b", "host-c", "host-a"]

        await pilot.press("s")
        await pilot.pause()

        assert _sparkline_labels(app) == ["host-a", "host-b", "host-c"]


async def test_pressing_d_moves_the_selected_node_down_and_persists_positions():
    client = _OrderingFakeClient(nodes=_three_nodes())
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 60)) as pilot:
        await app.poll()
        await pilot.press("d")
        await pilot.pause()

        assert _table_host_ids(app) == ["host-c", "host-b", "host-a"]
        assert client.update_node_calls == [
            ("host-c", {"position": 0}),
            ("host-b", {"position": 1}),
            ("host-a", {"position": 2}),
        ]
        assert app.query_one("#nodes-table", DataTable).cursor_row == 1
        assert _sparkline_labels(app) == ["host-c", "host-b", "host-a"]


async def test_pressing_u_moves_the_selected_node_up_and_the_cursor_follows_it():
    client = _OrderingFakeClient(nodes=_three_nodes())
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 60)) as pilot:
        await app.poll()
        await pilot.press("down", "down", "u")
        await pilot.pause()

        assert _table_host_ids(app) == ["host-b", "host-a", "host-c"]
        assert app.query_one("#nodes-table", DataTable).cursor_row == 1


async def test_moving_past_either_end_changes_nothing():
    client = _OrderingFakeClient(nodes=_three_nodes())
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 60)) as pilot:
        await app.poll()

        await pilot.press("u")
        await pilot.pause()

        assert client.update_node_calls == []
        assert _table_host_ids(app) == ["host-b", "host-c", "host-a"]


async def test_moving_a_node_while_a_column_sort_is_active_does_nothing():
    client = _OrderingFakeClient(nodes=_three_nodes())
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 60)) as pilot:
        await app.poll()
        await pilot.press("s")

        await pilot.press("d")
        await pilot.pause()

        assert client.update_node_calls == []


async def test_a_moved_order_is_what_a_fresh_dashboard_sees_after_the_server_reorders():
    client = _OrderingFakeClient(nodes=_three_nodes())
    first = DashboardApp(client=client, interval_s=100.0)
    async with first.run_test(size=(120, 60)) as pilot:
        await first.poll()
        await pilot.press("d")
        await pilot.pause()

    second = DashboardApp(client=client, interval_s=100.0)
    async with second.run_test(size=(120, 60)):
        await second.poll()

        assert _table_host_ids(second) == ["host-c", "host-b", "host-a"]


async def test_nodes_without_a_position_field_still_render_in_server_order():
    nodes = {"nodes": [_node("host-a", 0), _node("host-b", 0)]}
    assert all("position" not in n for n in nodes["nodes"])
    client = _OrderingFakeClient(nodes=nodes)
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        await pilot.pause()

        assert _table_host_ids(app) == ["host-a", "host-b"]


def _chart_lines(app, host_id):
    chart = app.query_one(f"#load-{host_id}", Sparkline)
    return [chart.render_line(y).text.rstrip() for y in range(chart.size.height)]


async def _poll_n(app, pilot, count):
    for _ in range(count):
        await app.poll()
    await pilot.pause()


async def test_a_saturated_node_load_chart_is_four_full_rows_not_a_flat_line():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=8, max_concurrent_requests=8)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await _poll_n(app, pilot, 3)

        assert _chart_lines(app, "host-a") == ["███"] * 4


async def test_an_idle_node_load_chart_shows_only_a_baseline():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=0, max_concurrent_requests=5)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await _poll_n(app, pilot, 2)

        assert _chart_lines(app, "host-a") == ["", "", "", "▁▁"]


async def test_nodes_with_the_same_load_history_render_identical_charts():
    client = _FakeClient(
        nodes={
            "nodes": [
                _node("host-a", in_flight=8, max_concurrent_requests=8),
                _node("host-b", in_flight=4, max_concurrent_requests=4),
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await _poll_n(app, pilot, 3)

        assert _chart_lines(app, "host-a") == _chart_lines(app, "host-b")


async def test_a_small_dip_from_full_load_stays_visually_small():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=8, max_concurrent_requests=8)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await _poll_n(app, pilot, 3)
        client._nodes = {"nodes": [_node("host-a", in_flight=7, max_concurrent_requests=8)]}
        await _poll_n(app, pilot, 1)

        lines = _chart_lines(app, "host-a")
        assert [line[-1] for line in lines] == ["▄", "█", "█", "█"]
        assert {line[:3] for line in lines} == {"███"}


async def test_the_exported_screenshot_shows_green_orange_and_red_bars(monkeypatch):
    _enable_screenshot_colour(monkeypatch)
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=8, max_concurrent_requests=8)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        for in_flight in (8, 5, 1):
            client._nodes = {
                "nodes": [_node("host-a", in_flight=in_flight, max_concurrent_requests=8)]
            }
            await _poll_n(app, pilot, 1)

        svg = app.export_screenshot()

    for style in (GREEN, ORANGE, RED):
        assert style.color.get_truecolor().hex in svg


async def test_the_load_label_shows_host_counter_and_rounded_percent():
    client = _FakeClient(
        nodes={
            "nodes": [
                _node("host-a", in_flight=1, max_concurrent_requests=3),
                _node("host-b", in_flight=8, max_concurrent_requests=8),
                _node("host-c", in_flight=0, max_concurrent_requests=0),
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        labels = [
            str(app.query_one(f"#label-{h}", Label).render())
            for h in ("host-a", "host-b", "host-c")
        ]
        assert labels == ["host-a · 1/3 · 33%", "host-b · 8/8 · 100%", "host-c · 0/0 · n/a"]


async def test_the_load_label_follows_the_next_poll():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=0)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()
        client._nodes = {"nodes": [_node("host-a", in_flight=2)]}

        await app.poll()

        assert str(app.query_one("#label-host-a", Label).render()) == "host-a · 2/4 · 50%"


def test_the_load_label_counter_uses_the_status_cell_colours():
    def counter_style(host):
        label = _load_label(host)
        counter = f"{host['in_flight']}/{host['max_concurrent_requests']}"
        return {label.plain[s.start : s.end]: str(s.style) for s in label.spans}.get(counter, "")

    assert counter_style(_node("busy", in_flight=2)) == "bold yellow"
    assert counter_style(_node("idle", in_flight=0)) == ""
    assert counter_style(_node("off", in_flight=0, status="offline")) == "dim"
    assert counter_style(_node("unk", in_flight=2, status="unknown")) == "dim"


def test_the_load_label_percent_uses_the_chart_colour_band():
    def percent_style(host):
        label = _load_label(host)
        return {label.plain[s.start : s.end]: s.style for s in label.spans}.get(
            label.plain.rsplit(" · ", 1)[1]
        )

    assert percent_style(_node("full", in_flight=4)) == GREEN
    assert percent_style(_node("half", in_flight=2)) == ORANGE
    assert percent_style(_node("low", in_flight=1)) == RED
    assert percent_style(_node("none", in_flight=0, max_concurrent_requests=0)) is None


async def test_dashboard_displays_effective_max_concurrent_requests_when_present():
    node = _node("mini-3", in_flight=2, max_concurrent_requests=4)
    node["effective_max_concurrent_requests"] = 8
    client = _FakeClient(nodes={"nodes": [node]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        await pilot.pause()

        table = app.query_one("#nodes-table", DataTable)
        status_cell = str(table.get_row_at(0)[1])
        label_text = str(app.query_one("#label-mini-3", Label).render())
        sparkline_data = list(app.query_one(Sparkline).data)

    assert "2/8" in status_cell
    assert label_text == "mini-3 · 2/8 · 25%"
    assert sparkline_data == [0.25]


async def test_nodes_table_replaces_models_column_with_child_rows_and_sums_additive_metrics(
    monkeypatch,
):
    _enable_screenshot_colour(monkeypatch)
    clock_values = iter([T0, T0 + timedelta(seconds=60)])
    router_two_loaded = {
        **_node("mini-1", in_flight=4, max_concurrent_requests=8),
        "backend_type": "llamaserver",
        "swap_mode": "router",
        "throughput": {
            "window_s": 3600.0,
            "tasks_per_hour": 180.0,
            "tasks_per_hour_per_slot": 18.0,
        },
        "models": [
            {
                "id": "gemma-e4b",
                "state": "loaded",
                "healthy": True,
                "recent_failures": 0,
                "in_flight": 3,
                "max_concurrent_requests": 8,
                "throughput": {"tasks_per_hour": 150.0, "tasks_per_hour_per_slot": 18.75},
            },
            {
                "id": "eurollm-22b",
                "state": "loaded",
                "healthy": True,
                "recent_failures": 0,
                "in_flight": 1,
                "max_concurrent_requests": 2,
                "throughput": {"tasks_per_hour": 30.0, "tasks_per_hour_per_slot": 15.0},
            },
            {
                "id": "unused-unloaded",
                "state": "unloaded",
                "healthy": True,
                "recent_failures": 0,
                "in_flight": 0,
                "max_concurrent_requests": 4,
            },
        ],
    }
    single_loaded_router = {
        **_node("mini-single", in_flight=2, max_concurrent_requests=8),
        "backend_type": "llamaserver",
        "swap_mode": "router",
        "models": [
            {
                "id": "gemma-e4b",
                "state": "loaded",
                "healthy": True,
                "recent_failures": 0,
                "in_flight": 2,
                "max_concurrent_requests": 8,
            }
        ],
    }
    plain_node = _node("cachy-1", in_flight=8, max_concurrent_requests=8)
    metrics_poll1 = (
        'llm_home_lab_host_completions_total{host_id="mini-1"} 100\n'
        'llm_home_lab_host_model_completions_total{host_id="mini-1",model="gemma-e4b"} 85\n'
        'llm_home_lab_host_model_completions_total{host_id="mini-1",model="eurollm-22b"} 15\n'
    )
    metrics_poll2 = (
        'llm_home_lab_host_completions_total{host_id="mini-1"} 140\n'
        'llm_home_lab_host_latency_ms_avg{host_id="mini-1"} 620.0\n'
        'llm_home_lab_host_prompt_tokens_avg{host_id="mini-1"} 310.0\n'
        'llm_home_lab_host_prompt_tokens_min{host_id="mini-1"} 80\n'
        'llm_home_lab_host_prompt_tokens_max{host_id="mini-1"} 920\n'
        'llm_home_lab_host_completion_tokens_avg{host_id="mini-1"} 64.0\n'
        'llm_home_lab_host_completion_tokens_min{host_id="mini-1"} 12\n'
        'llm_home_lab_host_completion_tokens_max{host_id="mini-1"} 210\n'
        'llm_home_lab_host_model_completions_total{host_id="mini-1",model="gemma-e4b"} 120\n'
        'llm_home_lab_host_model_latency_ms_avg{host_id="mini-1",model="gemma-e4b"} 480.0\n'
        'llm_home_lab_host_model_prompt_tokens_avg{host_id="mini-1",model="gemma-e4b"} 260.0\n'
        'llm_home_lab_host_model_prompt_tokens_min{host_id="mini-1",model="gemma-e4b"} 80\n'
        'llm_home_lab_host_model_prompt_tokens_max{host_id="mini-1",model="gemma-e4b"} 600\n'
        'llm_home_lab_host_model_completion_tokens_avg{host_id="mini-1",model="gemma-e4b"} 52.0\n'
        'llm_home_lab_host_model_completion_tokens_min{host_id="mini-1",model="gemma-e4b"} 12\n'
        'llm_home_lab_host_model_completion_tokens_max{host_id="mini-1",model="gemma-e4b"} 140\n'
        'llm_home_lab_host_model_completions_total{host_id="mini-1",model="eurollm-22b"} 20\n'
        'llm_home_lab_host_model_latency_ms_avg{host_id="mini-1",model="eurollm-22b"} 1460.0\n'
        'llm_home_lab_host_model_prompt_tokens_avg{host_id="mini-1",model="eurollm-22b"} 610.0\n'
        'llm_home_lab_host_model_prompt_tokens_min{host_id="mini-1",model="eurollm-22b"} 200\n'
        'llm_home_lab_host_model_prompt_tokens_max{host_id="mini-1",model="eurollm-22b"} 920\n'
        "llm_home_lab_host_model_completion_tokens_avg"
        '{host_id="mini-1",model="eurollm-22b"} 136.0\n'
        'llm_home_lab_host_model_completion_tokens_min{host_id="mini-1",model="eurollm-22b"} 40\n'
        'llm_home_lab_host_model_completion_tokens_max{host_id="mini-1",model="eurollm-22b"} 210\n'
    )
    client = _FakeClient(
        nodes={"nodes": [router_two_loaded, single_loaded_router, plain_node]},
        metrics_text=metrics_poll1,
    )
    app = DashboardApp(client=client, interval_s=100.0, clock=lambda: next(clock_values))

    async with app.run_test(size=(160, 40)) as pilot:
        await app.poll()
        client._metrics_text = metrics_poll2
        await app.poll()
        await pilot.pause()

        table = app.query_one("#nodes-table", DataTable)
        labels = [str(column.label) for column in table.ordered_columns]
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        svg = app.export_screenshot()
        assert str(app.query_one("#mlabel-mini-single_gemma-e4b", Label).render()) == (
            "  └─ gemma-e4b · 2/8 · 25%"
        )

    assert "models" not in labels
    assert len(labels) == 12
    assert len(rows) == 6

    summary, child_euro, child_gemma, single_row, single_child, plain_row = rows
    assert summary[0] == "mini-1"
    assert summary[1].plain == "online 4/10"
    assert summary[5] == "llamaserver"
    assert summary[4] == "18.0"
    assert child_euro[0] == ""
    assert child_gemma[0] == ""
    assert child_euro[5] == ""
    assert child_euro[6] == ""
    assert child_euro[1].plain == "├─ eurollm-22b 1/2"
    assert child_gemma[1].plain == "└─ gemma-e4b 3/8"
    assert _span_styles(child_euro[1])["eurollm-22b"] == "bold green"
    assert _span_styles(child_euro[1])["1/2"] == "bold yellow"
    assert _span_styles(child_gemma[1])["gemma-e4b"] == "bold green"
    assert _span_styles(child_gemma[1])["3/8"] == "bold yellow"

    # Additive metrics on Row 1 equal the sum of child model rows.
    assert float(summary[3]) == float(child_euro[3]) + float(child_gemma[3])
    assert int(summary[7]) == int(child_euro[7]) + int(child_gemma[7]) == 140
    assert float(summary[8].removesuffix("/min")) == (
        float(child_euro[8].removesuffix("/min")) + float(child_gemma[8].removesuffix("/min"))
    )
    assert summary[9] == "620"
    assert child_euro[9] == "1460"
    assert child_gemma[9] == "480"

    assert single_row[0] == "mini-single"
    assert single_child[1].plain == "└─ gemma-e4b 2/8"
    assert plain_row[0] == "cachy-1"
    assert "eurollm-22b" in svg.replace("&#160;", " ")
    assert "gemma-e4b" in svg.replace("&#160;", " ")


async def test_failed_model_stays_isolated_from_node_status_capacity_and_shows_node_errors():
    router_node = {
        **_node("mini-2", in_flight=3, max_concurrent_requests=8, recent_failures=2),
        "backend_type": "llamaserver",
        "swap_mode": "router",
        "models": [
            {
                "id": "gemma-e4b",
                "state": "loaded",
                "healthy": True,
                "recent_failures": 0,
                "in_flight": 3,
                "max_concurrent_requests": 8,
            },
            {
                "id": "hy-mt2",
                "state": "loading",
                "healthy": True,
                "recent_failures": 0,
                "in_flight": 0,
                "max_concurrent_requests": 6,
            },
            {
                "id": "qwen-14b",
                "state": "sleeping",
                "healthy": True,
                "recent_failures": 0,
                "in_flight": 0,
                "max_concurrent_requests": 1,
            },
            {
                "id": "broken-70b",
                "state": "failed",
                "healthy": False,
                "recent_failures": 3,
                "in_flight": 0,
                "max_concurrent_requests": 2,
            },
        ],
    }
    client = _FakeClient(nodes={"nodes": [router_node]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(160, 40)) as pilot:
        await app.poll()
        await pilot.pause()

        table = app.query_one("#nodes-table", DataTable)
        rows = [table.get_row_at(i) for i in range(table.row_count)]

    assert len(rows) == 5
    summary, row_loaded, row_loading, row_sleeping, row_failed = rows
    # Node remains online; capacity excludes failed model (8, not 10); unattributed errors explicit.
    assert summary[1].plain == "online 3/8"
    assert _span_styles(summary[1])["online"] == "bold green"
    assert summary[2].plain == "5 (2 node)"
    assert _span_styles(summary[2])["5"] == "bold red"

    assert row_loaded[1].plain == "├─ gemma-e4b 3/8"
    assert _span_styles(row_loading[1])["hy-mt2 (loading)"] == "bold yellow"
    assert _span_styles(row_sleeping[1])["qwen-14b (sleeping)"] == "cyan"
    assert row_failed[1].plain == "└─ broken-70b (failed) 0/2"
    assert _span_styles(row_failed[1])["broken-70b (failed)"] == "bold red"
    assert row_failed[2].plain == "3"


async def test_non_router_node_with_multiple_model_limits_shows_child_rows_and_single_stays_one():
    multi_limits = {
        **_node("mac-1", in_flight=1, max_concurrent_requests=8),
        "in_flight_by_model": {"gemma-e4b": 1},
        "model_limits": {
            "gemma-e4b": {"max_concurrent_requests": 8},
            "eurollm-22b": {"context_window": 4096},
        },
    }
    single_limit = {
        **_node("mac-2", in_flight=0, max_concurrent_requests=8),
        "model_limits": {"eurollm-22b": {"max_concurrent_requests": 4}},
    }
    client = _FakeClient(nodes={"nodes": [multi_limits, single_limit]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(160, 40)) as pilot:
        await app.poll()
        await pilot.pause()

        table = app.query_one("#nodes-table", DataTable)
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        assert str(app.query_one("#mlabel-mac-1_eurollm-22b", Label).render()) == (
            "  └─ eurollm-22b · 0/8 · 0%"
        )

    assert len(rows) == 4
    assert rows[0][0] == "mac-1"
    assert rows[1][0] == ""
    assert rows[1][1].plain == "├─ gemma-e4b 1/8"
    assert rows[2][0] == ""
    assert rows[2][1].plain == "└─ eurollm-22b 0/8"
    assert rows[3][0] == "mac-2"


async def test_selecting_a_child_model_row_targets_its_parent_node_for_edit_and_move():
    router_node = {
        **_full_node("mini-1", in_flight=4, max_concurrent_requests=8),
        "backend_type": "llamaserver",
        "swap_mode": "router",
        "models": [
            {
                "id": "gemma-e4b",
                "state": "loaded",
                "healthy": True,
                "recent_failures": 0,
                "in_flight": 3,
                "max_concurrent_requests": 8,
            },
            {
                "id": "eurollm-22b",
                "state": "loaded",
                "healthy": True,
                "recent_failures": 0,
                "in_flight": 1,
                "max_concurrent_requests": 2,
            },
        ],
    }
    plain_node = _full_node("cachy-1", in_flight=0, max_concurrent_requests=8)
    client = _OrderingFakeClient(nodes={"nodes": [router_node, plain_node]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(140, 40)) as pilot:
        await app.poll()
        await pilot.pause()

        table = app.query_one("#nodes-table", DataTable)
        # Cursor onto child row 1 (eurollm-22b under mini-1) and press 'e'.
        table.move_cursor(row=1)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, NodeFormScreen)
        assert app.screen._host_id == "mini-1"
        await pilot.press("escape")
        await pilot.pause()

        # Still on child row 1, press 'd' to move mini-1 down below cachy-1.
        table.move_cursor(row=1)
        await pilot.press("d")
        await pilot.pause()

        host_ids = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
    assert host_ids == ["cachy-1", "mini-1", "", ""]


async def test_node_load_shows_compact_two_row_per_model_subcharts_and_m_toggles(monkeypatch):
    _enable_screenshot_colour(monkeypatch)
    router_node = {
        **_node("mini-1", in_flight=5, max_concurrent_requests=8),
        "backend_type": "llamaserver",
        "swap_mode": "router",
        "models": [
            {
                "id": "gemma-e4b",
                "state": "loaded",
                "healthy": True,
                "recent_failures": 0,
                "in_flight": 3,
                "max_concurrent_requests": 8,
            },
            {
                "id": "eurollm-22b",
                "state": "loaded",
                "healthy": True,
                "recent_failures": 0,
                "in_flight": 2,
                "max_concurrent_requests": 2,
            },
        ],
    }
    plain_node = _node("cachy-1", in_flight=8, max_concurrent_requests=8)
    client = _FakeClient(nodes={"nodes": [router_node, plain_node]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(140, 45)) as pilot:
        await app.poll()
        await pilot.pause()

        subcharts = list(app.query(".model-load-chart"))
        assert len(subcharts) == 2
        assert all(chart.size.height == 2 for chart in subcharts)
        assert str(app.query_one("#label-mini-1", Label).render()) == "mini-1 · 5/10 · 50%"
        sub_labels = [str(lbl.render()) for lbl in app.query(".model-load-label")]
        assert sub_labels == [
            "  ├─ eurollm-22b · 2/2 · 100%",
            "  └─ gemma-e4b · 3/8 · 38%",
        ]
        svg = app.export_screenshot()

        await pilot.press("m")
        await pilot.pause()
        assert len(app.query(".model-load-chart")) == 0

        await pilot.press("m")
        await pilot.pause()
        assert len(app.query(".model-load-chart")) == 2

    assert GREEN.color.get_truecolor().hex in svg
    assert ORANGE.color.get_truecolor().hex in svg


async def test_editing_prefills_model_limits_for_allowed_models_and_existing_limits():
    client = _FakeClient(
        nodes={
            "nodes": [
                _full_node(
                    "mini-1",
                    max_concurrent_requests=8,
                    allowed_models=["gemma-e4b", "eurollm-22b"],
                    model_limits={
                        "eurollm-22b": {"max_concurrent_requests": 4, "context_window": 16384},
                        "hy-mt2": {"max_concurrent_requests": 6},
                    },
                )
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        limits = app.screen.query_one("#model-limits-rows")
        assert [w.value for w in limits.query(".limit-model")] == [
            "gemma-e4b",
            "eurollm-22b",
            "hy-mt2",
        ]
        assert [w.value for w in limits.query(".limit-max-concurrent")] == ["", "4", "6"]
        assert [w.value for w in limits.query(".limit-context-window")] == ["", "16384", ""]


async def test_editing_with_null_model_limits_saves_only_non_blank_overrides():
    client = _FakeClient(
        nodes={
            "nodes": [
                _full_node(
                    "mini-1",
                    max_concurrent_requests=8,
                    allowed_models=["gemma-e4b", "eurollm-22b"],
                    model_limits=None,
                )
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        limits = app.screen.query_one("#model-limits-rows")
        slots_inputs = list(limits.query(".limit-max-concurrent"))
        ctx_inputs = list(limits.query(".limit-context-window"))
        assert [w.value for w in limits.query(".limit-model")] == ["gemma-e4b", "eurollm-22b"]
        slots_inputs[1].value = "4"
        ctx_inputs[1].value = "16384"

        await pilot.click("#save")
        await pilot.pause()

        _, fields = client.update_node_calls[0]
        assert fields["model_limits"] == {
            "eurollm-22b": {"max_concurrent_requests": 4, "context_window": 16384}
        }


async def test_clearing_all_model_limits_saves_null():
    client = _FakeClient(
        nodes={
            "nodes": [
                _full_node(
                    "mini-1",
                    allowed_models=["eurollm-22b"],
                    model_limits={"eurollm-22b": {"max_concurrent_requests": 4}},
                )
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        limits = app.screen.query_one("#model-limits-rows")
        list(limits.query(".limit-max-concurrent"))[0].value = ""

        await pilot.click("#save")
        await pilot.pause()

        assert client.update_node_calls[0][1]["model_limits"] is None


async def test_adding_and_removing_model_limit_rows_updates_saved_payload():
    client = _FakeClient(nodes={"nodes": [_full_node("mini-1", model_limits=None)]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        app.screen.query_one("#add-limit", Button).press()
        app.screen.query_one("#add-limit", Button).press()
        await pilot.pause()

        limits = app.screen.query_one("#model-limits-rows")
        models = list(limits.query(".limit-model"))
        slots = list(limits.query(".limit-max-concurrent"))
        ctxs = list(limits.query(".limit-context-window"))
        models[0].value = "drop-me"
        slots[0].value = "2"
        models[1].value = "eurollm-22b"
        slots[1].value = "4"
        ctxs[1].value = "16384"

        list(limits.query(".mapping-remove"))[0].press()
        await pilot.pause()

        await pilot.click("#save")
        await pilot.pause()

        assert client.update_node_calls[0][1]["model_limits"] == {
            "eurollm-22b": {"max_concurrent_requests": 4, "context_window": 16384}
        }


async def test_invalid_model_limits_keep_modal_open_with_clear_error(monkeypatch):
    _enable_screenshot_colour(monkeypatch)
    client = _FakeClient(
        nodes={
            "nodes": [
                _full_node(
                    "mini-1",
                    allowed_models=["eurollm-22b"],
                    model_limits={"eurollm-22b": {"max_concurrent_requests": 4}},
                )
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(120, 40)) as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        limits = app.screen.query_one("#model-limits-rows")
        slots_input = list(limits.query(".limit-max-concurrent"))[0]
        ctx_input = list(limits.query(".limit-context-window"))[0]
        save_button = app.screen.query_one("#save", Button)

        for bad_slots in ("0", "-3", "four"):
            slots_input.value = bad_slots
            save_button.press()
            await pilot.pause()
            assert isinstance(app.screen, NodeFormScreen)
            assert client.update_node_calls == []
            assert "positive whole number" in str(
                app.screen.query_one("#edit-error", Static).render()
            )

        slots_input.value = "4"
        for bad_ctx in ("0", "-1024", "16k"):
            ctx_input.value = bad_ctx
            save_button.press()
            await pilot.pause()
            assert isinstance(app.screen, NodeFormScreen)
            assert client.update_node_calls == []
            assert "context_window" in str(app.screen.query_one("#edit-error", Static).render())

        ctx_input.value = ""
        app.screen.query_one("#add-limit", Button).press()
        await pilot.pause()
        list(limits.query(".limit-model"))[1].value = "eurollm-22b"
        list(limits.query(".limit-max-concurrent"))[1].value = "2"
        save_button.press()
        await pilot.pause()
        assert isinstance(app.screen, NodeFormScreen)
        assert "duplicate" in str(app.screen.query_one("#edit-error", Static).render())

        list(limits.query(".limit-model"))[1].value = ""
        save_button.press()
        await pilot.pause()
        assert isinstance(app.screen, NodeFormScreen)
        assert "missing a model name" in str(app.screen.query_one("#edit-error", Static).render())

        svg = app.export_screenshot()
        svg_text = svg.replace("&#160;", " ")
        assert "eurollm-22b" in svg_text
        assert "missing a model name" in svg_text


async def test_node_edit_dialog_fits_without_scrolling_at_typical_terminal_sizes():
    node = _full_node(
        "mini-1",
        backend_type="llamaserver",
        max_concurrent_requests=8,
        allowed_models=["gemma-e4b", "eurollm-22b", "hy-mt2"],
        model_limits={
            "eurollm-22b": {"max_concurrent_requests": 4, "context_window": 16384},
            "hy-mt2": {"max_concurrent_requests": 6},
        },
    )

    for size in ((100, 24), (120, 30), (120, 40)):
        client = _FakeClient(nodes={"nodes": [node]})
        app = DashboardApp(client=client, interval_s=100.0)
        async with app.run_test(size=size) as pilot:
            await app.poll()
            await pilot.press("e")
            await pilot.pause()

            form_body = app.screen.query_one("#form-body")
            save_button = app.screen.query_one("#save", Button)
            assert form_body.max_scroll_y == 0, f"unexpected scroll at {size}"
            assert save_button.region.bottom <= size[1], f"save button clipped at {size}"


async def test_node_edit_dialog_falls_back_to_scrolling_when_terminal_is_too_small():
    node = _full_node(
        "mini-1",
        backend_type="llamaserver",
        max_concurrent_requests=8,
        allowed_models=["gemma-e4b", "eurollm-22b", "hy-mt2"],
        model_limits={
            "eurollm-22b": {"max_concurrent_requests": 4, "context_window": 16384},
            "hy-mt2": {"max_concurrent_requests": 6},
        },
    )
    client = _FakeClient(nodes={"nodes": [node]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test(size=(100, 14)) as pilot:
        await app.poll()
        await pilot.press("e")
        await pilot.pause()

        form_body = app.screen.query_one("#form-body")
        save_button = app.screen.query_one("#save", Button)
        assert form_body.max_scroll_y > 0
        assert save_button.region.bottom <= 14
        await pilot.click("#save")
        await pilot.pause()

    assert len(client.update_node_calls) == 1


async def test_router_capacity_counts_only_healthy_loaded_models():
    loading = {
        **_node("loading", in_flight=1, max_concurrent_requests=8),
        "models": [
            {"id": "ready", "state": "loaded", "max_concurrent_requests": 2},
            {"id": "pending", "state": "loading", "max_concurrent_requests": 4},
        ],
    }
    failed = {
        **_node("failed", in_flight=0, max_concurrent_requests=8),
        "models": [{"id": "broken", "state": "failed", "healthy": False}],
    }
    app = DashboardApp(client=_FakeClient(nodes={"nodes": [loading, failed]}), interval_s=100.0)

    async with app.run_test(size=(160, 40)):
        await app.poll()
        table = app.query_one("#nodes-table", DataTable)
        assert table.get_row_at(0)[1].plain == "online 1/2"
        assert table.get_row_at(3)[1].plain == "online 0/0"
        assert str(app.query_one("#label-loading", Label).render()) == "loading · 1/2 · 50%"
        assert str(app.query_one("#label-failed", Label).render()) == "failed · 0/0 · n/a"
        assert list(app.query_one("#load-loading", Sparkline).data) == [0.5]
        assert list(app.query_one("#load-failed", Sparkline).data) == [0.0]


async def test_router_model_charts_follow_activity_and_remove_idle_models():
    node = {
        **_node("router"),
        "models": [
            {"id": "ready", "state": "loaded"},
            {"id": "pending", "state": "loading"},
            {"id": "sleeping", "state": "sleeping"},
            {"id": "broken", "state": "failed", "healthy": False, "in_flight": 1},
            {
                "id": "traffic",
                "state": "failed",
                "healthy": False,
                "throughput": {"tasks_per_hour": 2},
            },
            {"id": "idle-failed", "state": "failed", "healthy": False},
        ],
    }
    client = _FakeClient(nodes={"nodes": [node]})
    app = DashboardApp(client=client, interval_s=100.0)
    async with app.run_test(size=(160, 40)):
        await app.poll()
        assert {
            str(label.render()).split(" · ")[0].strip(" ├└─")
            for label in app.query(".model-load-label")
        } == {"ready", "broken", "traffic"}
        node["models"][3]["in_flight"] = 0
        node["models"][4]["throughput"]["tasks_per_hour"] = 0
        node["models"][1]["in_flight"] = 1
        await app.poll()
        assert {
            str(label.render()).split(" · ")[0].strip(" ├└─")
            for label in app.query(".model-load-label")
        } == {"ready", "pending"}


async def test_empty_router_state_capacity_and_unavailable_fallback():
    empty = {
        **_node("empty", max_concurrent_requests=8),
        "models": [],
        "throughput": {"tasks_per_hour": 10},
    }
    unavailable = {
        **_node("unavailable", max_concurrent_requests=8),
        "models": None,
        "throughput": {"tasks_per_hour": 16},
    }
    app = DashboardApp(client=_FakeClient(nodes={"nodes": [empty, unavailable]}), interval_s=100.0)
    async with app.run_test(size=(160, 40)):
        await app.poll()
        table = app.query_one("#nodes-table", DataTable)
        assert table.get_row_at(0)[1].plain == "online 0/0"
        assert table.get_row_at(0)[4] == "n/a"
        assert table.get_row_at(1)[1].plain == "online 0/8"
        assert table.get_row_at(1)[4] == "2.0"
        assert str(app.query_one("#label-empty", Label).render()) == "empty · 0/0 · n/a"


async def test_selected_model_survives_refresh_and_falls_back_when_removed():
    node = {
        **_node("router"),
        "models": [{"id": "a", "state": "loaded"}, {"id": "b", "state": "loaded"}],
    }
    app = DashboardApp(client=_FakeClient(nodes={"nodes": [node]}), interval_s=100.0)
    async with app.run_test(size=(160, 40)):
        await app.poll()
        table = app.query_one("#nodes-table", DataTable)
        table.move_cursor(row=2)
        node["models"][1]["state"] = "loading"
        await app.poll()
        assert table.get_row_at(table.cursor_row)[1].plain.startswith("└─ b")
        node["models"].pop()
        await app.poll()
        assert table.cursor_row == 0


async def test_non_router_breakdown_includes_all_observed_models_and_charts():
    node = {
        **_node("host", in_flight=3, max_concurrent_requests=8),
        "model_limits": {"a": {"max_concurrent_requests": 2}, "b": {"max_concurrent_requests": 3}},
        "in_flight_by_model": {"a": 1, "b": 1, "c": 1},
        "model_throughput": {"c": {"tasks_per_hour": 1}},
    }
    metrics = (
        'llm_home_lab_host_completions_total{host_id="host"} 6\n'
        'llm_home_lab_host_model_completions_total{host_id="host",model="a"} 1\n'
        'llm_home_lab_host_model_completions_total{host_id="host",model="b"} 2\n'
        'llm_home_lab_host_model_completions_total{host_id="host",model="c"} 3\n'
    )
    app = DashboardApp(
        client=_FakeClient(nodes={"nodes": [node]}, metrics_text=metrics), interval_s=100.0
    )
    async with app.run_test(size=(160, 40)):
        await app.poll()
        table = app.query_one("#nodes-table", DataTable)
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        assert len(rows) == 4
        assert rows[3][1].plain == "└─ c 1/8"
        assert sum(int(row[7]) for row in rows[1:]) == int(rows[0][7]) == 6
        assert str(app.query_one("#mlabel-host_c", Label).render()) == "  └─ c · 1/8 · 12%"
        node["in_flight_by_model"].pop("c")
        node["model_throughput"].pop("c")
        await app.poll()
        assert table.row_count == 4
        assert str(app.query_one("#mlabel-host_c", Label).render()) == "  └─ c · 0/8 · 0%"
