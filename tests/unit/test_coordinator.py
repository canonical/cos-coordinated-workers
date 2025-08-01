import dataclasses
import json
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from unittest.mock import PropertyMock, patch
from urllib.parse import urlparse

import ops
import pytest
from charms.catalogue_k8s.v1.catalogue import CatalogueItem
from cosl.interfaces.utils import DataValidationError
from ops import RelationChangedEvent, testing

from coordinated_workers.coordinator import (
    ClusterRolesConfig,
    Coordinator,
    S3NotFoundError,
    TLSConfig,
)
from coordinated_workers.interfaces.cluster import (
    ClusterRemovedEvent,
    ClusterRequirerAppData,
    ClusterRequirerUnitData,
)
from coordinated_workers.nginx import NginxConfig
from tests.unit.test_worker import MyCharm

MOCK_CERTS_DATA = "<TLS_STUFF>"
MOCK_TLS_CONFIG = TLSConfig(MOCK_CERTS_DATA, MOCK_CERTS_DATA, MOCK_CERTS_DATA)


@pytest.fixture
def coordinator_state():
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
                    "application": "writer",
                    "unit": "writer/0",
                    "charm_name": "test-writer",
                },
                address="something",
            ).dump()
        },
    )
    requires_relations["cluster_worker2"] = testing.Relation(
        "my-cluster",
        remote_app_name="worker2",
        remote_app_data=ClusterRequirerAppData(role="backend").dump(),
        remote_units_data={
            0: ClusterRequirerUnitData(
                juju_topology={
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

    return testing.State(
        containers={
            testing.Container("nginx", can_connect=True),
            testing.Container("nginx-prometheus-exporter", can_connect=True),
        },
        relations=list(requires_relations.values()) + list(provides_relations.values()),
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
            },
            "provides": {
                "my-dashboards": {"interface": "grafana_dashboard"},
                "my-metrics": {"interface": "prometheus_scrape"},
                "my-ds-exchange-provide": {"interface": "grafana_datasource_exchange"},
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
                },
                nginx_config=NginxConfig("localhost", {}, {}),
                workers_config=lambda coordinator: f"workers configuration for {coordinator._charm.meta.name}",
                worker_ports=self._worker_ports,
                # nginx_options: Optional[NginxMappingOverrides] = None,
                # is_coherent: Optional[Callable[[ClusterProvider, ClusterRolesConfig], bool]] = None,
                # is_recommended: Optional[Callable[[ClusterProvider, ClusterRolesConfig], bool]] = None,
            )

    return MyCoordinator


def test_worker_roles_subset_of_minimal_deployment(
    coordinator_state: testing.State, coordinator_charm: ops.CharmBase
):
    # Test that the combination of worker roles is a subset of the minimal deployment roles

    # GIVEN a coordinator_charm
    ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)

    # AND a coordinator_state defining relations to worker charms with incomplete distributed roles
    missing_backend_worker_relation = {
        relation
        for relation in coordinator_state.relations
        if relation.remote_app_name != "worker2"
    }

    # WHEN we process any event
    with ctx(
        ctx.on.update_status(),
        state=dataclasses.replace(coordinator_state, relations=missing_backend_worker_relation),
    ) as mgr:
        charm: coordinator_charm = mgr.charm

        # THEN the deployment is not coherent
        assert not charm.coordinator.is_coherent


def test_worker_ports_published(
    coordinator_state: testing.State,
    coordinator_charm: ops.CharmBase,
):
    ports_per_role = {"read": (10, 22), "write": (1, 2132)}

    def _worker_ports(_, role):
        return ports_per_role.get(role, ())

    coordinator_charm._worker_ports = _worker_ports

    # GIVEN a coordinator_charm that has these ports configured
    ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)

    # WHEN we process any event
    state_out = ctx.run(
        ctx.on.update_status(),
        state=dataclasses.replace(coordinator_state, leader=True),
    )
    # THEN the ports are correctly distributed to the workers via their relations
    worker_relations = state_out.get_relations("my-cluster")
    for relation in worker_relations:
        remote_worker_role = json.loads(relation.remote_app_data["role"])
        expected_ports = ports_per_role.get(remote_worker_role, ())

        assert set(json.loads(relation.local_app_data.get("worker_ports", ()))) == set(
            expected_ports
        )


def test_without_s3_integration_raises_error(
    coordinator_state: testing.State, coordinator_charm: ops.CharmBase
):
    # Test that a charm without an s3 integration raises S3NotFoundError

    # GIVEN a coordinator charm without an s3 integration
    ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)
    relations_without_s3 = {
        relation for relation in coordinator_state.relations if relation.endpoint != "my-s3"
    }

    # WHEN we process any event
    with ctx(
        ctx.on.update_status(),
        state=dataclasses.replace(coordinator_state, relations=relations_without_s3),
    ) as mgr:
        # THEN the _s3_config method raises an S3NotFoundError
        with pytest.raises(S3NotFoundError):
            mgr.charm.coordinator._s3_config


@pytest.mark.parametrize("region", (None, "canada"))
@pytest.mark.parametrize("tls_ca_chain", (None, ["my ca chain"]))
@pytest.mark.parametrize("bucket", ("bucky",))
@pytest.mark.parametrize("secret_key", ("foo",))
@pytest.mark.parametrize("access_key", ("foo",))
@pytest.mark.parametrize(
    "endpoint, endpoint_stripped",
    (
        ("example.com", "example.com"),
        ("http://example.com", "example.com"),
        ("https://example.com", "example.com"),
    ),
)
def test_s3_integration(
    coordinator_state: testing.State,
    coordinator_charm: ops.CharmBase,
    region,
    endpoint,
    endpoint_stripped,
    secret_key,
    access_key,
    bucket,
    tls_ca_chain,
):
    # Test that a charm with a s3 integration gives the expected _s3_config

    # GIVEN a coordinator charm with a s3 integration
    ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)
    s3_relation = coordinator_state.get_relations("my-s3")[0]
    relations_except_s3 = [
        relation for relation in coordinator_state.relations if relation.endpoint != "my-s3"
    ]
    s3_app_data = {
        k: json.dumps(v)
        for k, v in {
            **({"region": region} if region else {}),
            **({"tls-ca-chain": tls_ca_chain} if tls_ca_chain else {}),
            "endpoint": endpoint,
            "access-key": access_key,
            "secret-key": secret_key,
            "bucket": bucket,
        }.items()
    }

    # WHEN we process any event
    with ctx(
        ctx.on.update_status(),
        state=dataclasses.replace(
            coordinator_state,
            leader=True,
            relations=relations_except_s3
            + [dataclasses.replace(s3_relation, remote_app_data=s3_app_data)],
        ),
    ) as mgr:
        # THEN the s3_connection_info method returns the expected data structure
        coordinator: Coordinator = mgr.charm.coordinator
        assert coordinator.s3_connection_info.region == region
        assert coordinator.s3_connection_info.bucket == bucket
        assert coordinator.s3_connection_info.endpoint == endpoint
        assert coordinator.s3_connection_info.secret_key == secret_key
        assert coordinator.s3_connection_info.access_key == access_key
        assert coordinator.s3_connection_info.tls_ca_chain == tls_ca_chain
        assert coordinator._s3_config["endpoint"] == endpoint_stripped
        assert coordinator._s3_config["insecure"] is not (
            tls_ca_chain or urlparse(endpoint).scheme == "https"
        )


def test_tracing_receivers_urls(
    coordinator_state: testing.State, coordinator_charm: ops.CharmBase
):
    charm_tracing_relation = testing.Relation(
        endpoint="my-charm-tracing",
        remote_app_data={
            "receivers": json.dumps(
                [{"protocol": {"name": "otlp_http", "type": "http"}, "url": "1.2.3.4:4318"}]
            )
        },
    )
    workload_tracing_relation = testing.Relation(
        endpoint="my-workload-tracing",
        remote_app_data={
            "receivers": json.dumps(
                [
                    {"protocol": {"name": "otlp_http", "type": "http"}, "url": "5.6.7.8:4318"},
                    {"protocol": {"name": "otlp_grpc", "type": "grpc"}, "url": "5.6.7.8:4317"},
                ]
            )
        },
    )
    ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)

    with ctx(
        ctx.on.update_status(),
        state=dataclasses.replace(
            coordinator_state, relations=[charm_tracing_relation, workload_tracing_relation]
        ),
    ) as mgr:
        coordinator: Coordinator = mgr.charm.coordinator
        assert coordinator._charm_tracing_receivers_urls == {
            "otlp_http": "1.2.3.4:4318",
        }
        assert coordinator._workload_tracing_receivers_urls == {
            "otlp_http": "5.6.7.8:4318",
            "otlp_grpc": "5.6.7.8:4317",
        }


def find_relation(relations, endpoint):
    return next(filter(lambda r: r.endpoint == endpoint, relations))


@pytest.mark.parametrize("tls", (True, False))
def test_charm_tracing_configured(
    coordinator_state: testing.State, coordinator_charm: ops.CharmBase, tls: bool, tmp_path
):
    # GIVEN a charm tracing integration (and tls?)
    relations = set(coordinator_state.relations)

    url = "1.2.3.4:4318"
    charm_tracing_relation = testing.Relation(
        endpoint="my-charm-tracing",
        remote_app_data={
            "receivers": json.dumps(
                [{"protocol": {"name": "otlp_http", "type": "http"}, "url": url}]
            )
        },
    )
    relations.remove(find_relation(relations, "my-charm-tracing"))
    relations.add(charm_tracing_relation)

    certs_relation = find_relation(relations, "my-certificates")
    if tls:

        def tls_mock():
            stack = ExitStack()
            stack.enter_context(
                patch.object(
                    Coordinator,
                    "tls_config",
                    new_callable=PropertyMock,
                    return_value=MOCK_TLS_CONFIG,
                )
            )
            stack.enter_context(
                patch("coordinated_workers.nginx.CA_CERT_PATH", new=tmp_path / "rootcacert")
            )
            return stack
    else:

        def tls_mock():
            return nullcontext()

        relations.remove(certs_relation)

    # WHEN we receive any event
    with tls_mock():
        ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)
        # THEN the coordinator has called ops_tracing.set_destination with the expected params
        with patch("ops_tracing.set_destination") as p:
            ctx.run(
                ctx.on.update_status(),
                state=dataclasses.replace(coordinator_state, relations=relations),
            )
    p.assert_called_with(url=url + "/v1/traces", ca=MOCK_CERTS_DATA if tls else None)


@pytest.mark.parametrize(
    "event",
    (
        testing.CharmEvents.update_status(),
        testing.CharmEvents.start(),
        testing.CharmEvents.install(),
        testing.CharmEvents.config_changed(),
    ),
)
def test_invalid_databag_content(coordinator_charm: ops.CharmBase, event):
    # Test Invalid relations databag for ClusterProvider.gather_addresses_by_role

    # GIVEN a coordinator charm with a cluster relation and invalid remote databag contents
    requires_relations = {
        endpoint: testing.Relation(endpoint=endpoint, interface=interface["interface"])
        for endpoint, interface in {
            "my-certificates": {"interface": "certificates"},
            "my-logging": {"interface": "loki_push_api"},
            "my-charm-tracing": {"interface": "tracing"},
            "my-workload-tracing": {"interface": "tracing"},
        }.items()
    }
    requires_relations["cluster_worker0"] = testing.Relation(
        "my-cluster",
        remote_app_name="worker0",
        remote_app_data=ClusterRequirerAppData(role="read").dump(),
    )
    requires_relations["cluster_worker1"] = testing.Relation(
        "my-cluster",
        remote_app_name="worker1",
        remote_app_data=ClusterRequirerAppData(role="read").dump(),
    )
    requires_relations["cluster_worker2"] = testing.Relation(
        "my-cluster",
        remote_app_name="worker2",
    )

    provides_relations = {
        endpoint: testing.Relation(endpoint=endpoint, interface=interface["interface"])
        for endpoint, interface in {
            "my-dashboards": {"interface": "grafana_dashboard"},
            "my-metrics": {"interface": "prometheus_scrape"},
        }.items()
    }

    invalid_databag_state = testing.State(
        containers={
            testing.Container("nginx", can_connect=True),
            testing.Container("nginx-prometheus-exporter", can_connect=True),
        },
        relations=list(requires_relations.values()) + list(provides_relations.values()),
    )

    # WHEN: the coordinator processes any event
    ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)
    with ctx(event, invalid_databag_state) as manager:
        cluster = manager.charm.coordinator.cluster
        # THEN the coordinator sets unit to blocked since the cluster is inconsistent with the missing relation.
        cluster.gather_addresses_by_role()
        manager.run()
    assert cluster.model.unit.status == ops.BlockedStatus("[consistency] Cluster inconsistent.")


@pytest.mark.parametrize("app", (True, False))
def test_invalid_app_or_unit_databag(
    coordinator_charm: ops.CharmBase, coordinator_state, app: bool
):
    # Test that when a relation changes and either the app or unit data is invalid
    #   the worker emits a ClusterRemovedEvent

    # WHEN you define a properly configured charm
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )

    # IF the relation data is invalid (forced by the patched Exception)
    object_to_patch = (
        "coordinated_workers.interfaces.cluster.ClusterProviderAppData.load"
        if app
        else "coordinated_workers.interfaces.cluster.ClusterRequirerUnitData.load"
    )

    with patch(object_to_patch, side_effect=DataValidationError("Mock error")):
        # AND the relation changes
        relation = testing.Relation("cluster")

        ctx.run(
            ctx.on.relation_changed(relation),
            testing.State(
                containers={testing.Container("foo", can_connect=True)}, relations={relation}
            ),
        )

    # NOTE: this difference should not exist, and the ClusterRemovedEvent should always
    #   be emitted in case of corrupted data

    # THEN the charm emits a ClusterRemovedEvent
    if app:
        assert len(ctx.emitted_events) == 2
        assert isinstance(ctx.emitted_events[0], RelationChangedEvent)
        assert isinstance(ctx.emitted_events[1], ClusterRemovedEvent)
    else:
        assert len(ctx.emitted_events) == 1
        assert isinstance(ctx.emitted_events[0], RelationChangedEvent)


@pytest.mark.parametrize(
    ("hostname", "expected_app_hostname"),
    (
        (
            "foo-app-0.foo-app-headless.test.svc.cluster.local",
            "foo-app.test.svc.cluster.local",
        ),
        (
            "foo-app-0.foo-app-headless.test.svc.custom.domain",
            "foo-app.test.svc.custom.domain",
        ),
        (
            "foo-app-0.foo-app-headless.test.svc.custom.svc.domain",
            "foo-app.test.svc.custom.svc.domain",
        ),
        ("localhost", "localhost"),
        ("my.custom.domain", "my.custom.domain"),
        ("192.0.2.1", "192.0.2.1"),
    ),
)
def test_app_hostname(
    coordinator_charm: ops.CharmBase,
    hostname: str,
    expected_app_hostname: str,
):
    # GIVEN a hostname
    ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)

    # WHEN any event fires
    with ctx(ctx.on.update_status(), testing.State(model=testing.Model("test"))) as mgr:
        # THEN if hostname is a valid k8s pod fqdn, app_hostname is set to the k8s service fqdn
        # else app_hostname is set to whatever value hostname has
        assert (
            mgr.charm.coordinator.app_hostname(hostname, mgr.charm.app.name, mgr.charm.model.name)
            == expected_app_hostname
        )


def test_catalogue_integration(coordinator_state: testing.State):
    class MyCatalogCoord(ops.CharmBase):
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
                "my-catalogue": {"interface": "catalogue"},
            },
            "provides": {
                "my-dashboards": {"interface": "grafana_dashboard"},
                "my-metrics": {"interface": "prometheus_scrape"},
                "my-ds-exchange-provide": {"interface": "grafana_datasource_exchange"},
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
                    "catalogue": "my-catalogue",
                },
                nginx_config=NginxConfig("localhost", {}, {}),
                workers_config=lambda coordinator: f"workers configuration for {coordinator._charm.meta.name}",
                worker_ports=self._worker_ports,
                catalogue_item=CatalogueItem("foo", "bar", "baz", "qux"),
                # nginx_options: Optional[NginxMappingOverrides] = None,
                # is_coherent: Optional[Callable[[ClusterProvider, ClusterRolesConfig], bool]] = None,
                # is_recommended: Optional[Callable[[ClusterProvider, ClusterRolesConfig], bool]] = None,
            )

    # GIVEN a catalogue integration
    ctx = testing.Context(MyCatalogCoord, meta=MyCatalogCoord.META)
    catalogue_relation = testing.Relation(endpoint="my-catalogue")
    relations_with_catalog = set(coordinator_state.relations).union({catalogue_relation})

    # WHEN any event fires
    state_out = ctx.run(
        ctx.on.update_status(),
        dataclasses.replace(coordinator_state, leader=True, relations=relations_with_catalog),
    )

    # THEN the coordinator has published his catalogue item
    catalogue_relation_out = state_out.get_relation(catalogue_relation.id)
    assert catalogue_relation_out.local_app_data


@contextmanager
def patch_source_alert_rules():
    resources_base_path = Path(__file__).parent / "resources"
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "coordinated_workers.coordinator.NGINX_ORIGINAL_METRICS_ALERT_RULES_PATH",
                new=resources_base_path / "metrics_alert_rules" / "nginx",
            )
        )

        stack.enter_context(
            patch(
                "coordinated_workers.coordinator.WORKER_ORIGINAL_METRICS_ALERT_RULES_PATH",
                new=resources_base_path / "metrics_alert_rules" / "workers",
            )
        )

        stack.enter_context(
            patch(
                "coordinated_workers.coordinator.ORIGINAL_LOGS_ALERT_RULES_PATH",
                new=resources_base_path / "logs_alert_rules",
            )
        )

        yield


@pytest.mark.parametrize("alerts_type", ("metrics", "logs"))
@pytest.mark.parametrize("workers_no", (1, 2))
def test_rendered_alert_rules(
    coordinator_charm: ops.CharmBase, coordinator_state, workers_no, alerts_type
):
    relation_endpoint = "my-metrics" if alerts_type == "metrics" else "my-logging"
    # ASSUMPTIONS from the sample files in ../resources
    # 4 includes the 2 centralised alert rules added by cos-lib
    coordinator_rules_no = 4 if alerts_type == "metrics" else 2
    worker_rules_no = 1 if alerts_type == "metrics" else 2

    # GIVEN a coordinator charm
    ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)
    # AND an S3 relation
    s3_relation = testing.Relation(
        "my-s3",
        interface="s3",
        remote_app_data={
            "endpoint": "s3",
            "bucket": "foo-bucket",
            "access-key": "my-access-key",
            "secret-key": "my-secret-key",
        },
    )
    # AND a COHERENT cluster of workers
    workers_relation = []
    for worker_no in range(workers_no):
        workers_relation.append(
            testing.Relation(
                "my-cluster",
                remote_app_name=f"worker{worker_no}",
                remote_app_data=ClusterRequirerAppData(role="all").dump(),
                remote_units_data={
                    0: ClusterRequirerUnitData(
                        juju_topology={
                            "application": f"all-{worker_no}",
                            "unit": f"all-{worker_no}/0",
                            "charm_name": "test-all",
                        },
                        address="something",
                    ).dump()
                },
            )
        )
    # AND a <relation_endpoint> relation
    # can be either my-metrics or my-logging
    telemetry_relation = testing.Relation(relation_endpoint)

    # AND this is a leader unit
    state = dataclasses.replace(
        coordinator_state,
        relations={s3_relation, telemetry_relation, *workers_relation},
        leader=True,
    )

    # AND it has sample alert rules
    with patch_source_alert_rules():
        # WHEN a <relation_endpoint>_changed event fires
        state_out = ctx.run(ctx.on.relation_changed(telemetry_relation), state)

    target_relation = state_out.get_relations(relation_endpoint)[0]
    alert_rules_str = target_relation.local_app_data["alert_rules"]

    # THEN each worker has a rendered alert with its topology
    for worker_no in range(workers_no):
        worker_databag_topology = f'"juju_application": "all-{worker_no}"'
        # AND the worker topology appears as many times as there are alert rules for that worker
        assert alert_rules_str.count(worker_databag_topology) == worker_rules_no

    # AND the coordinator has a rendered alert with its topology
    coordinator_databag_topology = '"juju_application": "foo-app"'
    # AND the coordinator topology appears as many times as there are alert rules for that coordinator
    assert alert_rules_str.count(coordinator_databag_topology) == coordinator_rules_no

    # AND total alert rules = total rendered worker alerts + total rendered coordinated alerts
    alert_rules = json.loads(alert_rules_str)
    total_worker_rules_no = worker_rules_no * workers_no
    assert (
        sum(len(group["rules"]) for group in alert_rules["groups"])
        == total_worker_rules_no + coordinator_rules_no
    )
