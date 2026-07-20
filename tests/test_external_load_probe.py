import asyncio
import json
from datetime import UTC, datetime, timedelta

from llm_home_lab.registry.external_load import ExternalLoadProbe

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 0, hang: bool = False):
        self._stdout = stdout
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(999)
        return self._stdout, b""

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _runner(process: _FakeProcess):
    async def create_subprocess(*args, **kwargs):
        return process

    return create_subprocess


async def test_a_loaded_model_reports_its_status_and_queued_count():
    entries = [{"status": "processingPrompt", "queued": 2}]
    process = _FakeProcess(stdout=json.dumps(entries).encode())
    probe = ExternalLoadProbe(create_subprocess=_runner(process))

    result = await probe.probe("host-a", "http://192.168.1.10:1234", at=T0)

    assert result.available is True
    assert result.status == "processingPrompt"
    assert result.queued == 2


async def test_no_loaded_models_reports_idle_not_unavailable():
    process = _FakeProcess(stdout=json.dumps([]).encode())
    probe = ExternalLoadProbe(create_subprocess=_runner(process))

    result = await probe.probe("host-a", "http://192.168.1.10:1234", at=T0)

    assert result.available is True
    assert result.status == "idle"
    assert result.queued == 0


async def test_multiple_models_sum_queued_and_prefer_non_idle_status():
    entries = [
        {"status": "idle", "queued": 0},
        {"status": "processingPrompt", "queued": 3},
    ]
    process = _FakeProcess(stdout=json.dumps(entries).encode())
    probe = ExternalLoadProbe(create_subprocess=_runner(process))

    result = await probe.probe("host-a", "http://192.168.1.10:1234", at=T0)

    assert result.status == "processingPrompt"
    assert result.queued == 3


async def test_missing_lms_binary_reports_unavailable():
    async def create_subprocess(*args, **kwargs):
        raise FileNotFoundError("lms not found")

    probe = ExternalLoadProbe(create_subprocess=create_subprocess)

    result = await probe.probe("host-a", "http://192.168.1.10:1234", at=T0)

    assert result.available is False
    assert result.status is None
    assert result.queued is None


async def test_non_zero_exit_reports_unavailable():
    process = _FakeProcess(stdout=b"", returncode=1)
    probe = ExternalLoadProbe(create_subprocess=_runner(process))

    result = await probe.probe("host-a", "http://192.168.1.10:1234", at=T0)

    assert result.available is False


async def test_unparseable_output_reports_unavailable():
    process = _FakeProcess(stdout=b"not json")
    probe = ExternalLoadProbe(create_subprocess=_runner(process))

    result = await probe.probe("host-a", "http://192.168.1.10:1234", at=T0)

    assert result.available is False


async def test_a_hung_process_times_out_and_is_killed():
    process = _FakeProcess(stdout=b"", hang=True)
    probe = ExternalLoadProbe(timeout_s=0.05, create_subprocess=_runner(process))

    result = await probe.probe("host-a", "http://192.168.1.10:1234", at=T0)

    assert result.available is False
    assert process.killed is True


async def test_a_cached_result_is_reused_within_the_ttl():
    call_count = 0
    entries = [{"status": "idle", "queued": 0}]

    async def create_subprocess(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _FakeProcess(stdout=json.dumps(entries).encode())

    probe = ExternalLoadProbe(cache_ttl=timedelta(seconds=10), create_subprocess=create_subprocess)

    await probe.probe("host-a", "http://192.168.1.10:1234", at=T0)
    await probe.probe("host-a", "http://192.168.1.10:1234", at=T0 + timedelta(seconds=5))

    assert call_count == 1


async def test_a_stale_cached_result_triggers_a_fresh_probe():
    call_count = 0
    entries = [{"status": "idle", "queued": 0}]

    async def create_subprocess(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _FakeProcess(stdout=json.dumps(entries).encode())

    probe = ExternalLoadProbe(cache_ttl=timedelta(seconds=10), create_subprocess=create_subprocess)

    await probe.probe("host-a", "http://192.168.1.10:1234", at=T0)
    await probe.probe("host-a", "http://192.168.1.10:1234", at=T0 + timedelta(seconds=11))

    assert call_count == 2


async def test_two_hosts_are_probed_independently():
    async def create_subprocess(*args, **kwargs):
        hostname = args[args.index("--host") + 1]
        if hostname == "192.168.1.10":
            return _FakeProcess(stdout=b"not json")
        return _FakeProcess(stdout=json.dumps([{"status": "idle", "queued": 0}]).encode())

    probe = ExternalLoadProbe(create_subprocess=create_subprocess)

    result_a = await probe.probe("host-a", "http://192.168.1.10:1234", at=T0)
    result_b = await probe.probe("host-b", "http://192.168.1.20:1234", at=T0)

    assert result_a.available is False
    assert result_b.available is True
