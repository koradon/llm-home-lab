import asyncio
from datetime import UTC, datetime, timedelta

from textual.widgets import Button, DataTable, Input, Select, Sparkline, Static

from llm_home_lab.tui.app import DashboardApp, NodeFormScreen, RemoveNodeConfirmScreen
from llm_home_lab.tui.client import DiagnosticsClientError

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
    in_flight,
    max_concurrent_requests=4,
    status="online",
    recent_failures=None,
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
    return node


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
        assert group.size.height == 4


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
                    **_node("host-a", in_flight=0),
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
                    **_node("host-a", in_flight=0),
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
    return [str(label.render()) for label in app.query("#load-sparklines .node-load-group Label")]


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
