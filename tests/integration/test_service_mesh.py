"""Test the coordinated-worker deploys correctly to a service mesh."""

import lightkube
import pytest
from helpers import PackedCharm, deploy_coordinated_worker_solution
from jubilant import Juju, all_active
from lightkube.resources.core_v1 import Pod
from pytest_jubilant.main import TempModelFactory

COORDINATOR_NAME = "coordinator"
WORKER_A_NAME = "worker-a"
WORKER_B_NAME = "worker-b"
ISTIO_K8S_NAME = "istio-k8s"
ISTIO_BEACON_NAME = "istio-beacon-k8s"


def test_deploy(juju: Juju, coordinator_charm: PackedCharm, worker_charm: PackedCharm):
    deploy_coordinated_worker_solution(
        juju,
        coordinator_charm,
        COORDINATOR_NAME,
        worker_charm,
        WORKER_A_NAME,
        WORKER_B_NAME,
    )


@pytest.fixture(scope="module")
def juju_istio_system(temp_model_factory: TempModelFactory):
    """Return a Juju client configured for the istio-system model, automatically creating that model as needed.

    The model will have the same name as the automatically generated test model, but with the suffix 'istio-system'.
    """
    yield temp_model_factory.get_juju(suffix="istio-system")


def test_deploy_dependency_service_mesh(juju: Juju, juju_istio_system: Juju):
    """Deploy the istio service mesh."""
    juju_istio_system.deploy(
        "istio-k8s",
        app=ISTIO_K8S_NAME,
        channel="2/edge",
        trust=True,
    )

    juju.deploy(
        "istio-beacon-k8s",
        app=ISTIO_BEACON_NAME,
        channel="2/edge",
        trust=True,
    )

    juju_istio_system.wait(
        lambda status: all_active(status, ISTIO_K8S_NAME),
    )

    juju.wait(
        lambda status: all_active(status, ISTIO_BEACON_NAME),
    )


def test_configure_service_mesh(juju: Juju):
    """Configure the coordinated-worker to use the service mesh."""
    juju.integrate(COORDINATOR_NAME, ISTIO_BEACON_NAME)

    juju.wait(
        lambda status: all_active(status, COORDINATOR_NAME, ISTIO_BEACON_NAME),
    )

    # Assert that the Coordinator relation to service mesh worked correctly by checking for expected service mesh labels
    lightkube_client = lightkube.Client()
    coordinator_pod = lightkube_client.get(Pod, f"{COORDINATOR_NAME}-0", namespace=juju.model)
    assert coordinator_pod.metadata.labels["istio.io/dataplane-mode"] == "ambient"
