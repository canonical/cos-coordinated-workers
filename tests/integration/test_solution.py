"""Basic solution-level tests for charms using the Coordinated-Worker package.

These are simple smoke tests to assert a basic deployment of a coordinator and two workers deploys successfully.  More
specific tests than basic function should be covered in other test suites.
"""

import logging
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import jubilant
from helpers import PackedCharm, deploy_coordinated_worker_solution
from jubilant import Juju
from tenacity import retry, stop_after_delay, wait_exponential

COORDINATOR_NAME = "coordinator"
WORKER_A_NAME = "worker-a"
WORKER_B_NAME = "worker-b"

# Generous relative to a healthy response, but a unit that just settled may still be resolving
# DNS or accepting its first connection. nginx itself allows 5s just to connect upstream.
REQUEST_TIMEOUT = 10.0


@retry(
    wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_delay(120), reraise=True
)
def assert_url_contains(url: str, expected: str):
    """Assert a URL returns HTTP 200 and its body contains `expected`, retrying until it does.

    A unit reporting `active` does not guarantee its workload is already serving, so poll
    rather than trusting the first attempt.
    """
    logging.info(f"Checking {url} for {expected!r}")
    try:
        response = urlopen(url, timeout=REQUEST_TIMEOUT)
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        raise AssertionError(f"{url} was not reachable: {e}") from e

    assert response.code == 200, f"{url} was not reachable"
    assert expected in response.read().decode(), f"{url} did not return expected metrics"


def test_deploy(juju: Juju, coordinator_charm: PackedCharm, worker_charm: PackedCharm):
    # GIVEN a coordinator and two workers
    deploy_coordinated_worker_solution(
        juju,
        coordinator_charm,
        COORDINATOR_NAME,
        worker_charm,
        WORKER_A_NAME,
        WORKER_B_NAME,
    )
    juju.wait(jubilant.all_active, timeout=300, error=jubilant.any_error)


def test_metrics(juju: Juju):
    # NOTE: since we do not `set_ports` in the lib, we need to use the unit IP
    coord_unit_ip = juju.status().apps["coordinator"].units["coordinator/0"].address
    # WHEN querying the metrics endpoint of the coordinator
    # THEN metrics are successfully returned
    assert_url_contains(f"http://{coord_unit_ip}:9113/metrics", "# HELP ")

    # AND WHEN querying the metrics endpoint (via the nginx proxy of the coordinator) of the workers
    for worker in [WORKER_A_NAME, WORKER_B_NAME]:
        url = f"http://{coord_unit_ip}:8080/proxy/worker/{worker}-0/metrics"
        # THEN metrics are successfully returned
        assert_url_contains(url, 'version{version="')
