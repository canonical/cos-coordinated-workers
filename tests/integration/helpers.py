"""Helper functions for integration tests."""

import logging
from dataclasses import asdict, dataclass
from typing import Optional

import sh
from jubilant import Juju, all_active, all_blocked
from tenacity import (
    retry,
    stop_after_delay,
    wait_exponential,
)


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


@retry(
    wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_delay(120), reraise=True
)
def assert_request_returns_http_code(
    model: str, source_unit: str, target_url: str, method: str = "get", code: int = 200
):
    """Get the status code for a request from a source unit to a target URL on a given method.

    Note that if the request fails (ex: python script raises an exception) the exit code will be returned.
    """
    logging.info(f"Checking {source_unit} -> {target_url} on {method}")
    try:
        resp = sh.juju.ssh(  # pyright: ignore
            "-m",
            model,
            source_unit,
            f'curl -X {method.upper()} -s -o /dev/null -w "%{{http_code}}" {target_url}',
            _return_cmd=True,
        )
        returned_code = int(str(resp).strip())
    except sh.ErrorReturnCode as e:
        logging.warning(f"Got exit code {e.exit_code} executing sh.juju.ssh")
        logging.warning(f"STDOUT: {e.stdout}")
        logging.warning(f"STDERR: {e.stderr}")
        returned_code = e.exit_code

    logging.info(
        f"Got {returned_code} for {source_unit} -> {target_url} on {method} - expected {code}"
    )

    assert returned_code == code, (
        f"Expected {code} but got {returned_code} for {source_unit} -> {target_url} on {method}"
    )
