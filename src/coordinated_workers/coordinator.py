#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Generic coordinator for a distributed charm deployment."""

import json
import logging
import re
import shutil
import socket
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    TypedDict,
    Tuple,
    Union,
)
from urllib.parse import urlparse, urlunparse

import cosl
import ops
import ops_tracing
import pydantic
import yaml
from cosl.interfaces.datasource_exchange import DatasourceExchange
from opentelemetry import trace
from ops import StatusBase

from coordinated_workers import worker
from coordinated_workers.helpers import check_libs_installed
from coordinated_workers.interfaces.cluster import ClusterProvider, RemoteWriteEndpoint
from coordinated_workers.nginx import (
    Nginx,
    NginxConfig,
    NginxLocationConfig,
    NginxMappingOverrides,
    NginxPrometheusExporter,
    NginxUpstream,
)

check_libs_installed(
    "charms.data_platform_libs.v0.s3",
    "charms.grafana_k8s.v0.grafana_dashboard",
    "charms.prometheus_k8s.v0.prometheus_scrape",
    "charms.loki_k8s.v1.loki_push_api",
    "charms.tempo_coordinator_k8s.v0.tracing",
    "charms.observability_libs.v0.kubernetes_compute_resources_patch",
    "charms.tls_certificates_interface.v4.tls_certificates",
    "charms.catalogue_k8s.v1.catalogue",
)

from charms.catalogue_k8s.v1.catalogue import CatalogueConsumer, CatalogueItem
from charms.data_platform_libs.v0.s3 import S3Requirer
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.loki_k8s.v1.loki_push_api import LogForwarder, LokiPushApiConsumer
from charms.observability_libs.v0.kubernetes_compute_resources_patch import (
    KubernetesComputeResourcesPatch,
    adjust_resource_requirements,
)
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.tempo_coordinator_k8s.v0.tracing import ReceiverProtocol, TracingEndpointRequirer
from charms.tls_certificates_interface.v4.tls_certificates import (
    CertificateRequestAttributes,
    TLSCertificatesRequiresV4,
)
from cosl.reconciler import all_events, observe_events
from lightkube.models.core_v1 import ResourceRequirements

from coordinated_workers.models import TLSConfig

logger = logging.getLogger(__name__)

# The path of the rules that will be sent to Prometheus
_tracer = trace.get_tracer("coordinator.tracer")
# The paths of the base rules to be consolidated in CONSOLIDATED_METRICS_ALERT_RULES_PATH
NGINX_ORIGINAL_METRICS_ALERT_RULES_PATH = Path("src/prometheus_alert_rules/nginx")
WORKER_ORIGINAL_METRICS_ALERT_RULES_PATH = Path("src/prometheus_alert_rules/workers")
CONSOLIDATED_METRICS_ALERT_RULES_PATH = Path("src/prometheus_alert_rules/consolidated_rules")

# The paths of the base rules to be consolidated in CONSOLIDATED_LOGS_ALERT_RULES_PATH
ORIGINAL_LOGS_ALERT_RULES_PATH = Path("src/loki_alert_rules")
CONSOLIDATED_LOGS_ALERT_RULES_PATH = Path("src/loki_alert_rules/consolidated_rules")

# Paths for proxied worker telementry urlparse
PROXY_WORKER_TELEMETRY_PATHS = {
    "worker_metrics": "/proxy/worker/{unit}/metrics",
    "loki_endpoint": "proxy/loki/{unit}/push",
    "remote_write_endpoint": "proxy/remote-write/{unit}/write",
    "charm_tracing_receivers_urls": "/proxy/charm-tracing/{protocol}",
    "workload_tracing_receivers_urls": "/proxy/workload-tracing/{protocol}",
}
PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX = "worker-telemetry-proxy"


class S3NotFoundError(Exception):
    """Raised when the s3 integration is not present or not ready."""


class ClusterRolesConfigError(Exception):
    """Raised when the ClusterRolesConfig instance is not properly configured."""


class S3ConnectionInfo(pydantic.BaseModel):
    """Model for the s3 relation databag, as returned by the s3 charm lib."""

    # they don't use it, we do

    model_config = {"populate_by_name": True}

    endpoint: str
    bucket: str
    access_key: str = pydantic.Field(alias="access-key")  # type: ignore
    secret_key: str = pydantic.Field(alias="secret-key")  # type: ignore

    region: Optional[str] = pydantic.Field(None)  # type: ignore
    tls_ca_chain: Optional[List[str]] = pydantic.Field(None, alias="tls-ca-chain")  # type: ignore

    @property
    def ca_cert(self) -> Optional[str]:
        """Unify the ca chain provided by the lib into a single cert."""
        return "\n\n".join(self.tls_ca_chain) if self.tls_ca_chain else None


@dataclass
class ClusterRolesConfig:
    """Worker roles and deployment requirements."""

    roles: Iterable[str]
    """The union of enabled roles for the application."""
    meta_roles: Mapping[str, Iterable[str]]
    """Meta roles are composed of non-meta roles (default: all)."""
    minimal_deployment: Iterable[str]
    """The minimal set of roles that need to be allocated for the deployment to be considered consistent."""
    recommended_deployment: Dict[str, int]
    """The set of roles that need to be allocated for the deployment to be considered robust according to the official recommendations/guidelines.."""

    def __post_init__(self):
        """Ensure the various role specifications are consistent with one another."""
        are_meta_keys_valid = set(self.meta_roles.keys()).issubset(self.roles)
        are_meta_values_valid = all(
            set(meta_value).issubset(self.roles) for meta_value in self.meta_roles.values()
        )
        is_minimal_valid = set(self.minimal_deployment).issubset(self.roles)
        is_recommended_valid = set(self.recommended_deployment).issubset(self.roles)
        if not all(
            [
                are_meta_keys_valid,
                are_meta_values_valid,
                is_minimal_valid,
                is_recommended_valid,
            ]
        ):
            raise ClusterRolesConfigError(
                "Invalid ClusterRolesConfig: The configuration is not coherent."
            )

    def is_coherent_with(self, cluster_roles: Iterable[str]) -> bool:
        """Returns True if the provided roles satisfy the minimal deployment spec; False otherwise."""
        return set(self.minimal_deployment).issubset(set(cluster_roles))


def _validate_container_name(
    container_name: Optional[str],
    resources_requests: Optional[Callable[["Coordinator"], Dict[str, str]]],
):
    """Raise `ValueError` if `resources_requests` is not None and `container_name` is None."""
    if resources_requests is not None and container_name is None:
        raise ValueError(
            "Cannot have a None value for container_name while resources_requests is provided."
        )


_EndpointMapping = TypedDict(
    "_EndpointMapping",
    {
        "certificates": str,
        "cluster": str,
        "grafana-dashboards": str,
        "logging": str,
        "metrics": str,
        "charm-tracing": str,
        "workload-tracing": str,
        "s3": str,
        # optional integrations
        "send-datasource": Optional[str],
        "receive-datasource": Optional[str],
        "catalogue": Optional[str],
    },
    total=True,
)
"""Mapping of the relation endpoint names that the charms uses, as defined in metadata.yaml."""

_ResourceLimitOptionsMapping = TypedDict(
    "_ResourceLimitOptionsMapping",
    {
        "cpu_limit": str,
        "memory_limit": str,
    },
)
"""Mapping of the resources limit option names that the charms use, as defined in config.yaml."""


class Coordinator(ops.Object):
    """Charming coordinator.

    This class takes care of the shared tasks of a coordinator, including handling workers,
    running Nginx, and implementing self-monitoring integrations.
    """

    _default_degraded_message = "Degraded."
    _default_active_message = ""

    def __init__(
        self,
        charm: ops.CharmBase,
        roles_config: ClusterRolesConfig,
        external_url: str,  # the ingressed url if we have ingress, else fqdn
        worker_metrics_port: int,
        endpoints: _EndpointMapping,
        nginx_config: NginxConfig,
        workers_config: Callable[["Coordinator"], str],
        worker_ports: Optional[Callable[[str], Sequence[int]]] = None,
        nginx_options: Optional[NginxMappingOverrides] = None,
        is_coherent: Optional[Callable[[ClusterProvider, ClusterRolesConfig], bool]] = None,
        is_recommended: Optional[Callable[[ClusterProvider, ClusterRolesConfig], bool]] = None,
        resources_limit_options: Optional[_ResourceLimitOptionsMapping] = None,
        resources_requests: Optional[Callable[["Coordinator"], Dict[str, str]]] = None,
        container_name: Optional[str] = None,
        remote_write_endpoints: Optional[Callable[[], List[RemoteWriteEndpoint]]] = None,
        workload_tracing_protocols: Optional[List[ReceiverProtocol]] = None,
        catalogue_item: Optional[CatalogueItem] = None,
        proxy_worker_telemetry: Optional[bool] = False,
        proxy_worker_telemetry_port: Optional[int] = None,
    ):
        """Constructor for a Coordinator object.

        Args:
            charm: The coordinator charm object.
            roles_config: Definition of the roles and the deployment requirements.
            external_url: The external (e.g., ingressed) URL of the coordinator charm.
            worker_metrics_port: The port under which workers expose their metrics.
            nginx_config: A function generating the Nginx configuration file for the workload.
            workers_config: A function generating the configuration for the workers, to be
                published in relation data.
            worker_ports: A function returning the ports that a worker with a given role should open.
            endpoints: Endpoint names for coordinator relations, as defined in metadata.yaml.
            nginx_options: Non-default config options for Nginx.
            is_coherent: Custom coherency checker for a minimal deployment.
            is_recommended: Custom coherency checker for a recommended deployment.
            resources_limit_options: A dictionary containing resources limit option names. The dictionary should include
                "cpu_limit" and "memory_limit" keys with values as option names, as defined in the config.yaml.
                If no dictionary is provided, the default option names "cpu_limit" and "memory_limit" would be used.
            resources_requests: A function generating the resources "requests" portion to apply when patching a container using
                KubernetesComputeResourcesPatch. The "limits" portion of the patch gets populated by setting
                their respective config options in config.yaml.
            container_name: The container for which to apply the resources requests & limits.
                Required if `resources_requests` is provided.
            remote_write_endpoints: A function generating endpoints to which the workload
                and the worker charm can push metrics to.
            workload_tracing_protocols: A list of protocols that the worker intends to send
                workload traces with.
            catalogue_item: A catalogue application entry to be sent to catalogue.
            proxy_worker_telemetry: If True, enables routing worker metrics, logs and traces through nginx proxy.
            proxy_worker_telemetry_port: The HTTP port through which all worker telemetry traffic will be proxied when proxy_worker_telemetry is enabled.

        Raises:
        ValueError:
            If `resources_requests` is not None and `container_name` is None, a ValueError is raised.
        """
        super().__init__(charm, key="coordinator")
        _validate_container_name(container_name, resources_requests)

        # static attributes
        self._charm = charm
        self._external_url = external_url
        self._worker_metrics_port = worker_metrics_port
        self._endpoints = endpoints
        self._nginx_config = nginx_config
        self._roles_config = roles_config
        self._container_name = container_name
        self._resources_limit_options = resources_limit_options or {}
        self._catalogue_item = catalogue_item
        self._catalogue = (
            CatalogueConsumer(self._charm, relation_name=endpoint)
            if (endpoint := self._endpoints.get("catalogue"))
            else None
        )

        # dynamic attributes (callbacks)
        self._override_coherency_checker = is_coherent
        self._override_recommended_checker = is_recommended
        self._resources_requests_getter = (
            partial(resources_requests, self) if resources_requests is not None else None
        )
        self._remote_write_endpoints_getter = remote_write_endpoints
        self._workers_config_getter = partial(workers_config, self)
        self._proxy_worker_telemetry = proxy_worker_telemetry
        self._proxy_worker_telemetry_port = proxy_worker_telemetry_port
        # check if the worker telmetry can be validly proxied, if not disable proxying with an error log
        self._validate_proxy_worker_telemetry_setup(workload_tracing_protocols)  # type: ignore

        ## Integrations
        self.cluster = ClusterProvider(
            self._charm,
            frozenset(roles_config.roles),
            roles_config.meta_roles,
            endpoint=self._endpoints["cluster"],
            worker_ports=worker_ports,
        )

        
        self._certificates = TLSCertificatesRequiresV4(
            self._charm,
            relationship_name=self._endpoints["certificates"],
            certificate_requests=[self._certificate_request_attributes],
        )

        self.s3_requirer = S3Requirer(self._charm, self._endpoints["s3"])
        self.datasource_exchange = DatasourceExchange(
            self._charm,
            provider_endpoint=self._endpoints.get("send-datasource", None),
            requirer_endpoint=self._endpoints.get("receive-datasource", None),
        )

        self._grafana_dashboards = GrafanaDashboardProvider(
            self._charm, relation_name=self._endpoints["grafana-dashboards"]
        )

        # FIXME: https://github.com/canonical/cos-coordinated-workers/issues/23
        # Using two different logging wrappers on the same relation endpoint
        # causes unnecessary updates to the relation databag with alert rules that are
        # already populated by the other wrapper.
        self._worker_logging = LokiPushApiConsumer(
            self._charm,
            relation_name=self._endpoints["logging"],
            alert_rules_path=str(CONSOLIDATED_LOGS_ALERT_RULES_PATH),
        )
        self._coordinator_logging = LogForwarder(
            self._charm,
            relation_name=self._endpoints["logging"],
            alert_rules_path=str(CONSOLIDATED_LOGS_ALERT_RULES_PATH),
        )
        self.charm_tracing = TracingEndpointRequirer(
            self._charm,
            relation_name=self._endpoints["charm-tracing"],
            protocols=["otlp_http"],
        )
        self.workload_tracing = TracingEndpointRequirer(
            self._charm,
            relation_name=self._endpoints["workload-tracing"],
            protocols=workload_tracing_protocols,
        )

        # NOTE: setup nginx after tracing requirers as uses logging and tracing endpoints for config building
        self._upstreams_to_addresses = self.cluster.gather_addresses_by_role()
        if self._proxy_worker_telemetry:
            self._setup_proxy_worker_telemetry()

        self.nginx = Nginx(
            self._charm,
            config_getter=partial(
                self._nginx_config.get_config, self._upstreams_to_addresses
            ),
            tls_config_getter=lambda: self.tls_config,
            options=nginx_options,
        )
        self.nginx_exporter = NginxPrometheusExporter(self._charm, options=nginx_options)

        # NOTE: setup metrics after nginx as scrape jobs include nginx scrape jobs as well
        self._scraping = MetricsEndpointProvider(
            self._charm,
            relation_name=self._endpoints["metrics"],
            alert_rules_path=str(CONSOLIDATED_METRICS_ALERT_RULES_PATH),
            jobs=self._scrape_jobs,
            external_url=self._external_url,
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

        ## Observers
        # We always listen to collect-status
        self.framework.observe(self._charm.on.collect_unit_status, self._on_collect_unit_status)

        # If the cluster isn't ready, refuse to handle any other event as we can't possibly know what to do
        if not self.cluster.has_workers:
            logger.warning(
                f"Incoherent deployment. {charm.unit.name} is missing relation to workers. "
                "This charm will be unresponsive and refuse to handle any event until "
                "the situation is resolved by the cloud admin, to avoid data loss."
            )
            return
        if not self.is_coherent:
            logger.error(
                f"Incoherent deployment. {charm.unit.name} will be shutting down. "
                "This likely means you are lacking some required roles in your workers. "
                "This charm will be unresponsive and refuse to handle any event until "
                "the situation is resolved by the cloud admin, to avoid data loss."
            )
            return
        if self.cluster.has_workers and not self.s3_ready:
            logger.error(
                f"Incoherent deployment. {charm.unit.name} will be shutting down. "
                "This likely means you need to add an s3 integration, or wait for it to be ready. "
                "This charm will be unresponsive and refuse to handle any event until "
                "the situation is resolved by the cloud admin, to avoid data loss."
            )
            return

        observe_events(self._charm, all_events, self._reconcile)

    def _reconcile(self):
        """Run all logic that is independent of what event we're processing."""
        # There could be a race between the resource patch and pebble operations
        # i.e., charm code proceeds beyond a can_connect guard, and then lightkube patches the statefulset
        # and the workload is no longer available.
        # `resources_patch` might be `None` when no resources requests or limits are requested by the charm.
        if self.resources_patch and not self.resources_patch.is_ready():
            logger.debug("Resource patch not ready yet. Skipping cluster update step.")
            return

        # certificates must be synced before we reconcile the workloads; otherwise changes in the certs may go unnoticed.
        self._certificates.sync()
        # keep this on top right after certificates sync
        self._setup_charm_tracing()

        # reconcile workloads
        self.nginx.reconcile()
        self.nginx_exporter.reconcile()

        # reconcile relations
        self._reconcile_cluster_relations()
        self._consolidate_alert_rules()
        self._scraping.set_scrape_job_spec()  # type: ignore
        self._worker_logging.reload_alerts()

        if (catalogue := self._catalogue) and (item := self._catalogue_item):
            catalogue.update_item(item)

    ######################
    # UTILITY PROPERTIES #
    ######################

    @property
    def _charm_tracing_receivers_urls(self) -> Dict[str, str]:
        """Returns the charm tracing enabled receivers with their corresponding endpoints."""
        endpoints = self.charm_tracing.get_all_endpoints()
        receivers = endpoints.receivers if endpoints else ()
        return {receiver.protocol.name: receiver.url for receiver in receivers}

    @property
    def _workload_tracing_receivers_urls(self) -> Dict[str, str]:
        """Returns the workload tracing enabled receivers with their corresponding endpoints."""
        endpoints = self.workload_tracing.get_all_endpoints()
        receivers = endpoints.receivers if endpoints else ()
        return {receiver.protocol.name: receiver.url for receiver in receivers}

    @property
    def is_coherent(self) -> bool:
        """Check whether this coordinator is coherent."""
        if override_coherency_checker := self._override_coherency_checker:
            return override_coherency_checker(self.cluster, self._roles_config)

        return self._roles_config.is_coherent_with(self.cluster.gather_roles().keys())

    @property
    def missing_roles(self) -> Set[str]:
        """What roles are missing from this cluster, if any."""
        roles = self.cluster.gather_roles()
        missing_roles: Set[str] = set(self._roles_config.minimal_deployment).difference(
            roles.keys()
        )
        return missing_roles

    @property
    def is_recommended(self) -> Optional[bool]:
        """Check whether this coordinator is connected to the recommended number of workers.

        Will return None if no recommended criterion is defined.
        """
        if override_recommended_checker := self._override_recommended_checker:
            return override_recommended_checker(self.cluster, self._roles_config)

        rc = self._roles_config
        if not rc.recommended_deployment:
            # we don't have a definition of recommended: return None
            return None

        cluster = self.cluster
        roles = cluster.gather_roles()
        for role, min_n in rc.recommended_deployment.items():
            if roles.get(role, 0) < min_n:
                return False
        return True

    @property
    def can_handle_events(self) -> bool:
        """Check whether the coordinator should handle events."""
        return self.cluster.has_workers and self.is_coherent and self.s3_ready

    @property
    def hostname(self) -> str:
        """Unit's hostname."""
        return socket.getfqdn()

    @staticmethod
    def app_hostname(hostname: str, app_name: str, model_name: str) -> str:
        """The FQDN of the k8s service associated with this application.

        This service load balances traffic across all application units.
        Falls back to this unit's DNS name if the hostname does not resolve to a Kubernetes-style fqdn.
        """
        # hostname is expected to look like: 'tempo-0.tempo-headless.default.svc.cluster.local'
        hostname_parts = hostname.split(".")
        # 'svc' is always there in a K8s service fqdn
        # ref: https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/#services
        if "svc" not in hostname_parts:
            logger.debug(f"expected K8s-style fqdn, but got {hostname} instead")
            return hostname

        dns_name_parts = hostname_parts[hostname_parts.index("svc") :]
        dns_name = ".".join(dns_name_parts)  # 'svc.cluster.local'
        return f"{app_name}.{model_name}.{dns_name}"  # 'tempo.model.svc.cluster.local'

    @property
    def _internal_url(self) -> str:
        """Unit's hostname including the scheme."""
        scheme = "https" if self.tls_available else "http"
        return f"{scheme}://{self.hostname}"

    @property
    def tls_config(self) -> Optional[TLSConfig]:
        """Returns the TLS configuration, including certificates and private key, if available; None otherwise."""
        certificates, key = self._certificates.get_assigned_certificate(
            certificate_request=self._certificate_request_attributes
        )
        if not (key and certificates):
            return None
        return TLSConfig(certificates.certificate.raw, certificates.ca.raw, key.raw)

    @property
    def tls_available(self) -> bool:
        """Return True if tls is enabled and the necessary certs are found."""
        return bool(self.tls_config)

    @property
    def s3_connection_info(self) -> S3ConnectionInfo:
        """Cast and validate the untyped s3 databag to something we can handle."""
        try:
            # we have to type-ignore here because the s3 lib's type annotation is wrong
            return S3ConnectionInfo(**self.s3_requirer.get_s3_connection_info())  # type: ignore
        except pydantic.ValidationError:
            raise S3NotFoundError("s3 integration inactive or interface corrupt")

    @property
    def _s3_config(self) -> Dict[str, Any]:
        """The s3 configuration from relation data.

        The configuration is adapted to a drop-in format for the HA workers to use.

        Raises:
            S3NotFoundError: The s3 integration is inactive.
        """
        s3_data = self.s3_connection_info
        s3_endpoint_scheme = urlparse(s3_data.endpoint).scheme
        s3_config = {
            "endpoint": re.sub(rf"^{s3_endpoint_scheme}://", "", s3_data.endpoint),
            "region": s3_data.region,
            "access_key_id": s3_data.access_key,
            "secret_access_key": s3_data.secret_key,
            "bucket_name": s3_data.bucket,
            "insecure": not (s3_data.tls_ca_chain or s3_endpoint_scheme == "https"),
            # the tempo config wants a path to a file here. We pass the cert chain separately
            # over the cluster relation; the worker will be responsible for writing the file to disk
            "tls_ca_path": worker.S3_TLS_CA_CHAIN_FILE if s3_data.tls_ca_chain else None,
        }

        return s3_config

    @property
    def s3_ready(self) -> bool:
        """Check whether s3 is configured."""
        try:
            return bool(self._s3_config)
        except S3NotFoundError:
            return False

    @property
    def peer_addresses(self) -> List[str]:
        """If a peer relation is present, return the addresses of the peers."""
        peers = self._peers
        relation = self.model.get_relation("peers")
        # get unit addresses for all the other units from a databag
        addresses = []
        if peers and relation:
            addresses = [relation.data.get(unit, {}).get("local-ip") for unit in peers]
            addresses = list(filter(None, addresses))

        # add own address
        if self._local_ip:
            addresses.append(self._local_ip)

        return addresses

    @property
    def _local_ip(self) -> Optional[str]:
        """Local IP of the peers binding."""
        try:
            binding = self.model.get_binding("peers")
            if not binding:
                logger.error(
                    "unable to get local IP at this time: "
                    "peers binding not active yet. It could be that the charm "
                    "is still being set up..."
                )
                return None
            return str(binding.network.bind_address)
        except (ops.ModelError, KeyError) as e:
            logger.debug("failed to obtain local ip from peers binding", exc_info=True)
            logger.error(
                f"unable to get local IP at this time: failed with {type(e)}; "
                f"see debug log for more info"
            )
            return None

    @property
    def _workers_scrape_jobs(self) -> List[Dict[str, Any]]:
        """The Prometheus scrape jobs for the workers connected to the coordinator."""
        scrape_jobs: List[Dict[str, Any]] = []

        for worker_topology in self.cluster.gather_topology():
            if self._proxy_worker_telemetry:
                # when proxied through nginx
                # adress: address of the coordinator
                # path: location used in the nginx config for proxying worker metric
                targets = [
                    f"{self.hostname}:{self._proxy_worker_telemetry_port}"
                ]
                metrics_path = PROXY_WORKER_TELEMETRY_PATHS["worker_metrics"].format(
                    unit=worker_topology['unit'].replace('/', '-'),
                )
            else:
                # Direct access to worker metrics endpoints
                targets = [f"{worker_topology['address']}:{self._worker_metrics_port}"]
                metrics_path = "/metrics"

            job = {
                "metrics_path": metrics_path,
                "static_configs": [
                    {
                        "targets": targets,
                    }
                ],
                # setting these as "labels" in the static config gets some of them
                # replaced by the coordinator topology
                # https://github.com/canonical/prometheus-k8s-operator/issues/571
                "relabel_configs": [
                    {"target_label": "juju_charm", "replacement": worker_topology["charm_name"]},
                    {"target_label": "juju_unit", "replacement": worker_topology["unit"]},
                    {
                        "target_label": "juju_application",
                        "replacement": worker_topology["application"],
                    },
                    {"target_label": "juju_model", "replacement": self.model.name},
                    {"target_label": "juju_model_uuid", "replacement": self.model.uuid},
                ],
            }
            if self.tls_available:
                job["scheme"] = "https"  # pyright: ignore
            scrape_jobs.append(job)
        return scrape_jobs

    @property
    def _nginx_scrape_jobs(self) -> List[Dict[str, Any]]:
        """The Prometheus scrape job for Nginx."""
        job: Dict[str, Any] = {
            "static_configs": [
                {"targets": [f"{self.hostname}:{self.nginx.options['nginx_exporter_port']}"]}
            ]
        }

        return [job]

    @property
    def _scrape_jobs(self) -> List[Dict[str, Any]]:
        """The scrape jobs to send to Prometheus."""
        return self._workers_scrape_jobs + self._nginx_scrape_jobs

    @property
    def _certificate_request_attributes(self) -> CertificateRequestAttributes:
        return CertificateRequestAttributes(
            # common_name is required and has a limit of 64 chars.
            # it is superseded by sans anyway, so we can use a constrained name,
            # such as app_name
            common_name=self._charm.app.name,
            # update certificate with new SANs whenever a worker is added/removed
            sans_dns=frozenset(
                (
                    self.hostname,
                    self.app_hostname(self.hostname, self._charm.app.name, self._charm.model.name),
                    *self.cluster.gather_addresses(),
                )
            ),
        )

    ##################
    # EVENT HANDLERS #
    ##################

    def _on_peers_relation_created(self, event: ops.RelationCreatedEvent):
        if self._local_ip:
            event.relation.data[self._charm.unit]["local-ip"] = self._local_ip

    # keep this event handler at the bottom
    def _on_collect_unit_status(self, e: ops.CollectStatusEvent):
        # todo add [nginx.workload] statuses
        statuses: List[StatusBase] = []

        if self.resources_patch and self.resources_patch.get_status().name != "active":
            statuses.append(self.resources_patch.get_status())

        if not self.cluster.has_workers:
            statuses.append(ops.BlockedStatus("[consistency] Missing any worker relation."))
        elif not self.is_coherent:
            statuses.append(ops.BlockedStatus("[consistency] Cluster inconsistent."))
        elif not self.is_recommended:
            # if is_recommended is None: it means we don't meet the recommended deployment criterion.
            statuses.append(ops.ActiveStatus(self._default_degraded_message))

        if not self.s3_requirer.relations:
            statuses.append(ops.BlockedStatus("[s3] Missing S3 integration."))
        elif not self.s3_ready:
            statuses.append(ops.BlockedStatus("[s3] S3 not ready (probably misconfigured)."))

        if not statuses:
            statuses.append(ops.ActiveStatus(self._default_active_message))

        for status in statuses:
            e.add_status(status)

    ###################
    # UTILITY METHODS #
    ###################
    @property
    def _peers(self) -> Optional[Set[ops.model.Unit]]:
        relation = self.model.get_relation("peers")
        if not relation:
            return None

        # self is not included in relation.units
        return relation.units

    @property
    def loki_endpoints_by_unit(self) -> Dict[str, str]:
        """Loki endpoints from relation data in the format needed for Pebble log forwarding.

        Returns:
            A dictionary of remote units and the respective Loki endpoint.
            {
                "loki/0": "http://loki:3100/loki/api/v1/push",
                "another-loki/0": "http://another-loki:3100/loki/api/v1/push",
            }
        """
        endpoints: Dict[str, str] = {}
        relations: List[ops.Relation] = self.model.relations.get(self._endpoints["logging"], [])

        for relation in relations:
            for unit in relation.units:
                unit_databag = relation.data.get(unit, {})
                if "endpoint" not in unit_databag:
                    continue
                endpoint = unit_databag["endpoint"]
                deserialized_endpoint = json.loads(endpoint)
                url = deserialized_endpoint["url"]
                endpoints[unit.name] = url

        return endpoints

    def _reconcile_cluster_relations(self):
        """Build the workers config and distribute it to the relations."""
        if not self._charm.unit.is_leader():
            return

        tls_config = self.tls_config
        # we share the certs in plaintext as they're not sensitive information
        # On every function call, we always publish everything to the databag; however, if there
        # are no changes, Juju will notice there's no delta and do nothing
        self.cluster.publish_data(
            worker_config=self._workers_config_getter(),
            loki_endpoints=self.proxy_loki_endpoints_by_unit if self._proxy_worker_telemetry else self.loki_endpoints_by_unit,
            # all arguments below are optional:
            ca_cert=tls_config.ca_cert if tls_config else None,
            server_cert=tls_config.server_cert if tls_config else None,
            # FIXME: We're relying on a private method from the TLS library
            # https://github.com/canonical/cos-coordinated-workers/issues/16
            privkey_secret_id=self.cluster.grant_privkey(
                self._certificates._get_private_key_secret_label()  # type: ignore
            ),
            charm_tracing_receivers=self._proxy_charm_tracing_receivers_urls if self._proxy_worker_telemetry else self._charm_tracing_receivers_urls,
            workload_tracing_receivers=self._proxy_workload_tracing_receivers_urls if self._proxy_worker_telemetry else self._workload_tracing_receivers_urls,
            remote_write_endpoints=self.remote_write_endpoints,
            s3_tls_ca_chain=self.s3_connection_info.ca_cert,
        )

    def _consolidate_workers_alert_rules(self):
        """Regenerate the worker alert rules from relation data."""
        alert_rules_sources = (
            (
                WORKER_ORIGINAL_METRICS_ALERT_RULES_PATH,
                CONSOLIDATED_METRICS_ALERT_RULES_PATH,
                "promql",
            ),
            (ORIGINAL_LOGS_ALERT_RULES_PATH, CONSOLIDATED_LOGS_ALERT_RULES_PATH, "logql"),
        )
        apps: Set[str] = set()
        to_write: Dict[str, str] = {}
        for worker_topology in self.cluster.gather_topology():
            if worker_topology["application"] in apps:
                continue

            apps.add(worker_topology["application"])
            topology_dict = {
                "model": self.model.name,
                "model_uuid": self.model.uuid,
                "application": worker_topology["application"],
                "unit": worker_topology["unit"],
                "charm_name": worker_topology["charm_name"],
            }
            topology = cosl.JujuTopology.from_dict(topology_dict)
            for orig_path, consolidated_path, type in alert_rules_sources:
                alert_rules = cosl.AlertRules(query_type=type, topology=topology)  # type: ignore
                alert_rules.add_path(orig_path)
                file = f"{consolidated_path}/consolidated_{worker_topology['application']}.rules"
                to_write[file] = yaml.dump(alert_rules.as_dict())
        with _tracer.start_as_current_span("writing consolidated rules"):
            for file_name, alert_rules_contents in to_write.items():
                Path(file_name).write_text(alert_rules_contents)

    def _remove_consolidated_alert_rules(self, path: Path):
        with _tracer.start_as_current_span("clearing consolidated rules"):
            for file in path.glob("consolidated_*"):
                file.unlink()

    def _consolidate_nginx_alert_rules(self):
        """Copy Nginx alert rules to the merged alert folder."""
        alerts_paths = (
            (NGINX_ORIGINAL_METRICS_ALERT_RULES_PATH, CONSOLIDATED_METRICS_ALERT_RULES_PATH),
            (ORIGINAL_LOGS_ALERT_RULES_PATH, CONSOLIDATED_LOGS_ALERT_RULES_PATH),
        )

        for orig_path, consolidated_path in alerts_paths:
            for filename in orig_path.glob("*.*"):
                shutil.copy(filename, consolidated_path)

    def _consolidate_alert_rules(self):
        """Render the alert rules for Nginx and the connected workers."""
        with _tracer.start_as_current_span("consolidate alert rules"):
            for path in (
                CONSOLIDATED_METRICS_ALERT_RULES_PATH,
                CONSOLIDATED_LOGS_ALERT_RULES_PATH,
            ):
                path.mkdir(exist_ok=True)
                self._remove_consolidated_alert_rules(path)
            self._consolidate_workers_alert_rules()
            self._consolidate_nginx_alert_rules()

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
            self._resources_requests_getter() if self._resources_requests_getter else None,
            adhere_to_requests=True,  # type: ignore
        )

    def _setup_charm_tracing(self):
        """Configure ops.tracing to send traces to a tracing backend."""
        if self.charm_tracing.is_ready():
            endpoint = self.charm_tracing.get_endpoint("otlp_http")
            if not endpoint:
                return
            ops_tracing.set_destination(
                url=endpoint + "/v1/traces",
                ca=self.tls_config.ca_cert if self.tls_config else None,
            )

    #####################################
    # WORKER TELEMETRY PROXYING HELPERS #
    #####################################
    @property
    def proxy_loki_endpoints_by_unit(self) -> Dict[str, str]:
        """Returns proxy loki endpoints published to the cluster provider for log forwarding via the proxy.

        Returns:
            A dictionary of remote units and the respective proxy Loki endpoint.
            {
                "loki/0": "http://tempo:3200/proxy/loki/loki-0/push",
                "another-loki/0": "http://tempo:3200/proxy/loki/another-loki-0/push",
            }
        """
        endpoints: Dict[str, str] = {}

        for unit in self.loki_endpoints_by_unit:
            scheme = "https" if self.tls_available else "http"
            endpoints[unit] = f"{scheme}://{self.hostname}:{self._proxy_worker_telemetry_port}/{PROXY_WORKER_TELEMETRY_PATHS['loki_endpoint'].format(unit=unit.replace('/', '-'))}"

        return endpoints

    @property
    def remote_write_endpoints(self):
        """Returns the remote write endpoints based on if its available and if proxying telemetry is enabled."""
        if not self._remote_write_endpoints_getter:
            return None
        return self.proxy_remote_write_endpoints if self._proxy_worker_telemetry else self._remote_write_endpoints_getter()

    @property
    def proxy_remote_write_endpoints(self) -> Union[List[RemoteWriteEndpoint], None]:
        """Returns proxy remote write endpoints published to the cluster provider for metrics forwarding via the proxy.

        Returns:
            A list of RemoteWriteEndpoint.
        """
        endpoints:List[RemoteWriteEndpoint] = []

        if not self._remote_write_endpoints_getter:
            return None

        for endpoint in self._remote_write_endpoints_getter():
            p = urlparse(endpoint["url"])
            unit = p.hostname.split(".")[0]  # type: ignore
            scheme = "https" if self.tls_available else "http"
            proxy_url = f"{scheme}://{self.hostname}:{self._proxy_worker_telemetry_port}/{PROXY_WORKER_TELEMETRY_PATHS['remote_write_endpoint'].format(unit=unit)}"
            endpoints.append(
                RemoteWriteEndpoint(url=proxy_url)
            )

        return endpoints

    @property
    def _proxy_charm_tracing_receivers_urls(self) -> Dict[str, str]:
        """Returns proxy charm tracing receivers urls published to the cluster."""
        urls: Dict[str, str] = {}

        for protocol in self._charm_tracing_receivers_urls:
            scheme = "https" if self.tls_available else "http"
            proxy_url = f"{scheme}://{self.hostname}:{self._proxy_worker_telemetry_port}/{PROXY_WORKER_TELEMETRY_PATHS['charm_tracing_receivers_urls'].format(protocol=protocol)}"
            urls.update({protocol: proxy_url})

        return urls

    @property
    def _proxy_workload_tracing_receivers_urls(self) -> Dict[str, str]:
        """Returns proxy worload tracing receivers urls published to the cluster."""
        urls: Dict[str, str] = {}

        for protocol in self._workload_tracing_receivers_urls:
            scheme = "https" if self.tls_available else "http"
            proxy_url = f"{scheme}://{self.hostname}:{self._proxy_worker_telemetry_port}/{PROXY_WORKER_TELEMETRY_PATHS['workload_tracing_receivers_urls'].format(protocol=protocol)}"
            urls.update({protocol: proxy_url})

        return urls


    def _validate_proxy_worker_telemetry_setup(self, workload_tracing_protocols: Union[List[ReceiverProtocol], None]) -> None:  # type: ignore
        """Check if a valid proxy setup for worker telemetry is possible."""
        # return true if proxying for worker telemetry is not enabled
        if not self._proxy_worker_telemetry:
            return
        # return false if no port for proxying worker telemetry is provided
        if not self._proxy_worker_telemetry_port:
            logger.error(
                "Proxying worker telemetry via the coordinator failed."
                "Port for proxying worker telemetry not defined."
                "Falling back to telemetry pushing/pulling directly from the workers."
            )
            self._proxy_worker_telemetry = False
        # if no workload protocol is defined, let the TracingEndpointRequirer handle this
        if not workload_tracing_protocols:
            return
        for protocol in workload_tracing_protocols:  # type: ignore
            if "grpc" in protocol:
                logger.error(
                    "Proxying worker telemetry via the coordinator failed."
                    "Proxying worker telemetry does not support receiving workload traces via a GRPC based protocol."
                    "Falling back to telemetry pushing/pulling directly from the workers."
                )
                self._proxy_worker_telemetry = False
                break

    def _setup_proxy_worker_telemetry(self) -> None:
        """ Extends nginx coniguration with configurations required prxying worker telemetry to and from the workers via nginx."""
        # Extend nginx config with worker metrics if enabled
        worker_topology = self.cluster.gather_topology()
        if worker_topology:
            telemetry_upstreams, telemetry_locations = self._generate_worker_telemetry_nginx_config(
                worker_topology
            )
            self._nginx_config.extend_upstream_configs(telemetry_upstreams)
            self._nginx_config.update_server_ports_to_locations(telemetry_locations, overwrite=False)
        self._update_upstreams_with_worker_telemetry_servers()

    def _update_upstreams_with_worker_telemetry_servers(self) -> None:
        """Update the upstreams in the nginx config to include the required servers/clinets that send/receive worker telemetry."""
        # Merge role-based and unit-based addresses collection for nginx config
        # Every unit will get its own upstream for metric proxying
        self._upstreams_to_addresses.update(self.cluster.gather_addresses_by_unit())

        # loki upstream to address mapper
        for loki_unit, address in self.loki_endpoints_by_unit.items():
            p = urlparse(address)
            self._upstreams_to_addresses[loki_unit] = {p.hostname}  # type: ignore

        # remote write upstream to address mapper
        if self._remote_write_endpoints_getter:
            for endpoint in self._remote_write_endpoints_getter():
                p = urlparse(endpoint["url"])
                remote_write_unit = p.hostname.split(".")[0]  # type: ignore
                self._upstreams_to_addresses[remote_write_unit] = {p.hostname}  # type: ignore

        # tracing upstream to address mapper
        for protocol, address in self._charm_tracing_receivers_urls.items():
            p = urlparse(address)
            if p.hostname == self.hostname:  # we are tracing ourselves. ignore
                continue
            upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{protocol}"
            self._upstreams_to_addresses[upstream_name] = {p.hostname}  # type: ignore
        for protocol, address in self._workload_tracing_receivers_urls.items():
            p = urlparse(address)
            if p.hostname == self.hostname:  # we are tracing ourselves. ignore
                continue
            upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{protocol}"
            self._upstreams_to_addresses[upstream_name] = {p.hostname}  # type: ignore


    def _generate_worker_telemetry_nginx_config(self, worker_topology: List[Dict[str, str]]) -> Tuple[List[NginxUpstream], Dict[int, List[NginxLocationConfig]]]:
        """Generate nginx upstreams and locations for proxying worker telemetry via nginx."""

        upstreams_worker_metrics, locations_worker_metrics = self._generate_worker_metrics_nginx_config(
            worker_topology,
        )
        upstreams_loki_endpoints, locations_loki_endpoints = self._generate_loki_endpoints_nginx_config()
        upstreams_remote_write_endpoints, locations_remote_write_endpoints = self._generate_remote_write_endpoints_nginx_config()
        upstreams_tracing_urls, locations_tracing_urls = self._generate_tracing_urls_nginx_config()

        upstreams: List[NginxUpstream] = [
            *upstreams_worker_metrics,
            *upstreams_loki_endpoints,
            *upstreams_remote_write_endpoints,
            *upstreams_tracing_urls,
        ]
        locations: Dict[int, List[NginxLocationConfig]] = {
            self._proxy_worker_telemetry_port: [  # type: ignore
                *locations_worker_metrics,
                *locations_loki_endpoints,
                *locations_remote_write_endpoints,
                *locations_tracing_urls,
            ]
        }

        return upstreams, locations

    def _generate_worker_metrics_nginx_config(self, worker_topology: List[Dict[str, str]]) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
        """Generate nginx upstreams and locations for worker metrics routing."""
        upstreams: List[NginxUpstream] = []
        locations: List[NginxLocationConfig] = []

        for worker_ in worker_topology:
            unit_name = worker_["unit"]
            unit_name_sanitized = unit_name.replace("/", "-")
            upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{unit_name_sanitized}"

            upstreams.append(
                NginxUpstream(
                    name=upstream_name,
                    port=self._worker_metrics_port,
                    address_lookup_key=unit_name,
                )
            )

            locations.append(
                NginxLocationConfig(
                    path=PROXY_WORKER_TELEMETRY_PATHS["worker_metrics"].format(unit=unit_name_sanitized),
                    backend=upstream_name,
                    backend_url="/metrics",
                    is_grpc=False,
                )
            )

        return upstreams, locations

    def _generate_loki_endpoints_nginx_config(self) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
        """Generate nginx upstreams and locations for loki endpoints routing."""
        upstreams: List[NginxUpstream] = []
        locations: List[NginxLocationConfig] = []

        for unit_name, address in self.loki_endpoints_by_unit.items():
            unit_name_sanitized = unit_name.replace("/", "-")
            parsed_address = urlparse(address)
            upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{unit_name_sanitized}"

            upstreams.append(
                NginxUpstream(
                    name=upstream_name,
                    port=parsed_address.port,  # type: ignore
                    address_lookup_key=unit_name
                )
            )

            locations.append(
                NginxLocationConfig(
                    path=PROXY_WORKER_TELEMETRY_PATHS["loki_endpoint"].format(unit=unit_name_sanitized),
                    backend=upstream_name,
                    backend_url=parsed_address.path,
                    upstream_tls=parsed_address.scheme.endswith('s'),
                    is_grpc=False,
                )
            )

        return upstreams, locations

    def _generate_remote_write_endpoints_nginx_config(self) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
        """Generate nginx upstreams and locations for remote write endpoints routing."""
        upstreams: List[NginxUpstream] = []
        locations: List[NginxLocationConfig] = []

        if not self._remote_write_endpoints_getter:
            return upstreams, locations

        remote_write_endpoints: List[RemoteWriteEndpoint] = self._remote_write_endpoints_getter()

        for endpoints in remote_write_endpoints:
            parsed_address = urlparse(endpoints["url"])
            unit_name_sanitized = parsed_address.hostname.split(".")[0]  # type: ignore
            upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{unit_name_sanitized}"

            upstreams.append(
                NginxUpstream(
                    name=upstream_name,
                    port=parsed_address.port,  # type: ignore
                    address_lookup_key=unit_name_sanitized
                )
            )

            locations.append(
                NginxLocationConfig(
                    path=PROXY_WORKER_TELEMETRY_PATHS["remote_write_endpoint"].format(unit=unit_name_sanitized),
                    backend=upstream_name,
                    backend_url=parsed_address.path,
                    upstream_tls=parsed_address.scheme.endswith('s'),
                    is_grpc=False,
                )
            )

        return upstreams, locations

    def _generate_tracing_urls_nginx_config(self) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
        """Generate nginx upstreams and locations for routing charm and workload tracing."""
        upstreams: List[NginxUpstream] = []
        locations: List[NginxLocationConfig] = []
        created_upstreams: Set[str] = set()

        # Merge both dictionaries to avoid duplicates
        all_tracing_urls = {**self._charm_tracing_receivers_urls, **self._workload_tracing_receivers_urls}

        for protocol, address in all_tracing_urls.items():
            parsed_address = urlparse(address)  # we are tracing ourselves. ignore
            if parsed_address.hostname == self.hostname:
                continue
            upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{protocol}"

            # Create only unique upstreams
            if upstream_name not in created_upstreams:
                upstreams.append(
                    NginxUpstream(
                        name=upstream_name,
                        port=parsed_address.port,  # type: ignore
                        address_lookup_key=upstream_name,
                    )
                )
                created_upstreams.add(upstream_name)

            if protocol in self._charm_tracing_receivers_urls:
                locations.append(
                    NginxLocationConfig(
                        path=PROXY_WORKER_TELEMETRY_PATHS["charm_tracing_receivers_urls"].format(protocol=protocol),
                        backend=upstream_name,
                        backend_url=parsed_address.path,
                        upstream_tls=parsed_address.scheme.endswith('s'),
                        is_grpc=False,
                    )
                )

            if protocol in self._workload_tracing_receivers_urls:
                locations.append(
                    NginxLocationConfig(
                        path=PROXY_WORKER_TELEMETRY_PATHS["workload_tracing_receivers_urls"].format(protocol=protocol),
                        backend=upstream_name,
                        backend_url=parsed_address.path,
                        upstream_tls=parsed_address.scheme.endswith('s'),
                        is_grpc=False,  # FIXME: GRPC must be allowed. See issue.
                    )
                )

        return upstreams, locations
