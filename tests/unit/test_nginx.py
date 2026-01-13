import logging
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Tuple
from unittest.mock import MagicMock, PropertyMock, patch

import ops
import pytest
from ops import pebble, testing

from coordinated_workers.coordinator import (
    TLSConfig,
)
from coordinated_workers.nginx import (
    CA_CERT_PATH,
    CERT_PATH,
    DEFAULT_OPTIONS,
    KEY_PATH,
    NGINX_CONFIG,
    PROM_EXPORTER_CERT_PATH,
    PROM_EXPORTER_KEY_PATH,
    Nginx,
    NginxConfig,
    NginxLocationConfig,
    NginxMapConfig,
    NginxPrometheusExporter,
    NginxTracingConfig,
    NginxUpstream,
)
from tests.unit.helpers import tls_mock

sample_dns_ip = "198.18.0.0"
MOCK_CERTS_DATA = "<TLS_STUFF>"
MOCK_TLS_CONFIG = TLSConfig(MOCK_CERTS_DATA, MOCK_CERTS_DATA, MOCK_CERTS_DATA)

logger = logging.getLogger(__name__)


def _create_mounts(cert_paths):
    mounts = {}
    for path in cert_paths:
        temp_file = tempfile.NamedTemporaryFile(delete=False)
        mounts[path] = testing.Mount(location=path, source=temp_file.name)

    # TODO: Do we need to clean up the temp files since delete=False was set?
    return mounts


@pytest.fixture
def nginx_tempfiles():
    return {KEY_PATH, CERT_PATH, CA_CERT_PATH}


@pytest.fixture
def exporter_tempfiles():
    return {PROM_EXPORTER_KEY_PATH, PROM_EXPORTER_CERT_PATH}


@pytest.fixture
def certificate_mounts(nginx_tempfiles):
    return _create_mounts(nginx_tempfiles)


@pytest.fixture
def exporter_certificate_mounts(exporter_tempfiles):
    return _create_mounts(exporter_tempfiles)


@pytest.fixture
def nginx_context():
    return testing.Context(
        ops.CharmBase, meta={"name": "foo", "containers": {"nginx": {"type": "oci-image"}}}
    )


@pytest.fixture
def exporter_context():
    return testing.Context(
        ops.CharmBase,
        meta={"name": "foo", "containers": {"nginx-prometheus-exporter": {"type": "oci-image"}}},
    )


@pytest.fixture
def cert_mounts(request):
    # A fixture to provide parametrized values for certificate mounts.
    # If the parameter is a string, it's assumed to be a fixture name.
    if isinstance(request.param, str):
        return request.getfixturevalue(request.param)
    return request.param


def test_certs_on_disk(certificate_mounts: dict, nginx_context: testing.Context, nginx_container):
    # GIVEN any charm with a container
    ctx = nginx_context

    # WHEN we process any event
    with ctx(
        ctx.on.update_status(),
        state=testing.State(containers={replace(nginx_container, mounts=certificate_mounts)}),
    ) as mgr:
        charm = mgr.charm
        nginx = Nginx(charm)

        # THEN the certs exist on disk
        assert nginx.are_certificates_on_disk


def test_certs_deleted(certificate_mounts: dict, nginx_context: testing.Context, nginx_container):
    # Test deleting the certificates.

    # GIVEN any charm with a container
    ctx = nginx_context

    # WHEN we process any event
    with ctx(
        ctx.on.update_status(),
        state=testing.State(
            containers={
                replace(nginx_container, mounts=certificate_mounts),
            }
        ),
    ) as mgr:
        charm = mgr.charm
        nginx = Nginx(charm)

        # AND when we call delete_certificates
        nginx._delete_certificates()

        # THEN the certs get deleted from disk
        assert not nginx.are_certificates_on_disk


def test_has_config_changed(nginx_context: testing.Context, nginx_container):
    # Test changing the nginx config and catching the change.

    # GIVEN any charm with a container and a nginx config file
    test_config = tempfile.NamedTemporaryFile(delete=False, mode="w+")
    ctx = nginx_context
    # AND when we write to the config file
    with open(test_config.name, "w") as f:
        f.write("foo")

    # WHEN we process any event
    with ctx(
        ctx.on.update_status(),
        state=testing.State(
            containers={
                replace(
                    nginx_container,
                    mounts={
                        "config": testing.Mount(location=NGINX_CONFIG, source=test_config.name)
                    },
                )
            },
        ),
    ) as mgr:
        charm = mgr.charm
        nginx = Nginx(charm)

        # AND a unique config is added
        new_config = "bar"

        # THEN the _has_config_changed method correctly determines that foo != bar
        assert nginx._has_config_changed(new_config)


@pytest.mark.parametrize("container_name", ("nginx", "custom-nginx"))
def test_nginx_pebble_plan(container_name):
    expected_layer = {
        "summary": "nginx layer",
        "description": "pebble config layer for Nginx",
        "services": {
            container_name: {
                "override": "replace",
                "summary": "nginx",
                "command": "nginx -g 'daemon off;'",
                "startup": "enabled",
            }
        },
    }

    # GIVEN any charm with a container
    ctx = testing.Context(
        ops.CharmBase, meta={"name": "foo", "containers": {container_name: {"type": "oci-image"}}}
    )

    # WHEN we process any event
    with ctx(
        ctx.on.update_status(),
        state=testing.State(
            containers={
                testing.Container(
                    container_name,
                    can_connect=True,
                )
            },
        ),
    ) as mgr:
        charm = mgr.charm
        nginx = Nginx(charm, container_name=container_name)
        # THEN the generated pebble layer has the container_name set as the service name
        assert nginx.layer == expected_layer


@pytest.mark.parametrize("tls", (False, True))
def test_nginx_pebble_checks(tls, nginx_container):
    check_endpoint = f"http{'s' if tls else ''}://1.2.3.4/health"
    expected_partial_service_dict = {"nginx-up": "restart"}

    # GIVEN any charm with a container
    ctx = testing.Context(
        ops.CharmBase, meta={"name": "foo", "containers": {"nginx": {"type": "oci-image"}}}
    )

    # WHEN we process any event
    with ctx(
        ctx.on.update_status(),
        state=testing.State(
            containers={
                nginx_container,
            },
        ),
    ) as mgr:
        charm = mgr.charm
        # AND we pass a liveness check endpoint
        nginx = Nginx(charm, liveness_check_endpoint_getter=lambda _: check_endpoint)
        nginx.reconcile("mock nginx config")
        # THEN the generated pebble layer has the expected pebble check
        out = mgr.run()
        layer = out.get_container("nginx").layers["nginx"]
        actual_services = layer.services
        actual_checks = layer.checks
        assert actual_checks["nginx-up"].http == {"url": check_endpoint}
        # AND the pebble layer service has a restart on check-failure
        assert actual_services["nginx"].on_check_failure == expected_partial_service_dict


@contextmanager
def mock_resolv_conf(contents: str):
    with tempfile.NamedTemporaryFile() as tf:
        Path(tf.name).write_text(contents)
        with patch("coordinated_workers.nginx.RESOLV_CONF_PATH", tf.name):
            yield


@pytest.mark.parametrize(
    "mock_contents, expected_dns_ip",
    (
        (f"foo bar\nnameserver {sample_dns_ip}", sample_dns_ip),
        (f"nameserver {sample_dns_ip}\n foo bar baz", sample_dns_ip),
        (
            f"foo bar\nfoo bar\nnameserver {sample_dns_ip}\nnameserver 198.18.0.1",
            sample_dns_ip,
        ),
    ),
)
def test_dns_ip_addr_getter(mock_contents, expected_dns_ip):
    with mock_resolv_conf(mock_contents):
        assert NginxConfig._get_dns_ip_address() == expected_dns_ip


def test_dns_ip_addr_fail():
    with pytest.raises(RuntimeError):
        with mock_resolv_conf("foo bar"):
            NginxConfig._get_dns_ip_address()


@pytest.mark.parametrize("workload", ("tempo", "mimir", "loki"))
@pytest.mark.parametrize("tls", (False, True))
def test_generate_nginx_config(tls, workload):
    upstream_configs, server_ports_to_locations = _get_nginx_config_params(workload)
    # loki & mimir changes the port from 8080 to 443 when TLS is enabled
    if workload in ("loki", "mimir") and tls:
        server_ports_to_locations[443] = server_ports_to_locations.pop(8080)

    addrs_by_role = {
        role: {"worker-address"}
        for role in (upstream.worker_role for upstream in upstream_configs)
    }
    with mock_resolv_conf(f"foo bar\nnameserver {sample_dns_ip}"):
        nginx = NginxConfig(
            "localhost",
            upstream_configs=upstream_configs,
            server_ports_to_locations=server_ports_to_locations,
            enable_health_check=True if workload in ("mimir", "loki") else False,
            enable_status_page=True if workload in ("mimir", "loki") else False,
        )
        generated_config = nginx.get_config(addrs_by_role, tls)
        sample_config_path = (
            Path(__file__).parent
            / "resources"
            / f"sample_{workload}_nginx_conf{'_tls' if tls else ''}.txt"
        )
        assert sample_config_path.read_text() == generated_config


def test_generate_nginx_config_with_root_path():
    upstream_configs, server_ports_to_locations = _get_nginx_config_params("tempo")

    addrs_by_role = {
        role: {"worker-address"}
        for role in (upstream.worker_role for upstream in upstream_configs)
    }
    with mock_resolv_conf(f"foo bar\nnameserver {sample_dns_ip}"):
        nginx = NginxConfig(
            "localhost",
            upstream_configs=upstream_configs,
            server_ports_to_locations=server_ports_to_locations,
            enable_health_check=False,
            enable_status_page=False,
        )
        generated_config = nginx.get_config(addrs_by_role, False, root_path="/dist")
        sample_config_path = (
            Path(__file__).parent / "resources" / "sample_tempo_nginx_conf_root_path.txt"
        )
        assert sample_config_path.read_text() == generated_config


def test_generate_litmus_config_with_rewrite():
    upstream_configs, server_ports_to_locations = _get_nginx_config_params("litmus")

    addrs_by_role = {
        "auth": ["worker-address"],
        "backend": ["worker-address"],
    }
    with mock_resolv_conf(f"foo bar\nnameserver {sample_dns_ip}"):
        nginx = NginxConfig(
            "localhost",
            upstream_configs=upstream_configs,
            server_ports_to_locations=server_ports_to_locations,
            enable_health_check=False,
            enable_status_page=False,
        )
        generated_config = nginx.get_config(addrs_by_role, False)
        sample_config_path = (
            Path(__file__).parent / "resources" / "sample_litmus_conf_with_rewrite.txt"
        )
        assert sample_config_path.read_text() == generated_config


def test_generate_nginx_config_with_extra_location_directives():
    upstream_configs, server_ports_to_locations = _get_nginx_config_params("litmus_ssl")

    addrs_by_role = {
        role: {"worker-address"}
        for role in (upstream.worker_role for upstream in upstream_configs)
    }
    with mock_resolv_conf(f"foo bar\nnameserver {sample_dns_ip}"):
        nginx = NginxConfig(
            "localhost",
            upstream_configs=upstream_configs,
            server_ports_to_locations=server_ports_to_locations,
            enable_health_check=False,
            enable_status_page=False,
        )
        generated_config = nginx.get_config(addrs_by_role, False, root_path="/dist")
        sample_config_path = Path(__file__).parent / "resources" / "sample_litmus_ssl_conf.txt"
        assert sample_config_path.read_text() == generated_config


def test_location_skipped_if_no_matching_upstream():
    upstream_configs, server_ports_to_locations = (
        [],
        _get_server_ports_to_locations("litmus_ssl"),
    )

    addrs_by_role = {
        role: {"worker-address"}
        for role in (upstream.worker_role for upstream in upstream_configs)
    }
    with mock_resolv_conf(f"foo bar\nnameserver {sample_dns_ip}"):
        nginx = NginxConfig(
            "localhost",
            upstream_configs=upstream_configs,
            server_ports_to_locations=server_ports_to_locations,
            enable_health_check=False,
            enable_status_page=False,
        )
        generated_config = nginx.get_config(addrs_by_role, False, root_path="/dist")
        sample_config_path = (
            Path(__file__).parent / "resources" / "sample_litmus_missing_upstreams_conf.txt"
        )
        assert sample_config_path.read_text() == generated_config


def test_generate_nginx_config_with_tracing_enabled():
    mock_tracing_config = NginxTracingConfig(
        endpoint="endpoint:4317",
        service_name="nginx-workload",
        resource_attributes={
            "juju_application": "nginx",
            "juju_model": "test",
            "juju_unit": "nginx/0",
        },
    )
    upstream_configs, server_ports_to_locations = _get_nginx_config_params("litmus")

    addrs_by_role = {
        "auth": ["worker-address"],
        "backend": ["worker-address"],
    }
    with mock_resolv_conf(f"foo bar\nnameserver {sample_dns_ip}"):
        nginx = NginxConfig(
            "localhost",
            upstream_configs=upstream_configs,
            server_ports_to_locations=server_ports_to_locations,
            enable_health_check=False,
            enable_status_page=False,
        )
        generated_config = nginx.get_config(
            addrs_by_role, False, tracing_config=mock_tracing_config
        )
        sample_config_path = (
            Path(__file__).parent / "resources" / "sample_litmus_conf_with_tracing.txt"
        )
        assert sample_config_path.read_text() == generated_config


def test_generate_nginx_config_with_extra_http_variables():
    upstream_configs, server_ports_to_locations = _get_nginx_config_params("litmus")

    addrs_by_role = {
        "auth": ["worker-address"],
        "backend": ["worker-address"],
    }
    with mock_resolv_conf(f"foo bar\nnameserver {sample_dns_ip}"):
        nginx = NginxConfig(
            "localhost",
            upstream_configs=upstream_configs,
            server_ports_to_locations=server_ports_to_locations,
            map_configs=[
                NginxMapConfig(
                    source_variable="$http_upgrade",
                    target_variable="$connection_upgrade",
                    value_mappings={
                        "default": ["upgrade"],
                        "": ["close"],
                    },
                )
            ],
            enable_health_check=False,
            enable_status_page=False,
        )
        generated_config = nginx.get_config(addrs_by_role, False)
        sample_config_path = (
            Path(__file__).parent
            / "resources"
            / "sample_litmus_conf_with_extra_http_variables.txt"
        )
        assert sample_config_path.read_text() == generated_config


def test_exception_raised_if_nginx_module_missing(caplog):
    # GIVEN an instance of Nginx class
    mock_container = MagicMock()
    mock_unit = MagicMock()
    mock_charm = MagicMock()

    # AND a mock container that will fail when the pebble service is started
    mock_container.autostart = MagicMock(side_effect=pebble.ChangeError("something", MagicMock()))
    # AND ngx_otel_module file is not found in the container
    mock_container.exists.return_value = False

    mock_unit.get_container = MagicMock(return_value=mock_container)
    mock_charm.unit = mock_unit
    nginx = Nginx(mock_charm)

    # WHEN we call nginx.reconcile with some tracing related config
    # THEN an exception should be raised
    with pytest.raises(pebble.ChangeError):
        with caplog.at_level("ERROR"):
            nginx.reconcile(nginx_config="placeholder nginx config with ngx_otel_module")

    # AND we can verify that the missing-module message is in the logs
    assert "missing the ngx_otel_module" in caplog.text


upstream_configs = {
    "tempo": [
        NginxUpstream("zipkin", 9411, "distributor"),
        NginxUpstream("otlp-grpc", 4317, "distributor"),
        NginxUpstream("otlp-http", 4318, "distributor"),
        NginxUpstream("jaeger-thrift-http", 14268, "distributor"),
        NginxUpstream("jaeger-grpc", 14250, "distributor"),
        NginxUpstream("tempo-http", 3200, "query-frontend"),
        NginxUpstream("tempo-grpc", 9096, "query-frontend"),
    ],
    "mimir": [
        NginxUpstream("distributor", 8080, "distributor"),
        NginxUpstream("compactor", 8080, "compactor"),
        NginxUpstream("querier", 8080, "querier"),
        NginxUpstream("query-frontend", 8080, "query-frontend"),
        NginxUpstream("ingester", 8080, "ingester"),
        NginxUpstream("ruler", 8080, "ruler"),
        NginxUpstream("store-gateway", 8080, "store-gateway"),
    ],
    "loki": [
        NginxUpstream("read", 3100, "read"),
        NginxUpstream("write", 3100, "write"),
        NginxUpstream("all", 3100, "all"),
        NginxUpstream("backend", 3100, "backend"),
        NginxUpstream("worker", 3100, "worker", ignore_worker_role=True),
    ],
    "litmus": [
        NginxUpstream("auth", 3000, "auth"),
        NginxUpstream("backend", 8080, "backend"),
    ],
    "litmus_ssl": [
        NginxUpstream("auth", 3001, "auth"),
        NginxUpstream("backend", 8081, "backend"),
    ],
}
server_ports_to_locations = {
    "tempo": {
        9411: [NginxLocationConfig(backend="zipkin", path="/")],
        4317: [NginxLocationConfig(backend="otlp-grpc", path="/", is_grpc=True)],
        4318: [NginxLocationConfig(backend="otlp-http", path="/")],
        14268: [NginxLocationConfig(backend="jaeger-thrift-http", path="/")],
        14250: [NginxLocationConfig(backend="jaeger-grpc", path="/", is_grpc=True)],
        3200: [NginxLocationConfig(backend="tempo-http", path="/")],
        9096: [NginxLocationConfig(backend="tempo-grpc", path="/", is_grpc=True)],
    },
    "mimir": {
        8080: [
            NginxLocationConfig(path="/distributor", backend="distributor"),
            NginxLocationConfig(path="/api/v1/push", backend="distributor"),
            NginxLocationConfig(path="/otlp/v1/metrics", backend="distributor"),
            NginxLocationConfig(path="/prometheus/config/v1/rules", backend="ruler"),
            NginxLocationConfig(path="/prometheus/api/v1/rules", backend="ruler"),
            NginxLocationConfig(path="/prometheus/api/v1/alerts", backend="ruler"),
            NginxLocationConfig(path="/ruler/ring", backend="ruler", modifier="="),
            NginxLocationConfig(path="/prometheus", backend="query-frontend"),
            NginxLocationConfig(
                path="/api/v1/status/buildinfo", backend="query-frontend", modifier="="
            ),
            NginxLocationConfig(path="/api/v1/upload/block/", backend="compactor", modifier="="),
        ]
    },
    "loki": {
        8080: [
            NginxLocationConfig(path="/loki/api/v1/push", modifier="=", backend="write"),
            NginxLocationConfig(path="/loki/api/v1/rules", modifier="=", backend="backend"),
            NginxLocationConfig(path="/prometheus", modifier="=", backend="backend"),
            NginxLocationConfig(
                path="/api/v1/rules",
                modifier="=",
                backend="backend",
                backend_url="/loki/api/v1/rules",
            ),
            NginxLocationConfig(path="/loki/api/v1/tail", modifier="=", backend="read"),
            NginxLocationConfig(
                path="/loki/api/.*",
                modifier="~",
                backend="read",
                headers={"Upgrade": "$http_upgrade", "Connection": "upgrade"},
            ),
            NginxLocationConfig(path="/loki/api/v1/format_query", modifier="=", backend="worker"),
            NginxLocationConfig(
                path="/loki/api/v1/status/buildinfo", modifier="=", backend="worker"
            ),
            NginxLocationConfig(path="/ring", modifier="=", backend="worker"),
        ]
    },
    "litmus": {
        8185: [
            NginxLocationConfig(
                path="/",
                extra_directives={
                    "add_header": ["Cache-Control", "no-cache"],
                    "try_files": ["$uri", "/index.html"],
                    "autoindex": ["on"],
                },
            ),
            NginxLocationConfig(
                path="/auth", backend="auth", rewrite=["^/auth(/.*)$", "$1", "break"]
            ),
            NginxLocationConfig(path="/api", backend="backend"),
        ]
    },
    "litmus_ssl": {
        8185: [
            NginxLocationConfig(
                path="/",
                extra_directives={
                    "add_header": ["Cache-Control", "no-cache"],
                    "try_files": ["$uri", "/index.html"],
                    "autoindex": ["on"],
                },
            ),
            NginxLocationConfig(
                path="/auth",
                backend="auth",
                rewrite=["^/auth(/.*)$", "$1", "break"],
                extra_directives={
                    "proxy_ssl_verify": ["off"],
                    "proxy_ssl_session_reuse": ["on"],
                    "proxy_ssl_certificate": ["/etc/tls/tls.crt"],
                    "proxy_ssl_certificate_key": ["/etc/tls/tls.key"],
                },
            ),
            NginxLocationConfig(
                path="/api",
                backend="backend",
                extra_directives={
                    "proxy_ssl_verify": ["off"],
                    "proxy_ssl_session_reuse": ["on"],
                    "proxy_ssl_certificate": ["/etc/tls/tls.crt"],
                    "proxy_ssl_certificate_key": ["/etc/tls/tls.key"],
                },
            ),
        ]
    },
}


def _get_nginx_config_params(workload: str) -> Tuple[list, dict]:
    return upstream_configs[workload], _get_server_ports_to_locations(workload)


def _get_server_ports_to_locations(workload: str) -> Dict[int, List[NginxLocationConfig]]:
    return server_ports_to_locations[workload]


def test_exporter_certs_mgmt(
    exporter_context: testing.Context, exporter_container, exporter_certificate_mounts: dict
):
    # GIVEN any charm with a container
    ctx = exporter_context

    # WHEN we process any event with certificate mounts, to simulate TLS configured
    with ctx(
        ctx.on.update_status(),
        state=testing.State(
            containers={
                replace(exporter_container, mounts=exporter_certificate_mounts),
            }
        ),
    ) as mgr:
        nginx_prometheus_exporter = NginxPrometheusExporter(mgr.charm)

        # THEN the certs exist on disk
        assert nginx_prometheus_exporter.are_certificates_on_disk

        # AND when we delete_certificates
        nginx_prometheus_exporter._configure_tls(tls_config=None)

        # THEN the certs get deleted from disk
        assert not nginx_prometheus_exporter.are_certificates_on_disk


@contextmanager
def mock_web_config(contents: str):
    with tempfile.NamedTemporaryFile() as tf:
        Path(tf.name).write_text(contents)
        with patch("coordinated_workers.nginx.PROM_EXPORTER_WEB_CONFIG", tf.name):
            yield


# @pytest.mark.parametrize("cert_mounts", [{}, "exporter_certificate_mounts"], indirect=True)
# def test_exporter_web_config_file_switch(
#     cert_mounts,
#     exporter_context: testing.Context,
#     exporter_container,
# ):
# # GIVEN any charm with a container
# ctx = exporter_context
# is_tls = bool(cert_mounts)

# # WHEN we process any event with (or without) certificate mounts
# with mock_web_config("foo") if is_tls else contextmanager(lambda: (yield))():
#     with ctx(
#         ctx.on.update_status(),
#         state=testing.State(containers={replace(exporter_container, mounts=cert_mounts)}),
#     ) as mgr:
#         nginx_prometheus_exporter = NginxPrometheusExporter(mgr.charm)
#         mgr.run()
# # THEN the --web.config.file option is added to the pebble command if TLS
# assert (
#     f"--web.config.file={PROM_EXPORTER_WEB_CONFIG}" in nginx_prometheus_exporter.command
# ) == is_tls

# protocol = "https" if is_tls else "http"
# port_key = "nginx_tls_port" if is_tls else "nginx_port"
# port = DEFAULT_OPTIONS.get(port_key)
# expected_scrape_uri = f"--nginx.scrape-uri={protocol}://127.0.0.1:{port}/status"
# assert expected_scrape_uri in nginx_prometheus_exporter.command


def test_exporter_web_config_file_switch(
    mock_policy_resource_manager,
    coordinator_state: testing.State,
    coordinator_charm: ops.CharmBase,
    tmp_path,
):
    # GIVEN any charm with a container
    ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)

    # WHEN we process any event, without TLS configured
    with ctx(
        ctx.on.update_status(),
        state=coordinator_state,
    ) as mgr:
        services_out = mgr.run().get_container("nginx-prometheus-exporter").plan.services

    # THEN the --nginx.scrape-uri option is HTTP in the pebble command
    command = services_out.get("nginx-prometheus-exporter").command
    port = DEFAULT_OPTIONS.get("nginx_port")
    expected_scrape_uri = f"--nginx.scrape-uri=http://127.0.0.1:{port}/status"
    assert expected_scrape_uri in command

    # AND WHEN we process any event, with TLS configured
    with tls_mock(tmp_path):
        ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)
        with patch(
            "coordinated_workers.coordinator.NginxPrometheusExporter.are_certificates_on_disk",
            PropertyMock(return_value=True),
        ):
            state_out = ctx.run(
                ctx.on.update_status(),
                state=coordinator_state,
            )

    # THEN the --nginx.scrape-uri option is HTTPS in the pebble command
    services_out = state_out.get_container("nginx-prometheus-exporter").plan.services
    command = services_out.get("nginx-prometheus-exporter").command
    port = DEFAULT_OPTIONS.get("nginx_tls_port")
    expected_scrape_uri = f"--nginx.scrape-uri=https://127.0.0.1:{port}/status"
    assert expected_scrape_uri in command


def test_nginx_exporter_pebble_layer(
    mock_policy_resource_manager,
    coordinator_state: testing.State,
    coordinator_charm: ops.CharmBase,
    tmp_path,
):
    # GIVEN any charm with a container
    ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)

    # WHEN we process any event, without TLS configured
    with ctx(
        ctx.on.update_status(),
        state=coordinator_state,
    ) as mgr:
        # THEN we get a sentinel value
        services_out = mgr.run().get_container("nginx-prometheus-exporter").plan.services
        sentinel_no_tls = services_out.get("nginx-prometheus-exporter").environment.get("_reload")

    # AND WHEN we process any event, with TLS configured
    with tls_mock(tmp_path):
        ctx = testing.Context(coordinator_charm, meta=coordinator_charm.META)
        with patch(
            "coordinated_workers.coordinator.NginxPrometheusExporter.are_certificates_on_disk",
            PropertyMock(return_value=True),
        ):
            state_out = ctx.run(
                ctx.on.update_status(),
                state=coordinator_state,
            )

        # THEN we get a different sentinel value
        services_out = state_out.get_container("nginx-prometheus-exporter").plan.services
        sentinel_tls = services_out.get("nginx-prometheus-exporter").environment.get("_reload")
        assert sentinel_no_tls != sentinel_tls
