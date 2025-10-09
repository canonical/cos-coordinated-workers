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
    Union,
    cast,
)
from urllib.parse import urlparse

import cosl
import ops
import ops_tracing
import pydantic
import yaml
from cosl.interfaces.datasource_exchange import DatasourceExchange
from lightkube import Client
from opentelemetry import trace
from ops import StatusBase

from coordinated_workers import (
    service_mesh,
    worker,
    worker_telemetry,
)
from coordinated_workers.helpers import check_libs_installed
from coordinated_workers.interfaces.cluster import ClusterProvider, RemoteWriteEndpoint
from coordinated_workers.nginx import (
    Nginx,
    NginxConfig,
    NginxMappingOverrides,
    NginxPrometheusExporter,
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
    "charms.istio_beacon_k8s.v0.service_mesh",
)

from charms.catalogue_k8s.v1.catalogue import CatalogueConsumer, CatalogueItem
from charms.data_platform_libs.v0.s3 import S3Requirer
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.istio_beacon_k8s.v0.service_mesh import (
    AppPolicy,
    UnitPolicy,
    reconcile_charm_labels,
)
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
    """The set of roles that need to be allocated for the deployment to be considered robust according to the official recommendations/guidelines."""

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
        "service-mesh": Optional[str],
        "service-mesh-provide-cmr-mesh": Optional[str],
        "service-mesh-require-cmr-mesh": Optional[str],
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
        worker_telemetry_proxy_config: Optional[
            worker_telemetry.WorkerTelemetryProxyConfig
        ] = None,
        charm_mesh_policies: Optional[List[Union[AppPolicy, UnitPolicy]]] = None,
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
            worker_telemetry_proxy_config: Configuration including HTTP and HTTPS ports for proxying workers telemetry data via coordinator.
                Leaving it blank disables the worker telemetry proxying.
            charm_mesh_policies: Charm specific service mesh policies.
                These policies will only govern the traffic incoming to the coordinator.
                These policies will be added to the Coordinator defined policies that are common for all coordinated-workers charms.

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
        # the charm owned nginx config is preserved and deep copied for patching with worker telemetry config (if enabled)
        self._nginx_config = nginx_config.copy()
        self._roles_config = roles_config
        self._workload_tracing_protocols = workload_tracing_protocols
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

        self._upstreams_to_addresses = self.cluster.gather_addresses_by_role()

        self._proxy_worker_telemetry_port: Optional[int] = None
        if worker_telemetry_proxy_config:
            self._proxy_worker_telemetry_port = (
                worker_telemetry_proxy_config.https_port
                if self.tls_available
                else worker_telemetry_proxy_config.http_port
            )

        # NOTE: setup nginx after tracing requirers as it uses logging and tracing endpoints
        # for config building
        self.nginx = Nginx(
            self._charm,
            config_getter=partial(self._nginx_config.get_config, self._upstreams_to_addresses),
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

        # service mesh
        self._mesh = service_mesh.initialize(
            endpoints=self._endpoints,
            charm=self._charm,
            nginx_exporter_port=self.nginx_exporter.port,
            proxy_worker_telemetry_paths=worker_telemetry.PROXY_WORKER_TELEMETRY_PATHS,
            proxy_worker_telemetry_port=self._proxy_worker_telemetry_port,
            charm_mesh_policies=charm_mesh_policies,
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

        # reconcile the custom lables added to the application pods.
        self._reconcile_charm_labels()

        # certificates must be synced before we reconcile the workloads; otherwise changes in the certs may go unnoticed.
        self._certificates.sync()
        # keep this on top right after certificates sync
        self._setup_charm_tracing()

        # reconcile workloads
        self._reconcile_worker_telemetry()  # keep this above nginx.reconcile() since it modifies the nginx config
        self.nginx.reconcile()
        self.nginx_exporter.reconcile()

        # reconcile relations
        self._reconcile_cluster_relations()
        self._reconcile_mesh_policies()
        self._consolidate_alert_rules()
        self._scraping.set_scrape_job_spec()  # type: ignore
        self._worker_logging.reload_alerts()

        if (catalogue := self._catalogue) and (item := self._catalogue_item):
            catalogue.update_item(item)

    ######################
    # UTILITY PROPERTIES #
    ######################

    def _tracing_receivers_urls(
        self,
        requirer: TracingEndpointRequirer,
        _type: str,
        ignore_proxy: bool = False,
    ) -> Dict[str, str]:
        """Return the trace receiving urls per requested protocol."""
        endpoints = requirer.get_all_endpoints()  # type: ignore
        receivers = endpoints.receivers if endpoints else ()

        if (proxy_worker_telemetry_port := self._proxy_worker_telemetry_port) and not ignore_proxy:
            return worker_telemetry.proxy_tracing_receivers_urls(
                hostname=self.app_hostname(
                    self.hostname, self._charm.app.name, self._charm.model.name
                ),
                proxy_worker_telemetry_port=proxy_worker_telemetry_port,
                tls_available=self.tls_available,
                tracing_target_type=_type,
                protocols=[receiver.protocol.name for receiver in receivers],  # type: ignore
            )

        return {receiver.protocol.name: receiver.url for receiver in receivers}  # type: ignore

    @property
    def _charm_tracing_receivers_urls(self) -> Dict[str, str]:
        """Return the charm tracing receiver urls per receiver protocol.

        When worker telemetry proxy is enabled (when not self tracing), returns the coordinator's proxy url which maps to the upstream.

        Returns:
            A dictionary of tracing protocols and the respective tracing receiver urls.
            {
                "otlp_http": "http://tempo.tempo.svc.cluster.local:4318",
            }

            Or when worker telemetry proxy is enabled,
            {
                "otlp_http": "http://tempo-0.tempo-endpoints.tempo.svc.cluster.local:3300/proxy/charm-tracing/otlp_http/",
            }
        """
        return self._tracing_receivers_urls(self.charm_tracing, "charm-tracing")

    @property
    def _workload_tracing_receivers_urls(self) -> Dict[str, str]:
        """Return the workload tracing receiver urls per receiver protocol.

        When worker telemetry proxy is enabled (when not self tracing), returns the coordinator's proxy url which maps to the upstream.

        Returns:
            A dictionary of tracing protocols and the respective tracing receiver urls.
            {
                "otlp_http": "http://tempo.tempo.svc.cluster.local:4318",
            }

            Or when worker telemetry proxy is enabled,
            {
                "otlp_http": "http://tempo-0.tempo-endpoints.tempo.svc.cluster.local:3300/proxy/workload-tracing/otlp_http/",
            }
        """
        return self._tracing_receivers_urls(self.workload_tracing, "workload-tracing")

    @property
    def remote_write_endpoints(self) -> Optional[List[RemoteWriteEndpoint]]:
        """Return the remote write endpoints.

        When worker telemetry proxy is enabled, returns the coordinator's proxy url which maps to the upstream.

        Returns:
            A list of remote write endpoints:
            [
                http://prometheus-0.prometheus-endpoints.tempo.svc.cluster.local:9090/api/v1/write,
            ]

            Or when the worker telemetry proxy is enabled,
            [
                http://tempo-0.tempo-endpoints.tempo.svc.cluster.local:3300/proxy/remote-write/prometheus-0/write,
            ]
        """
        if not self._remote_write_endpoints_getter:
            return None

        endpoints = self._remote_write_endpoints_getter()
        if proxy_worker_telemetry_port := self._proxy_worker_telemetry_port:
            return worker_telemetry.proxy_remote_write_endpoints(
                hostname=self.app_hostname(
                    self.hostname, self._charm.app.name, self._charm.model.name
                ),
                proxy_worker_telemetry_port=proxy_worker_telemetry_port,
                tls_available=self.tls_available,
                endpoints=endpoints,
            )
        return endpoints

    @property
    def _upstream_loki_endpoints_by_unit(self) -> Dict[str, str]:
        """Return the Loki endpoints obtained from the `logging` relation per loki unit.

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

    @property
    def loki_endpoints_by_unit(self) -> Dict[str, str]:
        """Return the Loki endpoints per loki unit.

        When worker telemetry proxy is enabled, returns the coordinator's proxy url which maps to the upstream.

        Returns:
            A dictionary of remote units and the respective Loki endpoint.
            {
                "loki/0": "http://loki:3100/loki/api/v1/push",
                "another-loki/0": "http://another-loki:3100/loki/api/v1/push",
            }

            Or when worker telemetry proxy is enabled,
            {
                "loki/0": "http://tempo-0.temo-endpoints.tempo.svc.cluster.local:3300/proxy/loki/loki-0/push",
                "another-loki/0": "http://tempo-0.temo-endpoints.tempo.svc.cluster.local:3300/proxy/loki/another-loki-0/push",
            }
        """
        relations: List[ops.Relation] = self.model.relations.get(self._endpoints["logging"], [])

        if proxy_worker_telemetry_port := self._proxy_worker_telemetry_port:
            return worker_telemetry.proxy_loki_endpoints_by_unit(  # type: ignore
                hostname=self.app_hostname(
                    self.hostname, self._charm.app.name, self._charm.model.name
                ),
                proxy_worker_telemetry_port=proxy_worker_telemetry_port,
                tls_available=self.tls_available,
                logging_relations=relations,
            )

        else:
            return self._upstream_loki_endpoints_by_unit

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
            if self._proxy_worker_telemetry_port:
                # when proxied through nginx
                # address: address of the coordinator
                # path: location used in the nginx config for proxying worker metric

                targets = [
                    f"{self.app_hostname(self.hostname, self._charm.app.name, self._charm.model.name)}:{self._proxy_worker_telemetry_port}"
                ]
                metrics_path = worker_telemetry.PROXY_WORKER_TELEMETRY_PATHS["metrics"].format(
                    unit=worker_topology["unit"].replace("/", "-"),
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
            "static_configs": [{"targets": [f"{self.hostname}:{self.nginx_exporter.port}"]}]
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

    @property
    def _coordinated_workers_solution_labels(self) -> Dict[str, str]:
        """Labels to be applied to all pods in this coordinated-workers solution."""
        return {"app.kubernetes.io/part-of": f"{self._charm.app.name}"}

    @property
    def _worker_labels(self) -> Dict[str, str]:
        """Labels to be applied to worker pods."""
        labels = self._coordinated_workers_solution_labels
        if self._mesh and (mesh_labels := cast(Dict[str, str], self._mesh.labels())):  # type: ignore
            labels.update(mesh_labels)
        return labels

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

    def _reconcile_charm_labels(self) -> None:
        """Update any custom pod labels we require."""
        reconcile_charm_labels(
            client=Client(namespace=self._charm.model.name),
            app_name=self._charm.app.name,
            namespace=self._charm.model.name,
            label_configmap_name=f"{self._charm.app.name}-pod-labels",
            labels=self._coordinated_workers_solution_labels,
        )

    def _reconcile_mesh_policies(self) -> None:
        """Reconcile all the cluster internal mesh policies."""
        service_mesh.reconcile_cluster_internal_mesh_policies(
            mesh=self._mesh,
            cluster=self.cluster,
            charm=self._charm,
            target_selector_labels=self._coordinated_workers_solution_labels,
            logger=logger,
        )

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
            loki_endpoints=self.loki_endpoints_by_unit,
            # all arguments below are optional:
            ca_cert=tls_config.ca_cert if tls_config else None,
            server_cert=tls_config.server_cert if tls_config else None,
            # FIXME: We're relying on a private method from the TLS library
            # https://github.com/canonical/cos-coordinated-workers/issues/16
            privkey_secret_id=self.cluster.grant_privkey(
                self._certificates._get_private_key_secret_label()  # type: ignore
            ),
            charm_tracing_receivers=self._charm_tracing_receivers_urls,
            workload_tracing_receivers=self._workload_tracing_receivers_urls,
            remote_write_endpoints=self.remote_write_endpoints,
            s3_tls_ca_chain=self.s3_connection_info.ca_cert,
            worker_labels=self._worker_labels,
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

    def _reconcile_worker_telemetry(self):
        """Update the nginx config to route all the worker telemetry via the coordinator."""
        if not self._proxy_worker_telemetry_port:
            return

        # NOTE: use actual upstream telemetry addresses here! Or you will be proxying the proxy addresses.
        worker_telemetry.configure(
            nginx_config=self._nginx_config,
            tls_available=self.tls_available,
            workload_tracing_protocols=self._workload_tracing_protocols or [],
            worker_topology=self.cluster.gather_topology(),
            worker_metrics_port=self._worker_metrics_port,
            charm_tracing_receivers_urls=self._tracing_receivers_urls(
                self.charm_tracing, "charm-tracing", ignore_proxy=True
            ),
            workload_tracing_receivers_urls=self._tracing_receivers_urls(
                self.workload_tracing, "workload-tracing", ignore_proxy=True
            ),
            loki_endpoints_by_unit=self._upstream_loki_endpoints_by_unit,
            remote_write_endpoints_getter=self._remote_write_endpoints_getter,
            proxy_worker_telemetry_port=self._proxy_worker_telemetry_port,
        )
        worker_telemetry.configure_upstreams(
            upstreams_to_addresses=self._upstreams_to_addresses,
            addresses_by_unit=self.cluster.gather_addresses_by_unit(),
            remote_write_endpoints_getter=self._remote_write_endpoints_getter,
            charm_tracing_receivers_urls=self._tracing_receivers_urls(
                self.charm_tracing, "charm-tracing", ignore_proxy=True
            ),
            workload_tracing_receivers_urls=self._tracing_receivers_urls(
                self.workload_tracing, "workload-tracing", ignore_proxy=True
            ),
            loki_endpoints_by_unit=self._upstream_loki_endpoints_by_unit,
        )
