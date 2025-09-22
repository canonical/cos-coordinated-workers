"""Basic solution-level tests for charms using the Coordinated-Worker package.

These are simple smoke tests to assert a basic deployment of a coordinator and two workers deploys successfully.  More
specific tests than basic function should be covered in other test suites.
"""

from jubilant import Juju

from helpers import PackedCharm, deploy_coordinated_worker_solution

COORDINATOR_NAME = "coordinator"
WORKER_A_NAME = "worker-a"
WORKER_B_NAME = "worker-b"


def test_deploy(juju: Juju, coordinator_charm: PackedCharm, worker_charm: PackedCharm):
    deploy_coordinated_worker_solution(
        juju,
        coordinator_charm,
        COORDINATOR_NAME,
        worker_charm,
        WORKER_A_NAME,
        WORKER_B_NAME,
    )
