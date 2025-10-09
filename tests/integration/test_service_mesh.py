"""Test the coordinated-worker deploys correctly to a service mesh."""

import logging

import lightkube
import pytest
import tenacity
from helpers import (
    PackedCharm,
    assert_request_returns_http_code,
    deploy_coordinated_worker_solution,
)
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
    for app in (COORDINATOR_NAME, WORKER_A_NAME, WORKER_B_NAME):
        for attempt in tenacity.Retrying(
            stop=tenacity.stop_after_delay(30),
            wait=tenacity.wait_fixed(5),
            # if you don't succeed raise the last caught exception when you're done
            reraise=True,
        ):
            with attempt:
                pod_name = f"{app}-0"
                logging.info(
                    f"attempt #{attempt.retry_state.attempt_number} to assert expected service mesh labels on {pod_name}",
                )

                pod = lightkube_client.get(Pod, pod_name, namespace=juju.model)

                # Assert coordinated worker solution labels
                assert pod.metadata.labels["app.kubernetes.io/part-of"] == COORDINATOR_NAME, (
                    f"Pod {pod_name} missing coordinated worker solution label"
                )

                # Assert mesh labels
                logging.info(f"Pod {pod_name} labels: {pod.metadata.labels}")
                assert pod.metadata.labels.get("istio.io/dataplane-mode") == "ambient", (
                    f"Pod {pod_name} missing istio label"
                )


def test_cluster_internal_mesh_policies(juju: Juju, worker_charm: PackedCharm):
    """Test if the cluster internal mesh policies are applied correctly."""
    # deploy a tester that is not in the service mesh for benchmarking.
    out_of_mesh_app = "out-of-mesh-app"
    # juju.deploy(
    #     **asdict(worker_charm),
    #     app=out_of_mesh_app,
    #     trust=True,
    # )
    # juju.wait(lambda status: all_blocked(status, out_of_mesh_app))

    # coordinator can talk to both workers
    assert_request_returns_http_code(
        juju.model,
        f"{COORDINATOR_NAME}/0",
        f"http://{WORKER_A_NAME}-0.{WORKER_A_NAME}-endpoints.{juju.model}.svc.cluster.local:8080/foo",
        code=200,
    )
    assert_request_returns_http_code(
        juju.model,
        f"{COORDINATOR_NAME}/0",
        f"http://{WORKER_B_NAME}-0.{WORKER_B_NAME}-endpoints.{juju.model}.svc.cluster.local:8080/foo",
        code=200,
    )

    # workers can talk to each other
    assert_request_returns_http_code(
        juju.model,
        f"{WORKER_A_NAME}/0",
        f"http://{WORKER_B_NAME}-0.{WORKER_B_NAME}-endpoints.{juju.model}.svc.cluster.local:8080/foo",
        code=200,
    )
    assert_request_returns_http_code(
        juju.model,
        f"{WORKER_B_NAME}/0",
        f"http://{WORKER_A_NAME}-0.{WORKER_A_NAME}-endpoints.{juju.model}.svc.cluster.local:8080/foo",
        code=200,
    )

    # workers can talk to coordinator
    assert_request_returns_http_code(
        juju.model,
        f"{WORKER_A_NAME}/0",
        f"http://{COORDINATOR_NAME}-0.{COORDINATOR_NAME}-endpoints.{juju.model}.svc.cluster.local:8080/",
        code=200,
    )
    assert_request_returns_http_code(
        juju.model,
        f"{WORKER_B_NAME}/0",
        f"http://{COORDINATOR_NAME}-0.{COORDINATOR_NAME}-endpoints.{juju.model}.svc.cluster.local:8080/",
        code=200,
    )

    # out-of-mesh app CANNOT talk to any cluster components (should be blocked by authorization policies)
    assert_request_returns_http_code(
        juju.model,
        f"{out_of_mesh_app}/0",
        f"http://{COORDINATOR_NAME}-0.{COORDINATOR_NAME}-endpoints.{juju.model}.svc.cluster.local:8080/foo",
        code=1,
    )
    assert_request_returns_http_code(
        juju.model,
        f"{out_of_mesh_app}/0",
        f"http://{WORKER_A_NAME}-0.{WORKER_A_NAME}-endpoints.{juju.model}.svc.cluster.local:8080/foo",
        code=1,
    )
    assert_request_returns_http_code(
        juju.model,
        f"{out_of_mesh_app}/0",
        f"http://{WORKER_B_NAME}-0.{WORKER_B_NAME}-endpoints.{juju.model}.svc.cluster.local:8080/foo",
        code=1,
    )
