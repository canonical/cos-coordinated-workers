"""Helper functions for integration tests."""

import logging
from dataclasses import asdict, dataclass
from typing import Optional

from jubilant import Juju, all_active, all_blocked


@dataclass
class PackedCharm:
    charm: str  # aka charm name or local path to charm
    resources: Optional[dict] = None


@dataclass
class CharmDeploymentConfiguration:
    charm: str  # aka charm name or local path to charm
    app: str
    channel: str
    trust: bool
    config: Optional[dict] = None


s3_integrator = CharmDeploymentConfiguration(
    charm="s3-integrator",
    app="s3-integrator",
    channel="edge",
    trust=False,
    config={"endpoint": "x.y.z", "bucket": "coordinated-worker"},
)


def deploy_coordinated_worker_solution(
    juju: Juju,
    coordinator_charm: PackedCharm,
    coordinator_name: str,
    worker_charm: PackedCharm,
    worker_a_name: str,
    worker_b_name: str,
):
    logging.info("Deploying coordinator and worker")
    juju.deploy(**asdict(coordinator_charm), app=coordinator_name, trust=True)
    juju.deploy(**asdict(worker_charm), app=worker_a_name, trust=True, config={"role-a": True})
    juju.deploy(**asdict(worker_charm), app=worker_b_name, trust=True, config={"role-b": True})

    logging.info("Waiting for all to settle and be blocked")
    juju.wait(lambda status: all_blocked(status, coordinator_name, worker_a_name, worker_b_name))

    logging.info("Deploying s3-integrator")
    s3_integrator = deploy_s3_integrator(juju)
    juju.integrate(coordinator_name, s3_integrator)

    logging.info("Waiting for s3-integrator to settle and be active")
    juju.wait(
        lambda status: all_active(status, s3_integrator),
    )

    logging.info("Relating coordinator and workers")
    juju.integrate(coordinator_name, worker_a_name)
    juju.integrate(coordinator_name, worker_b_name)

    logging.info("Waiting for all to settle and be active")
    juju.wait(
        lambda status: all_active(status, coordinator_name, worker_a_name, worker_b_name),
        timeout=180,
    )


def deploy_s3_integrator(juju: Juju) -> str:
    """Deploy and configure s3-integrator, returning the deployed application name."""
    logging.info("Deploying s3-integrator")
    juju.deploy(**asdict(s3_integrator))
    juju.wait(lambda status: all_blocked(status, s3_integrator.app))
    logging.info("Configuring s3-integrator with fake credentials")
    juju.run(
        s3_integrator.app + "/0",
        "sync-s3-credentials",
        params={"access-key": "minio123", "secret-key": "minio123"},
    )
    juju.wait(lambda status: all_active(status, s3_integrator.app))
    return s3_integrator.app
