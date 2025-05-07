import dataclasses
from unittest.mock import MagicMock, PropertyMock, patch

import httpx
import ops
import pytest
import tenacity
from lightkube import ApiError
from ops import testing
from scenario import BlockedStatus
from scenario.context import CharmEvents

from coordinated_workers.coordinator import ClusterRolesConfig, Coordinator
from coordinated_workers.interfaces.cluster import ClusterProviderAppData, ClusterRequirerAppData
from coordinated_workers.nginx import NginxConfig
from tests.unit.test_worker_status import k8s_patch

my_roles = ClusterRolesConfig(
    roles={"role"},
    meta_roles={},
    minimal_deployment={"role": 1},
    recommended_deployment={"role": 2},
)


class MyCoordCharm(ops.CharmBase):
    external_url = "localhost:3200"

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self.coordinator = Coordinator(
            charm=self,
            roles_config=my_roles,
            external_url=self.external_url,
            worker_metrics_port=8080,
            endpoints={
                "cluster": "cluster",
                "s3": "s3",
                "certificates": "certificates",
                "grafana-dashboards": "grafana-dashboard",
                "logging": "logging",
                "metrics": "metrics-endpoint",
                "charm-tracing": "self-charm-tracing",
                "workload-tracing": "self-workload-tracing",
                "send-datasource": None,
                "catalogue": None,
                "receive-datasource": "my-ds-exchange-require",
            },
            nginx_config=NginxConfig("localhost", {}, {}),
            workers_config=lambda _: "worker config",
            resources_requests=lambda _: {"cpu": "50m", "memory": "100Mi"},
            container_name="charm",
        )


@pytest.fixture
def coord_charm():
    with k8s_patch():
        yield MyCoordCharm


@pytest.fixture
def ctx(coord_charm):
    return testing.Context(
        coord_charm,
        meta={
            "name": "lilith",
            "requires": {
                "s3": {"interface": "s3"},
                "logging": {"interface": "loki_push_api"},
                "certificates": {"interface": "tls-certificates"},
                "self-charm-tracing": {"interface": "tracing", "limit": 1},
                "self-workload-tracing": {"interface": "tracing", "limit": 1},
                "my-ds-exchange-require": {"interface": "grafana_datasource_exchange"},
            },
            "provides": {
                "cluster": {"interface": "cluster"},
                "grafana-dashboard": {"interface": "grafana_dashboard"},
                "metrics-endpoint": {"interface": "prometheus_scrape"},
                "my-ds-exchange-provide": {"interface": "grafana_datasource_exchange"},
            },
            "containers": {
                "nginx": {"type": "oci-image"},
                "nginx-prometheus-exporter": {"type": "oci-image"},
            },
        },
    )


@pytest.fixture()
def s3():
    return testing.Relation(
        "s3",
        remote_app_data={
            "access-key": "key",
            "bucket": "tempo",
            "endpoint": "http://1.2.3.4:9000",
            "secret-key": "soverysecret",
        },
        local_unit_data={"bucket": "tempo"},
    )


@pytest.fixture()
def worker():
    app_data = {}
    ClusterProviderAppData(worker_config="some: yaml").dump(app_data)
    remote_app_data = {}
    ClusterRequirerAppData(role="role").dump(remote_app_data)
    return testing.Relation("cluster", local_app_data=app_data, remote_app_data=remote_app_data)


@pytest.fixture()
def base_state(s3, worker):
    return testing.State(
        leader=True,
        containers={testing.Container("nginx"), testing.Container("nginx-prometheus-exporter")},
        relations={worker, s3},
    )


def set_containers(state, nginx_can_connect=False, exporter_can_connect=False):
    containers = {
        testing.Container("nginx", can_connect=nginx_can_connect),
        testing.Container("nginx-prometheus-exporter", can_connect=exporter_can_connect),
    }
    return dataclasses.replace(state, containers=containers)


@patch(
    "charms.observability_libs.v0.kubernetes_compute_resources_patch.ResourcePatcher.apply",
    MagicMock(return_value=None),
)
def test_status_check_no_workers(ctx, base_state, s3, caplog):
    # GIVEN the container cannot connect
    state = set_containers(base_state, True, False)
    state = dataclasses.replace(state, relations={s3})

    # WHEN we run any event
    state_out = ctx.run(ctx.on.config_changed(), state)

    # THEN the charm sets blocked
    assert state_out.unit_status == ops.BlockedStatus("[consistency] Missing any worker relation.")


@patch(
    "charms.observability_libs.v0.kubernetes_compute_resources_patch.ResourcePatcher.apply",
    MagicMock(return_value=None),
)
def test_status_check_no_s3(ctx, base_state, worker, caplog):
    # GIVEN the container cannot connect
    state = set_containers(base_state, True, False)
    state = dataclasses.replace(base_state, relations={worker})

    # WHEN we run any event
    state_out = ctx.run(ctx.on.config_changed(), state)

    # THEN the charm sets blocked
    assert state_out.unit_status == ops.BlockedStatus("[s3] Missing S3 integration.")


@patch(
    "charms.observability_libs.v0.kubernetes_compute_resources_patch.KubernetesComputeResourcesPatch.get_status",
    MagicMock(return_value=(ops.BlockedStatus(""))),
)
def test_status_check_k8s_patch_failed(ctx, base_state, caplog):
    # GIVEN the container can connect
    state = set_containers(base_state, True, True)

    # WHEN we run any event
    state_out = ctx.run(ctx.on.update_status(), state)

    assert state_out.unit_status == ops.BlockedStatus("")


@patch("charms.observability_libs.v0.kubernetes_compute_resources_patch.ResourcePatcher")
@patch(
    "coordinated_workers.coordinator.KubernetesComputeResourcesPatch.PATCH_RETRY_STOP",
    PropertyMock(return_value=tenacity.wait_fixed(1)),
)
def test_status_check_k8s_patch_success_after_retries(
    resource_patcher_mock, ctx, base_state, caplog
):
    # GIVEN the container can connect
    state = set_containers(base_state, True, True)

    # Retry on that error
    response = httpx.Response(
        status_code=404, content='{"status": {"code": 404, "message": "Not Found"},"code":"404"}'
    )
    # Success on 2nd try
    resource_patcher_mock.return_value.apply.side_effect = [ApiError(response=response), None]

    # on collect-unit-status, the request patches are not yet reflected
    with patch(
        "coordinated_workers.coordinator.KubernetesComputeResourcesPatch.get_status",
        MagicMock(return_value=ops.WaitingStatus("waiting")),
    ):
        state_intermediate = ctx.run(ctx.on.config_changed(), state)
    assert state_intermediate.unit_status == ops.WaitingStatus("waiting")

    with patch(
        "coordinated_workers.coordinator.KubernetesComputeResourcesPatch.get_status",
        MagicMock(return_value=ops.ActiveStatus("")),
    ):
        state_out = ctx.run(ctx.on.update_status(), state_intermediate)
    assert state_out.unit_status == ops.ActiveStatus("Degraded.")


@patch(
    "charms.observability_libs.v0.kubernetes_compute_resources_patch.ResourcePatcher.apply",
    MagicMock(return_value=None),
)
@pytest.mark.parametrize(
    "event",
    (
        CharmEvents.update_status(),
        CharmEvents.start(),
        CharmEvents.install(),
    ),
)
def test_tls_misconfigured_sets_blocked(coord_charm, ctx, base_state, event):
    # GIVEN an https ingress address, but no tls relation
    coord_charm.external_url = "https://198.18.0.0:3200"
    # WHEN the charm processes any event
    state_out = ctx.run(event, base_state)
    # THEN the charm sets blocked
    assert isinstance(state_out.unit_status, BlockedStatus)
    assert "[tls] misconfigured" in state_out.unit_status.message
