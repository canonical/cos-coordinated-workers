import dataclasses
from contextlib import ExitStack, contextmanager
from functools import partial
from unittest.mock import MagicMock, patch

import ops
import pytest
import tenacity
from ops import testing

from coordinated_workers.interfaces.cluster import ClusterProviderAppData
from coordinated_workers.worker import (
    NoReadinessCheckEndpointConfiguredError,
    Worker,
)


@pytest.fixture(params=[True, False])
def tls(request):
    return request.param


@contextmanager
def _urlopen_patch(url: str, resp: str, tls: bool):
    if url == f"{'https' if tls else 'http'}://localhost:3200/ready":
        mm = MagicMock()
        mm.read = MagicMock(return_value=resp.encode("utf-8"))
        yield mm
    else:
        raise RuntimeError("unknown path")


@contextmanager
def k8s_patch(status=ops.ActiveStatus(), is_ready=True):
    with patch("lightkube.core.client.GenericSyncClient"):
        with patch.multiple(
            "coordinated_workers.worker.KubernetesComputeResourcesPatch",
            _namespace="test-namespace",
            _patch=MagicMock(return_value=None),
            get_status=MagicMock(return_value=status),
            is_ready=MagicMock(return_value=is_ready),
        ) as patcher:
            yield patcher


@pytest.fixture(autouse=True)
def patch_status_wait():
    with ExitStack() as stack:
        # so we don't have to wait for minutes:
        stack.enter_context(
            patch(
                "coordinated_workers.worker.Worker.SERVICE_STATUS_UP_RETRY_WAIT",
                new=tenacity.wait_none(),
            )
        )
        stack.enter_context(
            patch(
                "coordinated_workers.worker.Worker.SERVICE_STATUS_UP_RETRY_STOP",
                new=tenacity.stop_after_delay(2),
            )
        )


@pytest.fixture
def ctx(tls):
    class MyCharm(ops.CharmBase):
        def __init__(self, framework: ops.Framework):
            super().__init__(framework)
            self.worker = Worker(
                self,
                "workload",
                lambda _: ops.pebble.Layer(
                    {
                        "summary": "summary",
                        "services": {"service": {"summary": "summary", "override": "replace"}},
                    }
                ),
                {"cluster": "cluster"},
                readiness_check_endpoint=self._readiness_check_endpoint,
                resources_requests=lambda _: {"cpu": "50m", "memory": "100Mi"},
                container_name="charm",
            )

        def _readiness_check_endpoint(self, _):
            return f"{'https' if tls else 'http'}://localhost:3200/ready"

    return testing.Context(
        MyCharm,
        meta={
            "name": "lilith",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"workload": {"type": "oci-image"}},
        },
        config={
            "options": {
                "role-all": {"type": "bool", "default": False},
                "role-read": {"type": "bool", "default": True},
                "role-write": {"type": "bool", "default": True},
            }
        },
    )


@pytest.fixture(params=[True, False])
def base_state(request):
    app_data = {}
    ClusterProviderAppData(worker_config="some: yaml").dump(app_data)
    return testing.State(
        leader=request.param,
        containers={
            testing.Container(
                "workload",
                can_connect=True,
                execs={testing.Exec(("update-ca-certificates", "--fresh"))},
            )
        },
        relations={testing.Relation("cluster", remote_app_data=app_data)},
    )


@contextmanager
def endpoint_starting(tls):
    with patch(
        "urllib.request.urlopen",
        new=partial(_urlopen_patch, tls=tls, resp="foo\nStarting: 10\n bar"),
    ):
        yield


@contextmanager
def endpoint_ready(tls):
    with patch("urllib.request.urlopen", new=partial(_urlopen_patch, tls=tls, resp="ready")):
        yield


@contextmanager
def config_on_disk():
    with patch("coordinated_workers.worker.Worker._running_worker_config", new=lambda _: True):
        yield


@k8s_patch()
def test_status_check_no_pebble(ctx, base_state, caplog):
    # GIVEN the container cannot connect
    state = dataclasses.replace(
        base_state, containers={testing.Container("workload", can_connect=False)}
    )

    # WHEN we run any event
    state_out = ctx.run(ctx.on.update_status(), state)

    # THEN the charm sets blocked
    assert state_out.unit_status == ops.WaitingStatus("Waiting for `workload` container")


@k8s_patch()
def test_status_check_no_config(ctx, base_state, caplog):
    # GIVEN there is no config file on disk
    # WHEN we run any event
    with patch("coordinated_workers.worker.Worker._running_worker_config", new=lambda _: None):
        state_out = ctx.run(ctx.on.update_status(), base_state)

    # THEN the charm sets blocked
    assert state_out.unit_status == ops.WaitingStatus(
        "Waiting for coordinator to publish a config"
    )
    # AND THEN the charm logs that the config isn't on disk
    assert "Config file not on disk. Skipping status check." in caplog.messages


@k8s_patch()
def test_status_check_starting(ctx, base_state, tls):
    # GIVEN getting the status returns "Starting: X"
    with endpoint_starting(tls):
        # AND GIVEN that the config is on disk
        with config_on_disk():
            # AND GIVEN that the container can connect (default in base_state)
            state = base_state
            # WHEN we run any event
            state_out = ctx.run(ctx.on.update_status(), state)
    # THEN the charm sets waiting: Starting...
    assert state_out.unit_status == ops.WaitingStatus("Starting...")


@k8s_patch()
def test_status_check_ready(ctx, base_state, tls):
    # GIVEN getting the status returns "ready"
    with endpoint_ready(tls):
        # AND GIVEN that the config is on disk
        with config_on_disk():
            # AND GIVEN that the container can connect
            state = base_state
            # WHEN we run any event
            state_out = ctx.run(ctx.on.update_status(), state)
    # THEN the charm sets waiting: Starting...
    assert state_out.unit_status == ops.ActiveStatus("read,write ready.")


def test_status_no_endpoint(ctx, base_state, caplog):
    # GIVEN a charm doesn't pass an endpoint to Worker
    class MyCharm(ops.CharmBase):
        def __init__(self, framework: ops.Framework):
            super().__init__(framework)
            self.worker = Worker(
                self,
                "workload",
                lambda _: ops.pebble.Layer({"services": {"foo": {"command": "foo"}}}),
                {"cluster": "cluster"},
            )

    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "damian",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"workload": {"type": "oci-image"}},
        },
        config={
            "options": {
                "role-all": {"type": "bool", "default": False},
                "role-read": {"type": "bool", "default": True},
                "role-write": {"type": "bool", "default": True},
            }
        },
    )
    # AND GIVEN that the container can connect
    state = base_state
    # WHEN we run any event
    state_out = ctx.run(ctx.on.update_status(), state)
    # THEN the charm sets Active: ready, even though we have no idea whether the endpoint is ready.
    assert state_out.unit_status == ops.ActiveStatus("read,write ready.")


def test_access_readiness_no_endpoint_raises():
    # GIVEN the caller doesn't pass an endpoint to Worker
    caller = MagicMock()
    with patch("cosl.juju_topology.JujuTopology.from_charm"):
        with patch("coordinated_workers.worker.Worker._reconcile"):
            worker = Worker(
                caller,
                "workload",
                lambda _: ops.pebble.Layer({"services": {"foo": {"command": "foo"}}}),
                {"cluster": "cluster"},
            )

    # THEN calling .check_readiness raises
    with pytest.raises(NoReadinessCheckEndpointConfiguredError):
        worker.check_readiness()  # noqa


def test_status_check_ready_with_patch(ctx, base_state, tls):
    with endpoint_ready(tls):
        with config_on_disk():
            with k8s_patch(status=ops.WaitingStatus("waiting")):
                state_out = ctx.run(ctx.on.config_changed(), base_state)
                assert state_out.unit_status == ops.WaitingStatus("waiting")
                with k8s_patch(status=ops.ActiveStatus("")):
                    state_out_out = ctx.run(ctx.on.update_status(), state_out)
                    assert state_out_out.unit_status == ops.ActiveStatus("read,write ready.")
