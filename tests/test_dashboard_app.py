import asyncio
from datetime import UTC, datetime, timedelta

from textual.widgets import DataTable, Input, Sparkline, Static

from llm_home_lab.tui.app import DashboardApp, NodeEditScreen
from llm_home_lab.tui.client import DiagnosticsClientError

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeClient:
    def __init__(self, nodes=None, alerts=None, metrics_text=None, error=None):
        self._nodes = nodes or {"nodes": []}
        self._alerts = alerts or {"alerts": []}
        self._metrics_text = metrics_text or ""
        self._error = error
        self.update_node_calls = []

    async def update_node(self, host_id, fields):
        self.update_node_calls.append((host_id, fields))

    async def list_nodes(self):
        if self._error:
            raise self._error
        return self._nodes

    async def list_alerts(self):
        if self._error:
            raise self._error
        return self._alerts

    async def fetch_metrics_text(self):
        if self._error:
            raise self._error
        return self._metrics_text

    async def trigger_health_check(self):
        if self._error:
            raise self._error


async def test_successful_poll_populates_all_three_panels():
    client = _FakeClient(
        nodes={
            "nodes": [
                {
                    "host_id": "host-a",
                    "backend_type": "lmstudio",
                    "in_flight": 1,
                    "max_concurrent_requests": 4,
                    "last_seen": "2026-07-19T00:00:00+00:00",
                    "status": "online",
                }
            ]
        },
        alerts={
            "alerts": [
                {
                    "rule_name": "latency",
                    "severity": "warning",
                    "state": "firing",
                    "value": 6000,
                    "threshold_value": 5000,
                    "runbook_url": "https://example/runbooks/latency",
                }
            ]
        },
        metrics_text=(
            'llm_home_lab_queue_depth 3\nllm_home_lab_token_usage_total{host_id="host-a"} 150\n'
        ),
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        nodes_table = app.query_one("#nodes-table", DataTable)
        alerts_table = app.query_one("#alerts-table", DataTable)
        queue_table = app.query_one("#queue-tokens-table", DataTable)

        assert nodes_table.row_count == 1
        assert alerts_table.row_count == 1
        assert queue_table.row_count == 4


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


async def test_missing_metric_renders_unavailable_without_affecting_other_panels():
    client = _FakeClient(
        nodes={
            "nodes": [
                {
                    "host_id": "host-a",
                    "backend_type": "lmstudio",
                    "in_flight": 0,
                    "max_concurrent_requests": 4,
                    "last_seen": "2026-07-19T00:00:00+00:00",
                    "status": "online",
                }
            ]
        },
        alerts={"alerts": []},
        metrics_text="",
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        nodes_table = app.query_one("#nodes-table", DataTable)
        queue_table = app.query_one("#queue-tokens-table", DataTable)

        assert nodes_table.row_count == 1
        assert queue_table.get_row_at(0) == ["queue_depth", "unavailable"]


async def test_critical_alert_severity_is_styled_red():
    client = _FakeClient(
        alerts={
            "alerts": [
                {
                    "rule_name": "availability",
                    "severity": "critical",
                    "state": "firing",
                    "value": 0.5,
                    "threshold_value": 0.99,
                    "runbook_url": "https://example/runbooks/availability",
                }
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#alerts-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[1].key
        severity_cell = table.get_cell(row_key, column_key)
        assert "red" in severity_cell.style


async def test_p95_latency_is_rendered_from_parsed_metrics():
    client = _FakeClient(
        metrics_text="llm_home_lab_queue_depth 0\nllm_home_lab_request_latency_p95_ms 123.5\n"
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#queue-tokens-table", DataTable)
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        assert ["p95_latency_ms", "124"] in rows


async def test_first_poll_shows_no_token_rate_yet():
    client = _FakeClient(metrics_text='llm_home_lab_token_usage_total{host_id="host-a"} 100\n')
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#queue-tokens-table", DataTable)
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        assert ["tokens/s[host-a]", "-"] in rows


def _node(host_id, in_flight, max_concurrent_requests=4, status="online", external_load=None):
    return {
        "host_id": host_id,
        "backend_type": "lmstudio",
        "in_flight": in_flight,
        "max_concurrent_requests": max_concurrent_requests,
        "last_seen": "2026-07-19T00:00:00+00:00",
        "status": status,
        "external_load": external_load,
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
        assert group.size.height == 7


async def test_a_busy_external_load_with_a_queue_backlog_combines_both_into_one_value():
    client = _FakeClient(
        nodes={
            "nodes": [
                _node(
                    "host-a",
                    in_flight=0,
                    external_load={"available": True, "status": "processingPrompt", "queued": 3},
                )
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        ext_load_sparkline = app.query_one("#extload-host-a", Sparkline)
        assert list(ext_load_sparkline.data) == [4.0]


async def test_a_busy_node_with_no_queue_backlog_still_shows_nonzero_external_load():
    client = _FakeClient(
        nodes={
            "nodes": [
                _node(
                    "host-a",
                    in_flight=0,
                    external_load={"available": True, "status": "generating", "queued": 0},
                )
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        ext_load_sparkline = app.query_one("#extload-host-a", Sparkline)
        assert list(ext_load_sparkline.data) == [1.0]


async def test_an_idle_external_load_is_charted_as_zero():
    client = _FakeClient(
        nodes={
            "nodes": [
                _node(
                    "host-a",
                    in_flight=0,
                    external_load={"available": True, "status": "idle", "queued": 0},
                )
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        ext_load_sparkline = app.query_one("#extload-host-a", Sparkline)
        assert list(ext_load_sparkline.data) == [0.0]


async def test_an_unavailable_external_load_is_charted_as_zero():
    client = _FakeClient(
        nodes={"nodes": [_node("host-a", in_flight=0, external_load={"available": False})]}
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        ext_load_sparkline = app.query_one("#extload-host-a", Sparkline)
        assert list(ext_load_sparkline.data) == [0.0]


async def test_a_second_poll_appends_to_the_same_external_load_sparkline():
    client = _FakeClient(
        nodes={
            "nodes": [
                _node(
                    "host-a",
                    in_flight=0,
                    external_load={"available": True, "status": "idle", "queued": 0},
                )
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()
        client._nodes = {
            "nodes": [
                _node(
                    "host-a",
                    in_flight=0,
                    external_load={"available": True, "status": "processingPrompt", "queued": 2},
                )
            ]
        }
        await app.poll()

        ext_load_sparkline = app.query_one("#extload-host-a", Sparkline)
        assert list(ext_load_sparkline.data) == [0.0, 3.0]


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
        assert len(sparklines) == 4
        ids = {s.id for s in sparklines}
        assert len(ids) == 4


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

        assert len(app.query(Sparkline)) == 6


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
        assert "red" in status_cell.style


async def test_an_unknown_node_status_is_styled_distinctly_from_online():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=0, status="unknown")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[1].key
        status_cell = table.get_cell(row_key, column_key)
        assert "yellow" in status_cell.style


async def test_an_online_node_status_has_no_alarming_style():
    client = _FakeClient(nodes={"nodes": [_node("host-a", in_flight=0, status="online")]})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[1].key
        status_cell = table.get_cell(row_key, column_key)
        assert "red" not in status_cell.style
        assert "yellow" not in status_cell.style


async def test_a_node_with_unavailable_external_load_is_styled_dim():
    client = _FakeClient(
        nodes={"nodes": [_node("host-a", in_flight=0, external_load={"available": False})]}
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[2].key
        ext_load_cell = table.get_cell(row_key, column_key)
        assert str(ext_load_cell) == "unavailable"
        assert "dim" in ext_load_cell.style


async def test_a_node_with_idle_external_load_has_no_alarming_style():
    client = _FakeClient(
        nodes={
            "nodes": [
                _node(
                    "host-a",
                    in_flight=0,
                    external_load={"available": True, "status": "idle", "queued": 0},
                )
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[2].key
        ext_load_cell = table.get_cell(row_key, column_key)
        assert str(ext_load_cell) == "idle"
        assert "yellow" not in ext_load_cell.style


async def test_a_node_with_busy_external_load_is_styled_yellow_with_queued_count():
    client = _FakeClient(
        nodes={
            "nodes": [
                _node(
                    "host-a",
                    in_flight=0,
                    external_load={"available": True, "status": "processingPrompt", "queued": 2},
                )
            ]
        }
    )
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test():
        await app.poll()

        table = app.query_one("#nodes-table", DataTable)
        row_key, _ = table.coordinate_to_cell_key((0, 0))
        column_key = table.ordered_columns[2].key
        ext_load_cell = table.get_cell(row_key, column_key)
        assert str(ext_load_cell) == "processingPrompt (2 queued)"
        assert "yellow" in ext_load_cell.style


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

        assert isinstance(app.screen, NodeEditScreen)
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
                    "context_window": 8192,
                    "max_concurrent_requests": 8,
                    "base_url": "http://localhost:1234",
                    "allowed_models": None,
                    "memory_budget_gb": None,
                },
            )
        ]
        assert not isinstance(app.screen, NodeEditScreen)


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
        assert not isinstance(app.screen, NodeEditScreen)


async def test_pressing_e_with_no_nodes_registered_does_nothing():
    client = _FakeClient(nodes={"nodes": []})
    app = DashboardApp(client=client, interval_s=100.0)

    async with app.run_test() as pilot:
        await app.poll()

        await pilot.press("e")
        await pilot.pause()

        assert not isinstance(app.screen, NodeEditScreen)


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

        assert isinstance(app.screen, NodeEditScreen)
        assert client.update_node_calls == []
        error = app.screen.query_one("#edit-error", Static)
        assert "whole numbers" in str(error.render())


async def test_second_poll_shows_token_rate_since_the_first():
    client = _FakeClient(metrics_text='llm_home_lab_token_usage_total{host_id="host-a"} 100\n')
    times = iter([T0, T0 + timedelta(seconds=5)])
    app = DashboardApp(client=client, interval_s=100.0, clock=lambda: next(times))

    async with app.run_test():
        await app.poll()
        client._metrics_text = 'llm_home_lab_token_usage_total{host_id="host-a"} 150\n'
        await app.poll()

        table = app.query_one("#queue-tokens-table", DataTable)
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        assert ["tokens/s[host-a]", "10.0/s"] in rows
