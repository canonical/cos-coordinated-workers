"""Integration test for the nginx-prometheus-exporter metrics endpoint."""

import logging
import ssl
from urllib.request import urlopen

import jubilant
from helpers import PackedCharm, deploy_coordinated_worker_solution
from jubilant import Juju, all_active
from tenacity import retry, stop_after_delay, wait_exponential

logger = logging.getLogger(__name__)

COORDINATOR_NAME = "coordinator"
WORKER_A_NAME = "worker-a"
WORKER_B_NAME = "worker-b"
SELF_SIGNED_CERTS_NAME = "self-signed-certificates"

NGINX_EXPORTER_PORT = 9113

@retry(
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_delay(120),
    reraise=True,
)
def check_https_metrics(url: str, ssl_ctx: ssl.SSLContext) -> None:
    logger.info(f"Checking nginx-prometheus-exporter metrics at {url}")
    response = urlopen(url, context=ssl_ctx, timeout=5.0)
    assert response.code == 200, f"{url} was not reachable"
    assert "# HELP " in response.read().decode(), (
        f"{url} did not return expected Prometheus metrics"
    )


def test_deploy(juju: Juju, coordinator_charm: PackedCharm, worker_charm: PackedCharm):
    """Deploy the coordinator and two workers."""
    deploy_coordinated_worker_solution(
        juju,
        coordinator_charm,
        COORDINATOR_NAME,
        worker_charm,
        WORKER_A_NAME,
        WORKER_B_NAME,
    )
    juju.wait(jubilant.all_active, timeout=300, error=jubilant.any_error)


def test_metrics_without_tls(juju: Juju):
    """Test that the nginx-prometheus-exporter metrics endpoint is accessible over HTTP."""
    # NOTE: since we do not `set_ports` in the lib, we need to use the unit IP
    coord_unit_ip = juju.status().apps[COORDINATOR_NAME].units[f"{COORDINATOR_NAME}/0"].address
    url = f"http://{coord_unit_ip}:{NGINX_EXPORTER_PORT}/metrics"

    logger.info(f"Checking nginx-prometheus-exporter metrics at {url}")
    response = urlopen(url, timeout=5.0)

    assert response.code == 200, f"{url} was not reachable"
    assert "# HELP " in response.read().decode(), f"{url} did not return expected Prometheus metrics"


def test_deploy_tls(juju: Juju):
    """Deploy self-signed-certificates and integrate with the coordinator to enable TLS."""
    juju.deploy(
        "self-signed-certificates",
        app=SELF_SIGNED_CERTS_NAME,
        channel="1/stable",
        trust=True,
    )
    juju.wait(lambda status: all_active(status, SELF_SIGNED_CERTS_NAME), timeout=120)

    juju.integrate(f"{COORDINATOR_NAME}:certificates", SELF_SIGNED_CERTS_NAME)

    juju.wait(
        lambda status: all_active(status, COORDINATOR_NAME, WORKER_A_NAME, WORKER_B_NAME),
        timeout=300,
        error=jubilant.any_error,
    )


def test_metrics_with_tls(juju: Juju):
    """Test that the nginx-prometheus-exporter metrics endpoint is accessible over HTTPS."""
    coord_unit_ip = juju.status().apps[COORDINATOR_NAME].units[f"{COORDINATOR_NAME}/0"].address
    url = f"https://{coord_unit_ip}:{NGINX_EXPORTER_PORT}/metrics"

    # Disable certificate verification since the cert is self-signed.
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    check_https_metrics(url, ssl_ctx)
