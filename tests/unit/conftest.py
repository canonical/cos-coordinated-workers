import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import ops
import pytest
import tenacity
from ops import testing
from ops.testing import Container, Exec

from coordinated_workers.coordinator import (
    ClusterRolesConfig,
    Coordinator,
)
from coordinated_workers.interfaces.cluster import (
    ClusterRequirerAppData,
    ClusterRequirerUnitData,
)
from coordinated_workers.nginx import NginxConfig

MOCK_CERTS_DATA = "<TLS_STUFF>"


@pytest.fixture(autouse=True)
def patch_all(tmp_path: Path):
    with ExitStack() as stack:
        # so we don't have to wait for minutes:
        stack.enter_context(
            patch(
                "coordinated_workers.worker.Worker.SERVICE_START_RETRY_WAIT",
                new=tenacity.wait_none(),
            )
        )
        stack.enter_context(
            patch(
                "coordinated_workers.worker.Worker.SERVICE_START_RETRY_STOP",
                new=tenacity.stop_after_delay(1),
            )
        )
        stack.enter_context(
            patch(
                "coordinated_workers.worker.Worker.SERVICE_STATUS_UP_RETRY_WAIT",
                new=tenacity.wait_none(),
            )
        )
        stack.enter_context(
            patch(
                "coordinated_workers.worker.Worker.SERVICE_STATUS_UP_RETRY_STOP",
                new=tenacity.stop_after_delay(1),
            )
        )

        # Prevent the worker's _update_tls_certificates method to try and write our local filesystem
        stack.enter_context(
            patch("coordinated_workers.worker.ROOT_CA_CERT", new=tmp_path / "rootcacert")
        )
        stack.enter_context(
            patch(
                "coordinated_workers.worker.ROOT_CA_CERT_PATH",
                new=Path(tmp_path / "rootcacert"),
            )
        )

        stack.enter_context(
            patch(
                "coordinated_workers.coordinator.CONSOLIDATED_METRICS_ALERT_RULES_PATH",
                new=tmp_path / "consolidated_metrics_rules",
            )
        )

        stack.enter_context(
            patch(
                "coordinated_workers.coordinator.CONSOLIDATED_LOGS_ALERT_RULES_PATH",
                new=tmp_path / "consolidated_logs_rules",
            )
        )

        yield


@pytest.fixture(autouse=True)
def mock_worker_lightkube_client(request):
    """Global mock for the Worker's lightkube client to avoid lightkube calls."""
    # Skip this fixture if the test has explicitly disabled it.
    # To use this feature in a test, mark it with @pytest.mark.disable_worker_lightkube_client_autouse
    if "disable_worker_lightkube_client_autouse" in request.keywords:
        yield
    else:
        with patch("coordinated_workers.worker.Client") as mocked:
            yield mocked


@pytest.fixture(autouse=True)
def mock_worker_reconcile_charm_labels(request):
    """Global mock for the Worker's reconcile_charm_labels to avoid lightkube calls."""
    # Skip this fixture if the test has explicitly disabled it.
    # To use this feature in a test, mark it with @pytest.mark.disable_worker_reconcile_charm_labels_autouse
    if "disable_worker_reconcile_charm_labels_autouse" in request.keywords:
        yield
    else:
        with patch("coordinated_workers.worker.reconcile_charm_labels") as mocked:
            yield mocked


@pytest.fixture(autouse=True)
def mock_policy_resource_manager():
    """Mock _get_policy_resource_manager to prevent lightkube Client instantiation in all tests."""
    with patch("coordinated_workers.service_mesh._get_policy_resource_manager") as mock_get_prm:
        # Create a mock PolicyResourceManager with the necessary methods
        mock_prm = MagicMock()
        mock_get_prm.return_value = mock_prm
        yield mock_get_prm


@pytest.fixture(autouse=True)
def mock_coordinator_lightkube_client(request):
    """Global mock for the Coordinator's lightkube client to avoid lightkube calls."""
    # Skip this fixture if the test has explicitly disabled it.
    # To use this feature in a test, mark it with @pytest.mark.disable_coordinator_lightkube_client_autouse
    if "disable_coordinator_lightkube_client_autouse" in request.keywords:
        yield
    else:
        with patch("coordinated_workers.coordinator.Client") as mocked:
            yield mocked


@pytest.fixture(autouse=True)
def mock_coordinator_reconcile_charm_labels(request):
    """Global mock for the Coordinator's reconcile_charm_labels to avoid lightkube calls."""
    # Skip this fixture if the test has explicitly disabled it.
    # To use this feature in a test, mark it with @pytest.mark.disable_coordinator_reconcile_charm_labels_autouse
    if "disable_coordinator_reconcile_charm_labels_autouse" in request.keywords:
        yield
    else:
        with patch("coordinated_workers.coordinator.reconcile_charm_labels") as mocked:
            yield mocked


@pytest.fixture
def nginx_container():
    return Container(
        "nginx",
        can_connect=True,
        execs={Exec(["update-ca-certificates", "--fresh"], return_code=0)},
    )


@pytest.fixture
def exporter_container():
    return Container(
        "nginx-prometheus-exporter",
        can_connect=True,
    )


@pytest.fixture
def coordinator_state(nginx_container, exporter_container):
    requires_relations = {
        endpoint: testing.Relation(endpoint=endpoint, interface=interface["interface"])
        for endpoint, interface in {
            "my-logging": {"interface": "loki_push_api"},
            "my-charm-tracing": {"interface": "tracing"},
            "my-workload-tracing": {"interface": "tracing"},
        }.items()
    }
    requires_relations["my-certificates"] = testing.Relation(
        "my-certificates",
        interface="certificates",
        remote_app_data={
            "certificates": json.dumps(
                [
                    {
                        "certificate": MOCK_CERTS_DATA,
                        "ca": MOCK_CERTS_DATA,
                        "chain": MOCK_CERTS_DATA,
                        "certificate_signing_request": MOCK_CERTS_DATA,
                    }
                ]
            ),
        },
    )
    requires_relations["my-s3"] = testing.Relation(
        "my-s3",
        interface="s3",
        remote_app_data={
            "endpoint": "s3",
            "bucket": "foo-bucket",
            "access-key": "my-access-key",
            "secret-key": "my-secret-key",
        },
    )
    requires_relations["cluster_worker0"] = testing.Relation(
        "my-cluster",
        remote_app_name="worker0",
        remote_app_data=ClusterRequirerAppData(role="read").dump(),
        remote_units_data={
            0: ClusterRequirerUnitData(
                juju_topology={
                    "model": "test-model",
                    "application": "reader",
                    "unit": "reader/0",
                    "charm_name": "test-reader",
                },
                address="something",
            ).dump()
        },
    )
    requires_relations["cluster_worker1"] = testing.Relation(
        "my-cluster",
        remote_app_name="worker1",
        remote_app_data=ClusterRequirerAppData(role="write").dump(),
        remote_units_data={
            0: ClusterRequirerUnitData(
                juju_topology={
                    "model": "test-model",
                    "application": "writer",
                    "unit": "writer/0",
                    "charm_name": "test-writer",
                },
                address="something",
            ).dump(),
            1: ClusterRequirerUnitData(
                juju_topology={
                    "model": "test-model",
                    "application": "writer",
                    "unit": "writer/1",
                    "charm_name": "test-writer",
                },
                address="something",
            ).dump(),
        },
    )
    requires_relations["cluster_worker2"] = testing.Relation(
        "my-cluster",
        remote_app_name="worker2",
        remote_app_data=ClusterRequirerAppData(role="backend").dump(),
        remote_units_data={
            0: ClusterRequirerUnitData(
                juju_topology={
                    "model": "test-model",
                    "application": "backender",
                    "unit": "backender/0",
                    "charm_name": "test-backender",
                },
                address="something",
            ).dump()
        },
    )

    provides_relations = {
        endpoint: testing.Relation(endpoint=endpoint, interface=interface["interface"])
        for endpoint, interface in {
            "my-dashboards": {"interface": "grafana_dashboard"},
            "my-metrics": {"interface": "prometheus_scrape"},
        }.items()
    }
    peer_relations = [testing.PeerRelation(endpoint="my-peers")]
    return testing.State(
        containers={
            nginx_container,
            exporter_container,
        },
        relations=list(requires_relations.values())
        + list(provides_relations.values())
        + peer_relations,
    )


@pytest.fixture()
def coordinator_charm(request):
    class MyCoordinator(ops.CharmBase):
        META = {
            "name": "foo-app",
            "requires": {
                "my-certificates": {"interface": "certificates"},
                "my-cluster": {"interface": "cluster"},
                "my-logging": {"interface": "loki_push_api"},
                "my-charm-tracing": {"interface": "tracing", "limit": 1},
                "my-workload-tracing": {"interface": "tracing", "limit": 1},
                "my-s3": {"interface": "s3"},
                "my-ds-exchange-require": {"interface": "grafana_datasource_exchange"},
                "my-service-mesh": {"interface": "service_mesh", "limit": 1},
                "my-service-mesh-require-cmr-mesh": {"interface": "cross_model_mesh"},
            },
            "provides": {
                "my-dashboards": {"interface": "grafana_dashboard"},
                "my-metrics": {"interface": "prometheus_scrape"},
                "my-ds-exchange-provide": {"interface": "grafana_datasource_exchange"},
                "my-service-mesh-provide-cmr-mesh": {"interface": "cross_model_mesh"},
            },
            "peers": {
                "my-peers": {
                    "interface": "coordinated_workers_peers",
                },
            },
            "containers": {
                "nginx": {"type": "oci-image"},
                "nginx-prometheus-exporter": {"type": "oci-image"},
            },
        }

        _worker_ports = None

        def __init__(self, framework: ops.Framework):
            super().__init__(framework)
            # Note: Here it is a good idea not to use context mgr because it is "ops aware"
            self.coordinator = Coordinator(
                charm=self,
                # Roles were take from loki-coordinator-k8s-operator
                roles_config=ClusterRolesConfig(
                    roles={"all", "read", "write", "backend"},
                    meta_roles={"all": {"all", "read", "write", "backend"}},
                    minimal_deployment={
                        "read",
                        "write",
                        "backend",
                    },
                    recommended_deployment={
                        "read": 3,
                        "write": 3,
                        "backend": 3,
                    },
                ),
                external_url="https://foo.example.com",
                worker_metrics_port=123,
                endpoints={
                    "certificates": "my-certificates",
                    "cluster": "my-cluster",
                    "grafana-dashboards": "my-dashboards",
                    "logging": "my-logging",
                    "metrics": "my-metrics",
                    "charm-tracing": "my-charm-tracing",
                    "workload-tracing": "my-workload-tracing",
                    "s3": "my-s3",
                    "send-datasource": "my-ds-exchange-provide",
                    "receive-datasource": "my-ds-exchange-require",
                    "catalogue": None,
                    "service-mesh": "my-service-mesh",
                    "service-mesh-provide-cmr-mesh": "my-service-mesh-provide-cmr-mesh",
                    "service-mesh-require-cmr-mesh": "my-service-mesh-require-cmr-mesh",
                },
                nginx_config=NginxConfig("localhost", [], {}),
                workers_config=lambda coordinator: f"workers configuration for {coordinator._charm.meta.name}",
                worker_ports=self._worker_ports,
                # nginx_options: Optional[NginxMappingOverrides] = None,
                # is_coherent: Optional[Callable[[ClusterProvider, ClusterRolesConfig], bool]] = None,
                # is_recommended: Optional[Callable[[ClusterProvider, ClusterRolesConfig], bool]] = None,
                peer_relation="my-peers",
            )

    return MyCoordinator
