#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Generic worker for a distributed charm deployment."""

import logging
import re
import socket
import subprocess
import urllib.request
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict, Union
from urllib.error import HTTPError

import ops
import ops_tracing
import tenacity
import yaml
from cosl import JujuTopology
from ops import MaintenanceStatus, StatusBase
from ops.model import ActiveStatus, BlockedStatus, ModelError, WaitingStatus
from ops.pebble import Check, Layer, PathError, Plan, ProtocolError

from coordinated_workers.helpers import check_libs_installed
from coordinated_workers.interfaces.cluster import ClusterRequirer, TLSData

check_libs_installed(
    "charms.loki_k8s.v1.loki_push_api",
    "charms.observability_libs.v0.kubernetes_compute_resources_patch",
)

from charms.loki_k8s.v1.loki_push_api import _PebbleLogClient  # type: ignore
from charms.observability_libs.v0.kubernetes_compute_resources_patch import (
    KubernetesComputeResourcesPatch,
    adjust_resource_requirements,
)
from lightkube.models.core_v1 import ResourceRequirements

BASE_DIR = "/worker"
CONFIG_FILE = "/etc/worker/config.yaml"
CERT_FILE = "/etc/worker/server.cert"
S3_TLS_CA_CHAIN_FILE = "/etc/worker/s3_ca.crt"
KEY_FILE = "/etc/worker/private.key"
CLIENT_CA_FILE = "/etc/worker/ca.cert"
ROOT_CA_CERT = "/usr/local/share/ca-certificates/ca.crt"
ROOT_CA_CERT_PATH = Path(ROOT_CA_CERT)

logger = logging.getLogger(__name__)


def _validate_container_name(
    container_name: Optional[str],
    resources_requests: Optional[Callable[["Worker"], Dict[str, str]]],
):
    """Raise `ValueError` if `resources_requests` is not None and `container_name` is None."""
    if resources_requests is not None and container_name is None:
        raise ValueError(
            "Cannot have a None value for container_name while resources_requests is provided."
        )


_EndpointMapping = TypedDict("_EndpointMapping", {"cluster": str}, total=True)
"""Mapping of the relation endpoint names that the charms uses, as defined in metadata.yaml."""

_ResourceLimitOptionsMapping = TypedDict(
    "_ResourceLimitOptionsMapping",
    {
        "cpu_limit": str,
        "memory_limit": str,
    },
)
"""Mapping of the resources limit option names that the charms use, as defined in config.yaml."""


class WorkerError(Exception):
    """Base class for exceptions raised by this module."""


class NoReadinessCheckEndpointConfiguredError(Exception):
    """Internal error when readiness check endpoint is missing."""


class ServiceEndpointStatus(Enum):
    """Status of the worker service managed by pebble."""

    starting = "starting"
    up = "up"
    down = "down"


class Worker(ops.Object):
    """Charming worker."""

    # configuration for the service start retry logic in .restart().
    # this will determine how long we wait for pebble to try to start the worker process
    SERVICE_START_RETRY_STOP = tenacity.stop_after_delay(60 * 15)
    SERVICE_START_RETRY_WAIT = tenacity.wait_fixed(60)
    SERVICE_START_RETRY_IF = tenacity.retry_if_exception_type(ops.pebble.ChangeError)

    # configuration for the service status retry logic after a restart has occurred.
    # this will determine how long we wait for the worker process to report "ready" after it
    # has been successfully restarted
    SERVICE_STATUS_UP_RETRY_STOP = tenacity.stop_after_delay(60 * 15)
    SERVICE_STATUS_UP_RETRY_WAIT = tenacity.wait_fixed(10)
    SERVICE_STATUS_UP_RETRY_IF = tenacity.retry_if_not_result(bool)

    _endpoints: _EndpointMapping = {
        "cluster": "cluster",
    }

    def __init__(
        self,
        charm: ops.CharmBase,
        name: str,
        pebble_layer: Callable[["Worker"], Layer],
        endpoints: _EndpointMapping,
        readiness_check_endpoint: Optional[Union[str, Callable[["Worker"], str]]] = None,
        resources_limit_options: Optional[_ResourceLimitOptionsMapping] = None,
        resources_requests: Optional[Callable[["Worker"], Dict[str, str]]] = None,
        container_name: Optional[str] = None,
    ):
        """Constructor for a Worker object.

        Args:
            charm: The worker charm object.
            name: The name of the workload container.
            pebble_layer: The pebble layer of the workload.
            endpoints: Endpoint names for coordinator relations, as defined in metadata.yaml.
            readiness_check_endpoint: URL to probe with a pebble check to determine
                whether the worker node is ready. Passing None will effectively disable it.
            resources_limit_options: A dictionary containing resources limit option names. The dictionary should include
                "cpu_limit" and "memory_limit" keys with values as option names, as defined in the config.yaml.
                If no dictionary is provided, the default option names "cpu_limit" and "memory_limit" would be used.
            resources_requests: A function generating the resources "requests" portion to apply when patching a container using
                KubernetesComputeResourcesPatch. The "limits" portion of the patch gets populated by setting
                their respective config options in config.yaml.
            container_name: The container for which to apply the resources requests & limits.
                Required if `resources_requests` is provided.

        Raises:
        ValueError:
            If `resources_requests` is not None and `container_name` is None, a ValueError is raised.
        """
        super().__init__(charm, key="worker")
        self._charm = charm
        self._name = name
        self._pebble_layer = partial(
            pebble_layer, self
        )  # do not call this directly. use self.pebble_layer instead
        self.topology = JujuTopology.from_charm(self._charm)
        self._container = self._charm.unit.get_container(name)

        self._endpoints = endpoints
        _validate_container_name(container_name, resources_requests)

        # turn str to Callable[[Worker], str]
        self._readiness_check_endpoint: Optional[Callable[[Worker], str]]
        if isinstance(readiness_check_endpoint, str):
            self._readiness_check_endpoint = lambda _: readiness_check_endpoint
        else:
            self._readiness_check_endpoint = readiness_check_endpoint
        self._resources_requests_getter = (
            partial(resources_requests, self) if resources_requests is not None else None
        )
        self._container_name = container_name
        self._resources_limit_options = resources_limit_options or {}

        self.cluster = ClusterRequirer(
            charm=self._charm,
            endpoint=self._endpoints["cluster"],
        )

        self._log_forwarder = ManualLogForwarder(
            charm=self._charm,
            loki_endpoints=self.cluster.get_loki_endpoints(),
            refresh_events=[
                self.cluster.on.config_received,
                self.cluster.on.created,
                self.cluster.on.removed,
            ],
        )

        # Resources patch
        self.resources_patch = (
            KubernetesComputeResourcesPatch(
                self._charm,
                self._container_name,  # type: ignore
                resource_reqs_func=self._adjust_resource_requirements,
            )
            if self._resources_requests_getter
            else None
        )
        # holistic update logic, aka common exit hook
        self._reconcile()

        # Event listeners
        self.framework.observe(self._charm.on.collect_unit_status, self._on_collect_status)
        self.framework.observe(self.cluster.on.removed, self._log_forwarder.disable_logging)

        self.framework.observe(self._charm.on[self._name].pebble_ready, self._on_pebble_ready)
        self.framework.observe(
            self._charm.on[name].pebble_check_failed, self._on_pebble_check_failed
        )
        self.framework.observe(
            self._charm.on[name].pebble_check_recovered, self._on_pebble_check_recovered
        )

    # Event handlers
    def _on_pebble_ready(self, _: ops.PebbleReadyEvent):
        self._charm.unit.set_workload_version(self.running_version() or "")

    def _on_pebble_check_failed(self, event: ops.PebbleCheckFailedEvent):
        if event.info.name == "ready":
            logger.warning("Pebble `ready` check started to fail: worker node is down.")
            # collect-status will detect that we're not ready and set waiting status.

    def _on_pebble_check_recovered(self, event: ops.PebbleCheckFailedEvent):
        if event.info.name == "ready":
            logger.info("Pebble `ready` check is now passing: worker node is up.")
            # collect-status will detect that we're ready and set active status.

    @property
    def _worker_config(self):
        """The configuration that this worker should run with, as received from the coordinator.

        Charms that wish to modify their config before it's written to disk by the Worker
        should subclass the worker, override this method, and use it to manipulate the
        config that's presented to the Worker.
        """
        return self.cluster.get_worker_config()

    @property
    def pebble_layer(self) -> Optional[Layer]:
        """Attempt to fetch a pebble layer from the charm.

        If the charm raises, report the exception and return None.
        """
        try:
            return self._pebble_layer()
        except Exception:
            logger.exception("exception while attempting to get pebble layer from charm")
            return None

    @property
    def status(self) -> ServiceEndpointStatus:
        """Determine the status of the service's endpoint."""
        if not self._container.can_connect():
            logger.debug("Container cannot connect. Skipping status check.")
            return ServiceEndpointStatus.down

        if not self._running_worker_config():
            logger.debug("Config file not on disk. Skipping status check.")
            return ServiceEndpointStatus.down

        if not (layer := self.pebble_layer):
            return ServiceEndpointStatus.down

        # we really don't want this code to raise errors, so we blanket catch all.
        try:
            services = self._container.get_services(*layer.services.keys())
            if not services:
                logger.debug("No services found in pebble plan.")
            else:
                services_not_running = [
                    name for name, svc in services.items() if not svc.is_running()
                ]
                if services_not_running:
                    logger.debug(
                        f"Some services which should be running are not: {services_not_running}."
                    )
                    return ServiceEndpointStatus.down
                else:
                    logger.debug("All pebble services up.")

            # so far as pebble knows all services are up, now let's see if
            # the readiness endpoint confirm that
            return self.check_readiness()

        except NoReadinessCheckEndpointConfiguredError:
            # assume up
            return ServiceEndpointStatus.up

        except Exception:
            logger.exception(
                "Unexpected error while getting worker status. "
                "This could mean that the worker is still starting."
            )
            return ServiceEndpointStatus.down

    def check_readiness(self) -> ServiceEndpointStatus:
        """If the user has configured a readiness check endpoint, GET it and check the workload status."""
        check_endpoint = self._readiness_check_endpoint
        if not check_endpoint:
            raise NoReadinessCheckEndpointConfiguredError()

        try:
            with urllib.request.urlopen(check_endpoint(self)) as response:
                html: bytes = response.read()

            # ready response should simply be a string:
            #   "ready"
            raw_out = html.decode("utf-8").strip()
            if raw_out == "ready":
                return ServiceEndpointStatus.up

            # depending on the workload, we get something like:
            #   Some services are not Running:
            #   Starting: 1
            #   Running: 16
            # (tempo)
            #   Ingester not ready: waiting for 15s after being ready
            # (mimir)

            # anything that isn't 'ready' but also is a 2xx response will be interpreted as:
            # we're not ready yet, but we're working on it.
            logger.debug(f"GET {check_endpoint} returned: {raw_out!r}.")
            return ServiceEndpointStatus.starting

        except HTTPError as e:
            logger.debug(f"Error getting readiness endpoint, server not up (yet): {e}")
        except ConnectionResetError as e:
            logger.warning(
                f"Error getting readiness endpoint (check the workload container logs for details): {e}"
            )
        except Exception as e:
            logger.exception(f"Unexpected exception getting readiness endpoint: {e}")

        return ServiceEndpointStatus.down

    def _on_collect_status(self, e: ops.CollectStatusEvent):
        # these are the basic failure modes. if any of these conditions are not met, the worker
        # is still starting or not yet configured. The user needs to wait or take some action.
        statuses: List[StatusBase] = []
        if self.resources_patch and self.resources_patch.get_status().name != "active":
            statuses.append(self.resources_patch.get_status())
        if not self._container.can_connect():
            statuses.append(WaitingStatus(f"Waiting for `{self._name}` container"))
        if not self.model.get_relation(self._endpoints["cluster"]):
            statuses.append(BlockedStatus("Missing relation to a coordinator charm"))
        elif not self.cluster.relation:
            statuses.append(WaitingStatus("Cluster relation not ready"))
        if not self._worker_config or not self._running_worker_config():
            statuses.append(WaitingStatus("Waiting for coordinator to publish a config"))
        if not self.roles:
            statuses.append(
                BlockedStatus("Invalid or no roles assigned: please configure some valid roles")
            )
        if self._tls_misconfigured:
            statuses.append(
                BlockedStatus(
                    "TLS misconfigured: the ingressed endpoints are https, "
                    "but the coordinator didn't give us a certificate (yet)."
                )
            )

        # if none of the conditions above applies, the worker should in principle be either up or starting
        if not statuses:
            try:
                status = self.status
                if status == ServiceEndpointStatus.starting:
                    statuses.append(WaitingStatus("Starting..."))
                elif status == ServiceEndpointStatus.down:
                    logger.error(
                        "The worker service appears to be down and we don't know why. "
                        "Please check the pebble services' status and their logs."
                    )
                    statuses.append(BlockedStatus("node down (see logs)"))
            except WorkerError:
                # this means that the node is not down for any obvious reason (no container,...)
                # but we still can't know for sure that the node is up, because we don't have
                # a readiness endpoint configured.
                logger.debug(
                    "Unable to determine worker readiness: no endpoint given. "
                    "This means we're going to report active, but the node might still "
                    "be coming up and not ready to serve."
                )

        # if still there are no statuses, we report we're all ready
        if not statuses:
            statuses.append(
                ActiveStatus(
                    "(all roles) ready."
                    if ",".join(self.roles) == "all"
                    else f"{','.join(self.roles)} ready."
                )
            )

        # report all applicable statuses to the model
        for status in statuses:
            e.add_status(status)

    # Utility functions
    @property
    def roles(self) -> List[str]:
        """Return a list of the roles this worker should take on.

        Expects that the charm defines a set of roles by config like:
            "role-a": bool
            "role-b": bool
            "role-b": bool
        If this is not the case, it will raise an error.
        """
        config = self._charm.config

        role_config_options = [option for option in config.keys() if option.startswith("role-")]
        if not role_config_options:
            raise WorkerError(
                "The charm should define a set of `role-X` config "
                "options for it to use the Worker."
            )

        active_roles: List[str] = [
            role[5:] for role in role_config_options if config[role] is True
        ]
        return active_roles

    def _update_config(self) -> bool:
        """Update the worker config and restart the workload if necessary."""
        return any(
            (
                self._update_tls_certificates(),
                self._update_worker_config(),
                self._set_pebble_layer(),
            )
        )

    def _set_pebble_layer(self) -> bool:
        """Set Pebble layer.

        Assumes that the caller has verified that the worker is ready, i.e.
        that we have a container and a cluster configuration.

        Returns: True if Pebble layer was added, otherwise False.
        """
        current_plan = self._container.get_plan()
        if not (layer := self.pebble_layer):
            return False

        self._add_readiness_check(layer)

        def diff(layer: Layer, plan: Plan):
            layer_dct = layer.to_dict()
            plan_dct = plan.to_dict()
            for key in ["checks", "services"]:
                if layer_dct.get(key) != plan_dct.get(key):
                    return True
            return False

        if diff(layer, current_plan):
            logger.debug("Adding new layer to pebble...")
            self._container.add_layer(self._name, layer, combine=True)
            return True
        return False

    def _add_readiness_check(self, new_layer: Layer):
        """Add readiness check to a pebble layer."""
        if not self._readiness_check_endpoint:
            # skip
            return

        new_layer.checks["ready"] = Check(
            "ready",
            {
                "override": "replace",
                # threshold gets added automatically by pebble
                "threshold": 3,
                "http": {"url": self._readiness_check_endpoint(self)},
            },
        )

    def _reconcile(self):
        """Run all logic that is independent of what event we're processing."""
        # There could be a race between the resource patch and pebble operations
        # i.e., charm code proceeds beyond a can_connect guard, and then lightkube patches the statefulset
        # and the workload is no longer available
        # `resources_patch` might be `None` when no resources requests or limits are requested by the charm.
        if self.resources_patch and not self.resources_patch.is_ready():
            logger.debug("Resource patch not ready yet. Skipping reconciliation step.")
            return

        self._update_cluster_relation()
        self._setup_charm_tracing()

        if self.is_ready():
            logger.debug("Worker ready. Updating config...")
            if worker_ports := self.cluster.get_worker_ports():
                logger.debug(f"opening ports {worker_ports} as received from the coordinator.")
                self._charm.unit.set_ports(*worker_ports)

            # we restart in 2 situations:
            # - we need to because our config has changed
            # - some services are not running
            configs_changed = self._update_config()
            success = None
            if configs_changed:
                logger.debug("Config changed. Restarting worker services...")
                success = self.restart()

            elif services_down := self._get_services_down():
                logger.debug(f"Some services are down: {services_down}. Restarting worker...")
                success = self.restart()

            if success is False:
                # this means that we have managed to start the process without pebble errors,
                # but somehow the status is still not "up" after 15m
                # we are going to set blocked status, but we can also log it here
                logger.warning("failed to (re)start the worker services")

        else:
            logger.debug("Worker not ready. Tearing down...")

            if self._container.can_connect():
                logger.debug("Wiping configs and stopping workload...")
                self._wipe_configs()
                self.stop()

            else:
                logger.debug("Container offline: nothing to teardown.")

    def _get_services_down(self) -> List[str]:
        # this can happen if s3 wasn't ready (server gave error) when we processed an earlier event
        # causing the worker service to die on startup (exited quickly with code...)
        # so we try to restart it now.
        # TODO: would be nice if we could be notified of when s3 starts working, so we don't have to
        #  wait for an update-status and can listen to that instead.
        return [
            svc.name for svc in self._container.get_services().values() if not svc.is_running()
        ]

    def _wipe_configs(self):
        """Delete all configuration files on disk, purely for hygiene."""
        for config_file in (
            KEY_FILE,
            CLIENT_CA_FILE,
            CERT_FILE,
            S3_TLS_CA_CHAIN_FILE,
            ROOT_CA_CERT,
            CONFIG_FILE,
        ):
            self._container.remove_path(config_file, recursive=True)

        logger.debug("wiped all configs")

    def stop(self):
        """Stop the workload and tell pebble to not restart it.

        Assumes that pebble can connect.
        """
        container_services = tuple(self._container.get_plan().services)
        if not container_services:
            logger.warning("nothing to stop: no services found in plan")
            return
        self._container.stop(*container_services)

    def _update_cluster_relation(self) -> None:
        """Publish all the worker information to relation data."""
        self.cluster.publish_unit_address(socket.getfqdn())
        if self._charm.unit.is_leader() and self.roles:
            logger.info(f"publishing roles: {self.roles}")
            try:
                self.cluster.publish_app_roles(self.roles)
            except ModelError as e:
                # if we are handling an event prior to 'install', we could be denied write access
                # Swallowing the exception here relies on the reconciler pattern - this will be
                # retried at the next occasion and eventually that'll be after 'install'.
                if "ERROR permission denied (unauthorized access)" in e.args:
                    logger.debug(
                        "relation-set failed with a permission denied error. "
                        "This could be a transient issue."
                    )
                else:
                    # let it burn, we clearly don't know what's going on
                    raise

    def _running_worker_config(self) -> Optional[Dict[str, Any]]:
        """Return the worker config as dict, or None if retrieval failed."""
        if not self._container.can_connect():
            logger.debug("Could not connect to the workload container")
            return None

        try:
            raw_current = self._container.pull(CONFIG_FILE).read()
            return yaml.safe_load(raw_current)
        except (ProtocolError, PathError) as e:
            logger.warning(
                "Could not check the current worker configuration due to "
                "a failure in retrieving the file: %s",
                e,
            )
            return None

    def is_ready(self) -> bool:
        """Check whether the worker has all data it needs to operate."""
        if not self._container.can_connect():
            logger.warning("worker not ready: container cannot connect.")
            return False

        elif len(self.roles) == 0:
            logger.warning("worker not ready: role missing or misconfigured.")
            return False

        elif not self._worker_config:
            logger.warning("worker not ready: coordinator hasn't published a config")
            return False

        else:
            return True

    def _update_worker_config(self) -> bool:
        """Set worker config for the workload.

        Assumes that the caller has verified that the worker is ready, i.e.
        that we have a container and a cluster configuration.

        Returns: True if config has changed, otherwise False.
        Raises: BlockedStatusError exception if PebbleError, ProtocolError, PathError exceptions
            are raised by container.remove_path
        """
        # fetch the config from the coordinator
        worker_config = self._worker_config
        # and compare it against the one on disk (if any)
        if self._running_worker_config() != worker_config:
            config_as_yaml = yaml.safe_dump(worker_config)
            self._container.push(CONFIG_FILE, config_as_yaml, make_dirs=True)
            logger.info("Pushed new worker configuration")
            return True

        return False

    def _sync_tls_files(self, tls_data: TLSData):
        logger.debug("tls config in cluster. writing to container...")
        if tls_data.privkey_secret_id:
            private_key_secret = self.model.get_secret(id=tls_data.privkey_secret_id)
            private_key = private_key_secret.get_content().get("private-key")
        else:
            private_key = None

        new_contents: Optional[str]
        any_changes = False
        for new_contents, file in (
            (tls_data.ca_cert, CLIENT_CA_FILE),
            (tls_data.server_cert, CERT_FILE),
            (private_key, KEY_FILE),
            (tls_data.s3_tls_ca_chain, S3_TLS_CA_CHAIN_FILE),
            (tls_data.ca_cert, ROOT_CA_CERT),
        ):
            if not new_contents:
                if self._container.exists(file):
                    any_changes = True
                    self._container.remove_path(file, recursive=True)
                    logger.debug(f"{file} deleted")
                    continue

                logger.debug(f"{file} skipped")
                continue

            if self._container.exists(file):
                current_contents = self._container.pull(file).read()
                if current_contents == new_contents:
                    logger.debug(f"{file} unchanged")
                    continue

            logger.debug(f"{file} updated")
            any_changes = True
            self._container.push(file, new_contents, make_dirs=True)

        # Save the cacert in the charm container for charm traces
        # we do it unconditionally to avoid the extra complexity.
        if tls_data.ca_cert:
            ROOT_CA_CERT_PATH.write_text(tls_data.ca_cert)
        else:
            ROOT_CA_CERT_PATH.unlink(missing_ok=True)
        return any_changes

    def _update_tls_certificates(self) -> bool:
        """Update the TLS certificates on disk according to their availability.

        Assumes that the caller has verified that the worker is ready, i.e.
        that we have a container and a cluster configuration.

        Return True if we need to restart the workload after this update.
        """
        tls_data = self.cluster.get_tls_data(allow_none=True)
        if not tls_data:
            return False

        any_changes = self._sync_tls_files(tls_data)
        if any_changes:
            logger.debug("running update-ca-certificates")
            self._container.exec(["update-ca-certificates", "--fresh"]).wait()
            subprocess.run(["update-ca-certificates", "--fresh"])
        return any_changes

    def restart(self):
        """Restart the pebble service or start it if not already running, then wait for it to become ready.

        Default timeout is 15 minutes. Configure it by setting this class attr:
        >>> Worker.SERVICE_START_RETRY_STOP = tenacity.stop_after_delay(60 * 30)  # 30 minutes
        You can also configure SERVICE_START_RETRY_WAIT and SERVICE_START_RETRY_IF.

        This method will raise an exception if it fails to start the service within a
        specified timeframe. This will presumably bring the charm in error status, so
        that juju will retry the last emitted hook until it finally succeeds.

        The assumption is that the state we are in when this method is called is consistent.
        The reason why we're failing to restart is dependent on some external factor (such as network,
        the reachability of a remote API, or the readiness of an external service the workload depends on).
        So letting juju retry the same hook will get us unstuck as soon as that contingency is resolved.

        See https://discourse.charmhub.io/t/its-probably-ok-for-a-unit-to-go-into-error-state/13022

        Raises:
            ChangeError, after continuously failing to restart the service.
        """
        if not self._container.exists(CONFIG_FILE):
            logger.error("cannot restart worker: config file doesn't exist (yet).")
            return
        if not self.roles:
            logger.debug("cannot restart worker: no roles have been configured.")
            return
        if not (layer := self.pebble_layer):
            return
        service_names = layer.services.keys()

        try:
            for attempt in tenacity.Retrying(
                # this method may fail with ChangeError (exited quickly with code...)
                retry=self.SERVICE_START_RETRY_IF,
                # give this method some time to pass (by default 15 minutes)
                stop=self.SERVICE_START_RETRY_STOP,
                # wait 1 minute between tries
                wait=self.SERVICE_START_RETRY_WAIT,
                # if you don't succeed raise the last caught exception when you're done
                reraise=True,
            ):
                with attempt:
                    self._charm.unit.status = MaintenanceStatus(
                        f"restarting... (attempt #{attempt.retry_state.attempt_number})"
                    )
                    # restart all services that our layer is responsible for
                    self._container.restart(*service_names)

        except ops.pebble.ConnectionError:
            logger.debug(
                "failed to (re)start worker jobs because the container unexpectedly died; "
                "this might mean the unit is still settling after deploy or an upgrade. "
                "This should resolve itself."
                # or it's a juju bug^TM
            )
            return False

        except ops.pebble.ChangeError:
            logger.error(
                "failed to (re)start worker jobs. This usually means that an external resource (such as s3) "
                "that the software needs to start is not available."
            )
            raise

        except Exception:
            logger.exception("failed to (re)start worker jobs due to an unexpected error.")
            raise

        try:
            for attempt in tenacity.Retrying(
                # status may report .down
                retry=self.SERVICE_STATUS_UP_RETRY_IF,
                # give this method some time to pass (by default 15 minutes)
                stop=self.SERVICE_STATUS_UP_RETRY_STOP,
                # wait 10 seconds between tries
                wait=self.SERVICE_STATUS_UP_RETRY_WAIT,
                # if you don't succeed raise the last caught exception when you're done
                reraise=True,
            ):
                with attempt:
                    self._charm.unit.status = MaintenanceStatus(
                        f"waiting for worker process to report ready... (attempt #{attempt.retry_state.attempt_number})"
                    )
                # set result to status; will retry unless it's up
                attempt.retry_state.set_result(self.status is ServiceEndpointStatus.up)

        except NoReadinessCheckEndpointConfiguredError:
            # collect_unit_status will surface this to the user
            logger.warning(
                "could not check worker service readiness: no check endpoint configured. "
                "Pass one to the Worker."
            )
            return True

        except Exception:
            logger.exception("unexpected error while attempting to determine worker status")

        return self.status is ServiceEndpointStatus.up

    def running_version(self) -> Optional[str]:
        """Get the running version from the worker process."""
        if not self._container.can_connect():
            return None

        version_output, _ = self._container.exec([f"/bin/{self._name}", "-version"]).wait_output()
        # Output looks like this:
        # <WORKLOAD_NAME>, version 2.4.0 (branch: HEAD, revision 32137ee...)
        if result := re.search(r"[Vv]ersion:?\s*(\S+)", version_output):
            return result.group(1)
        return None

    @property
    def _tls_misconfigured(self) -> bool:
        """Helper method to determine if tls is misconfigured."""
        # misconfiguration: the ingress has a tls relation, but we (the coordinator) don't:
        # as a consequence, we obtain **https** receiver endpoints,
        # but we don't have a server certificate.
        receiver_endpoints = {
            *self.cluster.get_workload_tracing_receivers().values(),
            *self.cluster.get_charm_tracing_receivers().values(),
        }
        any_receiver_endpoint_is_https = any(
            ep.startswith("https://") for ep in receiver_endpoints
        )

        has_server_cert = bool(getattr(self.cluster.get_tls_data(), "server_cert", None))
        if any_receiver_endpoint_is_https and not has_server_cert:
            return True
        return False

    def charm_tracing_config(self) -> Tuple[Optional[str], Optional[str]]:
        """Get the charm tracing configuration from the coordinator."""
        endpoint = self.cluster.get_charm_tracing_receivers().get("otlp_http")

        if not endpoint:
            return None, None

        tls_data = self.cluster.get_tls_data()
        server_ca_cert = tls_data.server_cert if tls_data else None

        if endpoint.startswith("https://"):
            if server_ca_cert is None:
                # https endpoint, but we don't have a cert ourselves:
                # disable charm tracing as it would fail to flush.
                return None, None

            elif not ROOT_CA_CERT_PATH.exists():
                # if endpoint is https and we have a tls integration BUT we don't have the
                # server_cert on disk yet (this could race with _update_tls_certificates):
                # put it there and proceed
                ROOT_CA_CERT_PATH.parent.mkdir(parents=True, exist_ok=True)
                ROOT_CA_CERT_PATH.write_text(server_ca_cert)

            return endpoint, ROOT_CA_CERT
        else:
            return endpoint, None

    def _adjust_resource_requirements(self) -> ResourceRequirements:
        """A method that gets called by `KubernetesComputeResourcesPatch` to adjust the resources requests and limits to patch."""
        cpu_limit_key = self._resources_limit_options.get("cpu_limit", "cpu_limit")
        memory_limit_key = self._resources_limit_options.get("memory_limit", "memory_limit")

        limits = {
            "cpu": self._charm.model.config.get(cpu_limit_key),
            "memory": self._charm.model.config.get(memory_limit_key),
        }
        return adjust_resource_requirements(
            limits,
            self._resources_requests_getter(),  # type: ignore
            adhere_to_requests=True,
        )

    def _setup_charm_tracing(self):
        """Configure charm tracing using ops_tracing."""
        if not self.is_ready():
            return

        endpoint, ca_path_str = self.charm_tracing_config()
        if not endpoint:
            return

        ca_text = None
        if ca_path_str and (ca_path := Path(ca_path_str)).exists():
            ca_text = ca_path.read_text()

        # we can't use ops.tracing.Tracing as this charm doesn't integrate with certs/tracing directly,
        # but the data goes through the coordinator. Instead, we use ops_tracing.set_destination.
        ops_tracing.set_destination(url=endpoint + "/v1/traces", ca=ca_text)


class ManualLogForwarder(ops.Object):
    """Forward the standard outputs of all workloads to explictly-provided Loki endpoints."""

    def __init__(
        self,
        charm: ops.CharmBase,
        *,
        loki_endpoints: Optional[Dict[str, str]],
        refresh_events: Optional[List[ops.BoundEvent]] = None,
    ):
        _PebbleLogClient.check_juju_version()
        super().__init__(charm, "worker-log-forwarder")
        self._charm = charm
        self._loki_endpoints = loki_endpoints
        self._topology: JujuTopology = JujuTopology.from_charm(charm)

        if not refresh_events:
            return

        for event in refresh_events:
            self.framework.observe(event, self.update_logging)

    def update_logging(self, _: Optional[ops.EventBase] = None):
        """Update the log forwarding to match the active Loki endpoints."""
        loki_endpoints = self._loki_endpoints

        if not loki_endpoints:
            logger.warning("No Loki endpoints available")
            loki_endpoints = {}

        for container in self._charm.unit.containers.values():
            if container.can_connect():
                _PebbleLogClient.disable_inactive_endpoints(  # type:ignore
                    container=container,
                    active_endpoints=loki_endpoints,
                    topology=self._topology,
                )
                _PebbleLogClient.enable_endpoints(  # type:ignore
                    container=container, active_endpoints=loki_endpoints, topology=self._topology
                )

    def disable_logging(self, _: Optional[ops.EventBase] = None):
        """Disable all log forwarding."""
        # This is currently necessary because, after a relation broken, the charm can still see
        # the Loki endpoints in the relation data.
        for container in self._charm.unit.containers.values():
            if container.can_connect():
                _PebbleLogClient.disable_inactive_endpoints(  # type:ignore
                    container=container, active_endpoints={}, topology=self._topology
                )
