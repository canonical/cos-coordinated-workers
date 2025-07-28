import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Tuple
from unittest.mock import patch

import ops
import pytest
from ops import testing

from coordinated_workers.nginx import (
    CA_CERT_PATH,
    CERT_PATH,
    KEY_PATH,
    NGINX_CONFIG,
    Nginx,
    NginxConfig,
    NginxLocationConfig,
    NginxUpstream,
)

sample_dns_ip = "198.18.0.0"

logger = logging.getLogger(__name__)


@pytest.fixture
def certificate_mounts():
    temp_files = {}
    for path in {KEY_PATH, CERT_PATH, CA_CERT_PATH}:
        temp_file = tempfile.NamedTemporaryFile(delete=False)
        temp_files[path] = temp_file

    mounts = {}
    for cert_path, temp_file in temp_files.items():
        mounts[cert_path] = testing.Mount(location=cert_path, source=temp_file.name)

    # TODO: Do we need to clean up the temp files since delete=False was set?
    return mounts


@pytest.fixture
def nginx_context():
    return testing.Context(
        ops.CharmBase, meta={"name": "foo", "containers": {"nginx": {"type": "oci-image"}}}
    )


def test_certs_on_disk(certificate_mounts: dict, nginx_context: testing.Context):
    # GIVEN any charm with a container
    ctx = nginx_context

    # WHEN we process any event
    with ctx(
        ctx.on.update_status(),
        state=testing.State(
            containers={testing.Container("nginx", can_connect=True, mounts=certificate_mounts)}
        ),
    ) as mgr:
        charm = mgr.charm
        nginx = Nginx(charm, lambda: "foo_string", None)

        # THEN the certs exist on disk
        assert nginx.are_certificates_on_disk


def test_certs_deleted(certificate_mounts: dict, nginx_context: testing.Context):
    # Test deleting the certificates.

    # GIVEN any charm with a container
    ctx = nginx_context

    # WHEN we process any event
    with ctx(
        ctx.on.update_status(),
        state=testing.State(
            containers={testing.Container("nginx", can_connect=True, mounts=certificate_mounts)}
        ),
    ) as mgr:
        charm = mgr.charm
        nginx = Nginx(charm, lambda: "foo_string", None)

        # AND when we call delete_certificates
        nginx._delete_certificates()

        # THEN the certs get deleted from disk
        assert not nginx.are_certificates_on_disk


def test_has_config_changed(nginx_context: testing.Context):
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
                testing.Container(
                    "nginx",
                    can_connect=True,
                    mounts={
                        "config": testing.Mount(location=NGINX_CONFIG, source=test_config.name)
                    },
                )
            },
        ),
    ) as mgr:
        charm = mgr.charm
        nginx = Nginx(charm, lambda: "foo_string", None)

        # AND a unique config is added
        new_config = "bar"

        # THEN the _has_config_changed method correctly determines that foo != bar
        assert nginx._has_config_changed(new_config)


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
}


def _get_nginx_config_params(workload: str) -> Tuple[list, dict]:
    return upstream_configs[workload], server_ports_to_locations[workload]
