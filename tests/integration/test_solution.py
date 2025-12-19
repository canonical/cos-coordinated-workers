"""Basic solution-level tests for charms using the Coordinated-Worker package.

These are simple smoke tests to assert a basic deployment of a coordinator and two workers deploys successfully.  More
specific tests than basic function should be covered in other test suites.
"""

from urllib.request import urlopen

import jubilant
from helpers import PackedCharm, deploy_coordinated_worker_solution
from jubilant import Juju

COORDINATOR_NAME = "coordinator"
WORKER_A_NAME = "worker-a"
WORKER_B_NAME = "worker-b"


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
    coord_ip = juju.status().apps["coordinator"].address
    # WHEN querying the metrics endpoint of the coordinator
    # TODO We need to enable coordinator metrics for this test to work
    # url = f"http://{coord_ip}:9113/metrics"

    # AND the metrics endpoint (via the nginx proxy of the coordinator) of the workers
    for worker in [WORKER_A_NAME, WORKER_B_NAME]:
        url = f"http://{coord_ip}:8080/proxy/worker/{worker}-0/metrics"
        response = urlopen(url, timeout=2.0)
        # THEN metrics are successfully returned
        assert response.code == 200, f"{url} was not reachable"
        assert 'version{version="' in response.read().decode(), (
            f"{url} did not return expected metrics"
        )
