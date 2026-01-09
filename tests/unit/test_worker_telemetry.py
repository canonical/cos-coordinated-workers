import json
from unittest.mock import PropertyMock, patch
from urllib.parse import urlparse

import ops
import pytest
from ops import testing

from coordinated_workers.coordinator import ClusterRolesConfig, Coordinator
from coordinated_workers.interfaces.cluster import ClusterRequirerAppData, ClusterRequirerUnitData
from coordinated_workers.nginx import NginxConfig
from coordinated_workers.worker_telemetry import (
    PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX,
    WorkerTelemetryProxyConfig,
)


@pytest.fixture
def coordinator_charm_with_proxy():
    """Create a coordinator charm with worker telemetry proxy enabled."""

    class MyCoordinatorWithProxy(ops.CharmBase):
        META = {
            "name": "test-coordinator",
            "requires": {
                "my-cluster": {"interface": "cluster"},
                "my-logging": {"interface": "loki_push_api"},
                "my-charm-tracing": {"interface": "tracing", "limit": 1},
                "my-workload-tracing": {"interface": "tracing", "limit": 1},
                "my-certificates": {"interface": "certificates"},
                "my-s3": {"interface": "s3"},
                "my-ds-exchange-require": {"interface": "grafana_datasource_exchange"},
            },
            "provides": {
                "my-metrics": {"interface": "prometheus_scrape"},
                "my-dashboards": {"interface": "grafana_dashboard"},
                "my-ds-exchange-provide": {"interface": "grafana_datasource_exchange"},
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

        def __init__(self, framework: ops.Framework):
            super().__init__(framework)
            self.coordinator = Coordinator(
                charm=self,
                roles_config=ClusterRolesConfig(
                    roles={"backend"},
                    meta_roles={},
                    minimal_deployment={"backend"},
                    recommended_deployment={"backend": 1},
                ),
                external_url="https://test-coordinator.example.com",
                worker_metrics_port=9090,
                endpoints={
                    "cluster": "my-cluster",
                    "logging": "my-logging",
                    "metrics": "my-metrics",
                    "charm-tracing": "my-charm-tracing",
                    "workload-tracing": "my-workload-tracing",
                    "certificates": "my-certificates",
                    "grafana-dashboards": "my-dashboards",
                    "s3": "my-s3",
                    "send-datasource": "my-ds-exchange-provide",
                    "receive-datasource": "my-ds-exchange-require",
                    "catalogue": None,
                    "service-mesh": None,
                    "service-mesh-provide-cmr-mesh": None,
                    "service-mesh-require-cmr-mesh": None,
                },
                nginx_config=NginxConfig("localhost", [], {}),
                workers_config=lambda coordinator: f"config for {coordinator._charm.meta.name}",
                worker_telemetry_proxy_config=WorkerTelemetryProxyConfig(
                    http_port=8080, https_port=8443
                ),
                peer_relation="my-peers",
            )

    return MyCoordinatorWithProxy


@pytest.fixture
def coordinator_charm_no_proxy():
    """Create a coordinator charm without worker telemetry proxy."""

    class MyCoordinatorNoProxy(ops.CharmBase):
        META = {
            "name": "test-coordinator",
            "requires": {
                "my-cluster": {"interface": "cluster"},
                "my-logging": {"interface": "loki_push_api"},
                "my-charm-tracing": {"interface": "tracing", "limit": 1},
                "my-workload-tracing": {"interface": "tracing", "limit": 1},
                "my-certificates": {"interface": "certificates"},
                "my-s3": {"interface": "s3"},
                "my-ds-exchange-require": {"interface": "grafana_datasource_exchange"},
            },
            "provides": {
                "my-metrics": {"interface": "prometheus_scrape"},
                "my-dashboards": {"interface": "grafana_dashboard"},
                "my-ds-exchange-provide": {"interface": "grafana_datasource_exchange"},
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

        def __init__(self, framework: ops.Framework):
            super().__init__(framework)
            self.coordinator = Coordinator(
                charm=self,
                roles_config=ClusterRolesConfig(
                    roles={"backend"},
                    meta_roles={},
                    minimal_deployment={"backend"},
                    recommended_deployment={"backend": 1},
                ),
                external_url="https://test-coordinator.example.com",
                worker_metrics_port=9090,
                endpoints={
                    "cluster": "my-cluster",
                    "logging": "my-logging",
                    "metrics": "my-metrics",
                    "charm-tracing": "my-charm-tracing",
                    "workload-tracing": "my-workload-tracing",
                    "certificates": "my-certificates",
                    "grafana-dashboards": "my-dashboards",
                    "s3": "my-s3",
                    "send-datasource": "my-ds-exchange-provide",
                    "receive-datasource": "my-ds-exchange-require",
                    "catalogue": None,
                    "service-mesh": None,
                    "service-mesh-provide-cmr-mesh": None,
                    "service-mesh-require-cmr-mesh": None,
                },
                nginx_config=NginxConfig("localhost", [], {}),
                workers_config=lambda coordinator: f"config for {coordinator._charm.meta.name}",
                peer_relation="my-peers",
            )

    return MyCoordinatorNoProxy


@pytest.fixture
def coordinator_state_with_telemetry(nginx_container, nginx_prometheus_exporter_container):
    """State with telemetry relations for testing worker telemetry proxy."""
    requires_relations = {}
    requires_relations["cluster_backend"] = testing.Relation(
        endpoint="my-cluster",
        remote_app_name="backend",
        remote_app_data=ClusterRequirerAppData(role="backend").dump(),
        remote_units_data={
            0: ClusterRequirerUnitData(
                juju_topology={
                    "application": "backend",
                    "unit": "backend/0",
                    "charm_name": "test-backend",
                },
                address="10.0.0.1",
            ).dump()
        },
    )
    requires_relations["my-logging"] = testing.Relation(
        endpoint="my-logging",
        interface="loki_push_api",
        remote_app_name="loki",
        remote_units_data={
            0: {"endpoint": json.dumps({"url": "http://loki-0:3100/loki/api/v1/push"})},
            1: {"endpoint": json.dumps({"url": "http://loki-1:3100/loki/api/v1/push"})},
        },
    )
    requires_relations["my-charm-tracing"] = testing.Relation(
        endpoint="my-charm-tracing",
        interface="tracing",
        remote_app_data={
            "receivers": json.dumps(
                [{"protocol": {"name": "otlp_http", "type": "http"}, "url": "http://tempo:4318"}]
            )
        },
    )
    requires_relations["my-workload-tracing"] = testing.Relation(
        endpoint="my-workload-tracing",
        interface="tracing",
        remote_app_data={
            "receivers": json.dumps(
                [{"protocol": {"name": "otlp_http", "type": "http"}, "url": "http://tempo:4318"}]
            )
        },
    )
    requires_relations["my-s3"] = testing.Relation(
        endpoint="my-s3",
        interface="s3",
        remote_app_data={
            "endpoint": "s3",
            "bucket": "foo-bucket",
            "access-key": "my-access-key",
            "secret-key": "my-secret-key",
        },
    )
    provides_relations = {
        "my-metrics": testing.Relation(endpoint="my-metrics", interface="prometheus_scrape"),
        "my-dashboards": testing.Relation(endpoint="my-dashboards", interface="grafana_dashboard"),
    }

    return testing.State(
        containers={
            nginx_container,
            nginx_prometheus_exporter_container,
        },
        relations=list(requires_relations.values()) + list(provides_relations.values()),
    )


def test_coordinator_returns_proxy_urls_when_proxy_enabled(
    coordinator_charm_with_proxy, coordinator_state_with_telemetry
):
    """Test that coordinator returns proxy URLs when WorkerTelemetryProxyConfig is provided."""
    # GIVEN a coordinator charm with worker telemetry proxy config enabled
    ctx = testing.Context(coordinator_charm_with_proxy, meta=coordinator_charm_with_proxy.META)

    # WHEN we process any event with telemetry relations present
    with patch.object(
        Coordinator, "hostname", new_callable=PropertyMock, return_value="coordinator.local"
    ):
        with patch.object(
            Coordinator, "tls_available", new_callable=PropertyMock, return_value=False
        ):
            with ctx(ctx.on.update_status(), state=coordinator_state_with_telemetry) as mgr:
                coordinator = mgr.charm.coordinator

                # THEN the coordinator returns proxy URLs for all telemetry types
                charm_urls = coordinator._charm_tracing_receivers_urls
                assert "otlp_http" in charm_urls

                proxy_url = charm_urls["otlp_http"]
                parsed = urlparse(proxy_url)
                assert parsed.hostname == "coordinator.local"  # App hostname format
                assert parsed.port == 8080  # HTTP port from proxy config
                assert "/proxy/charm-tracing/otlp_http/" in parsed.path

                # Test workload tracing URLs - should be proxy URLs
                workload_urls = coordinator._workload_tracing_receivers_urls
                assert "otlp_http" in workload_urls

                workload_proxy_url = workload_urls["otlp_http"]
                parsed = urlparse(workload_proxy_url)
                assert parsed.hostname == "coordinator.local"
                assert parsed.port == 8080
                assert "/proxy/workload-tracing/otlp_http/" in parsed.path

                # Test loki endpoints - should be proxy URLs
                loki_endpoints = coordinator.loki_endpoints_by_unit
                assert "loki/0" in loki_endpoints
                assert "loki/1" in loki_endpoints

                loki_proxy_url = loki_endpoints["loki/0"]
                parsed = urlparse(loki_proxy_url)
                assert parsed.hostname == "coordinator.local"
                assert parsed.port == 8080
                assert "/proxy/loki/loki-0/push" in parsed.path


def test_coordinator_returns_upstream_urls_when_proxy_disabled(
    coordinator_charm_no_proxy, coordinator_state_with_telemetry
):
    """Test that coordinator returns actual upstream URLs when no proxy config is provided."""
    # GIVEN a coordinator charm without worker telemetry proxy config
    ctx = testing.Context(coordinator_charm_no_proxy, meta=coordinator_charm_no_proxy.META)

    # WHEN we process any event with telemetry relations present
    with ctx(ctx.on.update_status(), state=coordinator_state_with_telemetry) as mgr:
        coordinator = mgr.charm.coordinator

        # THEN the coordinator returns actual upstream URLs for all telemetry types
        charm_urls = coordinator._charm_tracing_receivers_urls
        assert "otlp_http" in charm_urls

        upstream_url = charm_urls["otlp_http"]
        parsed = urlparse(upstream_url)
        assert parsed.hostname == "tempo"  # Actual upstream hostname
        assert parsed.port == 4318  # Actual upstream port
        assert "/proxy/" not in parsed.path  # No proxy path

        # Test workload tracing URLs - should be actual upstream URLs
        workload_urls = coordinator._workload_tracing_receivers_urls
        assert "otlp_http" in workload_urls

        workload_upstream_url = workload_urls["otlp_http"]
        parsed = urlparse(workload_upstream_url)
        assert parsed.hostname == "tempo"
        assert parsed.port == 4318
        assert "/proxy/" not in parsed.path

        # Test loki endpoints - should be actual upstream URLs
        loki_endpoints = coordinator.loki_endpoints_by_unit
        assert "loki/0" in loki_endpoints

        loki_upstream_url = loki_endpoints["loki/0"]
        parsed = urlparse(loki_upstream_url)
        assert parsed.hostname == "loki-0"
        assert parsed.port == 3100
        assert "/proxy/" not in parsed.path


@pytest.mark.parametrize("tls_available", [True, False])
def test_coordinator_proxy_urls_respect_tls_and_port_selection(
    coordinator_charm_with_proxy, coordinator_state_with_telemetry, tls_available
):
    """Test that coordinator proxy URLs use correct scheme/port based on TLS and proxy config port selection."""
    # GIVEN a coordinator charm with worker telemetry proxy config and TLS either enabled or disabled
    ctx = testing.Context(coordinator_charm_with_proxy, meta=coordinator_charm_with_proxy.META)

    # WHEN we process any event with the TLS availability state
    with patch.object(
        Coordinator, "hostname", new_callable=PropertyMock, return_value="coordinator.local"
    ):
        with patch.object(
            Coordinator, "tls_available", new_callable=PropertyMock, return_value=tls_available
        ):
            with ctx(ctx.on.update_status(), state=coordinator_state_with_telemetry) as mgr:
                coordinator = mgr.charm.coordinator

                # THEN the proxy URLs use the correct scheme and port based on TLS availability
                expected_scheme = "https" if tls_available else "http"
                expected_port = 8443 if tls_available else 8080

                # Verify proxy port selection
                assert coordinator._proxy_worker_telemetry_port == expected_port

                # Test charm tracing URLs
                charm_urls = coordinator._charm_tracing_receivers_urls
                charm_url = charm_urls["otlp_http"]
                parsed = urlparse(charm_url)
                assert parsed.scheme == expected_scheme
                assert parsed.port == expected_port

                # Test loki endpoints
                loki_endpoints = coordinator.loki_endpoints_by_unit
                loki_url = loki_endpoints["loki/0"]
                parsed = urlparse(loki_url)
                assert parsed.scheme == expected_scheme
                assert parsed.port == expected_port


def test_coordinator_remote_write_proxy_behavior(
    coordinator_charm_with_proxy, coordinator_state_with_telemetry
):
    """Test coordinator remote write endpoint proxying behavior."""
    # GIVEN a coordinator charm with worker telemetry proxy config and remote write endpoints
    ctx = testing.Context(coordinator_charm_with_proxy, meta=coordinator_charm_with_proxy.META)

    # Mock remote write endpoints getter
    sample_endpoints = [
        {"url": "http://prometheus-0.prometheus:9090/api/v1/write"},
        {"url": "https://prometheus-1.prometheus:9090/api/v1/write"},
    ]

    # WHEN we process any event with remote write endpoints available
    with patch.object(
        Coordinator, "hostname", new_callable=PropertyMock, return_value="coordinator.local"
    ):
        with patch.object(
            Coordinator, "tls_available", new_callable=PropertyMock, return_value=False
        ):
            with ctx(ctx.on.update_status(), state=coordinator_state_with_telemetry) as mgr:
                coordinator = mgr.charm.coordinator

                # Patch the remote write endpoints getter
                coordinator._remote_write_endpoints_getter = lambda: sample_endpoints

                # THEN the coordinator returns proxied remote write endpoints
                remote_write_endpoints = coordinator.remote_write_endpoints
                assert len(remote_write_endpoints) == 2

                first_endpoint = remote_write_endpoints[0]
                parsed = urlparse(first_endpoint["url"])
                assert parsed.hostname == "coordinator.local"  # App hostname format
                assert parsed.port == 8080  # HTTP port from proxy config
                assert "/proxy/remote-write/prometheus-0/write" in parsed.path


@pytest.mark.parametrize("proxy_enabled", [True, False])
def test_nginx_config_worker_telemetry_proxy_directives(
    proxy_enabled,
    coordinator_charm_with_proxy,
    coordinator_charm_no_proxy,
    coordinator_state_with_telemetry,
):
    """Test that nginx config contains worker telemetry proxy directives when proxy is enabled."""
    # GIVEN a coordinator charm with or without worker telemetry proxy config
    charm_class = coordinator_charm_with_proxy if proxy_enabled else coordinator_charm_no_proxy

    ctx = testing.Context(charm_class, meta=charm_class.META)

    # WHEN we reconcile worker telemetry and generate nginx configuration
    with patch.object(
        Coordinator, "hostname", new_callable=PropertyMock, return_value="coordinator.local"
    ):
        with patch.object(
            Coordinator, "tls_available", new_callable=PropertyMock, return_value=False
        ):
            with ctx(ctx.on.update_status(), state=coordinator_state_with_telemetry) as mgr:
                coordinator = mgr.charm.coordinator

                # Set up remote write endpoints for proxy testing
                coordinator._remote_write_endpoints_getter = lambda: [
                    {"url": "http://prometheus-0:9090/api/v1/write"}
                ]

                # Build the nginx config to test worker telemetry configuration
                nginx_config_obj = coordinator._build_nginx_config()
                nginx_config = nginx_config_obj.get_config(
                    coordinator._upstreams_to_addresses, listen_tls=False
                )

                # THEN the nginx config contains proxy directives only when proxy is enabled
                if proxy_enabled:
                    # Check for worker telemetry proxy locations when enabled
                    assert "/proxy/charm-tracing/" in nginx_config
                    assert "/proxy/workload-tracing/" in nginx_config
                    assert "/proxy/loki/" in nginx_config
                    assert "/proxy/remote-write/" in nginx_config
                    # Check for upstream definitions using the imported prefix
                    assert f"upstream {PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-" in nginx_config
                else:
                    # Check that proxy locations are NOT present when disabled
                    assert "/proxy/charm-tracing/" not in nginx_config
                    assert "/proxy/workload-tracing/" not in nginx_config
                    assert "/proxy/loki/" not in nginx_config
                    assert "/proxy/remote-write/" not in nginx_config
                    assert (
                        f"upstream {PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-" not in nginx_config
                    )


def test_nginx_upstream_keys_match_address_mapping(
    coordinator_charm_with_proxy, coordinator_state_with_telemetry
):
    """Test that nginx upstream keys match the keys in upstreams_to_addresses mapping."""
    # GIVEN a coordinator charm with worker telemetry proxy config
    ctx = testing.Context(coordinator_charm_with_proxy, meta=coordinator_charm_with_proxy.META)

    # WHEN we reconcile worker telemetry which populates both nginx config and upstreams_to_addresses
    with patch.object(
        Coordinator, "hostname", new_callable=PropertyMock, return_value="coordinator.local"
    ):
        with patch.object(
            Coordinator, "tls_available", new_callable=PropertyMock, return_value=False
        ):
            with ctx(ctx.on.update_status(), state=coordinator_state_with_telemetry) as mgr:
                coordinator = mgr.charm.coordinator

                # Set up remote write endpoints for proxy testing
                coordinator._remote_write_endpoints_getter = lambda: [
                    {"url": "http://prometheus-0:9090/api/v1/write"}
                ]

                # Build the nginx config to test worker telemetry configuration
                nginx_config_obj = coordinator._build_nginx_config()

                # THEN every nginx upstream config has a corresponding key in upstreams_to_addresses
                # Get all upstream configs from nginx config that require address lookup
                nginx_upstream_keys = set()
                for upstream in nginx_config_obj.upstream_configs:
                    # Skip upstreams that are configured to ignore address lookup
                    if not getattr(upstream, "ignore_worker_role", False):
                        nginx_upstream_keys.add(upstream.worker_role)

                # Get all keys from upstreams_to_addresses mapping
                address_mapping_keys = set(coordinator._upstreams_to_addresses.keys())

                # Every nginx upstream should have a corresponding address mapping
                # Note: address_mapping_keys may have more entries than nginx_upstream_keys
                assert nginx_upstream_keys.issubset(address_mapping_keys), (
                    f"Nginx upstream keys {nginx_upstream_keys - address_mapping_keys} "
                    f"not found in upstreams_to_addresses mapping"
                )


def test_telemetry_proxy_with_ip_address_and_default_port(
    coordinator_charm_with_proxy, coordinator_state_with_telemetry
):
    """Test that telemetry endpoints with IP addresses and no explicit port are handled correctly."""
    ctx = testing.Context(coordinator_charm_with_proxy, meta=coordinator_charm_with_proxy.META)

    # Simulates ingressed URL scenario: IP address with no explicit port
    sample_endpoints = [
        {"url": "https://192.168.1.108/cos-mimir/api/v1/push"},
    ]

    with patch.object(
        Coordinator, "hostname", new_callable=PropertyMock, return_value="coordinator.local"
    ):
        with patch.object(
            Coordinator, "tls_available", new_callable=PropertyMock, return_value=True
        ):
            with ctx(ctx.on.update_status(), state=coordinator_state_with_telemetry) as mgr:
                coordinator = mgr.charm.coordinator
                coordinator._remote_write_endpoints_getter = lambda: sample_endpoints

                # THEN the proxy URL should use sanitized IP (192-168-1-108)
                remote_write_endpoints = coordinator.remote_write_endpoints
                assert len(remote_write_endpoints) == 1
                parsed = urlparse(remote_write_endpoints[0]["url"])
                assert "/proxy/remote-write/192-168-1-108/write" in parsed.path

                # AND the nginx config should have valid upstream with default port 443
                nginx_config_obj = coordinator._build_nginx_config()
                nginx_config = nginx_config_obj.get_config(
                    coordinator._upstreams_to_addresses, listen_tls=True
                )
                assert "192.168.1.108:443" in nginx_config
                assert "192.168.1.108:None" not in nginx_config
                assert "worker-telemetry-proxy-192-168-1-108" in nginx_config
