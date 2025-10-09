#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Helper functions to enable the coordinator to proxy worker's telemetry through it."""

import dataclasses
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple, Union
from urllib.parse import urlparse

import ops
from charms.tempo_coordinator_k8s.v0.tracing import ReceiverProtocol

from coordinated_workers.interfaces.cluster import RemoteWriteEndpoint
from coordinated_workers.nginx import NginxConfig, NginxLocationConfig, NginxUpstream

WorkerTopology = List[Dict[str, str]]
RemoteWriteEndpointGetter = Optional[Callable[[], List[RemoteWriteEndpoint]]]


# Paths for proxied worker telemetry urlparse
PROXY_WORKER_TELEMETRY_PATHS = {
    "metrics": "/proxy/worker/{unit}/metrics",
    "logging": "/proxy/loki/{unit}/push",
    "remote-write": "/proxy/remote-write/{unit}/write",
    "charm-tracing": "/proxy/charm-tracing/{protocol}/",
    "workload-tracing": "/proxy/workload-tracing/{protocol}/",
}
PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX = "worker-telemetry-proxy"


@dataclasses.dataclass
class WorkerTelemetryProxyConfig:
    """Worker telemetry proxy configuration object."""

    http_port: int
    https_port: int


@dataclasses.dataclass
class _WorkerTelemetryNginxConfigSpec:
    """Specification for generating nginx config for different worker telemetry types."""

    upstream_name: str
    upstream_port: int
    upstream_lookup_key: str
    location_path: str
    location_backend_url: str
    location_upstream_tls: bool
    location_modifier: Optional[str] = None
    location_rewrite: Optional[List[str]] = None
    is_grpc: bool = False


def configure_upstreams(
    upstreams_to_addresses: Dict[str, Set[str]],
    addresses_by_unit: Dict[str, Set[str]],
    remote_write_endpoints_getter: RemoteWriteEndpointGetter,
    charm_tracing_receivers_urls: Dict[str, str],
    workload_tracing_receivers_urls: Dict[str, str],
    loki_endpoints_by_unit: Dict[str, str],
) -> None:
    """Update upstream name to address mapper to include the required servers/clients that send/receive worker telemetry.

    This function updates the `upstreams_to_addresses` object inplace to include the upstream logging, tracing, and remote_write endpoints with corresponding upstream names.
    The endpoints used in this mapping are upstream endpoints that actually receive the telemetry.

    Args:
        upstreams_to_addresses (dict): The upstreams_to_addresses to update with the telemetry upstream information
        addresses_by_unit (dict): The address of all the worker units
        remote_write_endpoints_getter (callable): A function that returns the upstream remote_write endpoints
        charm_tracing_receivers_urls (dict): The upstream charm tracings urls per tracing protocol
        workload_tracing_receivers_urls (dict): The upstream workload tracing urls per tracing protocol
        loki_endpoints_by_unit (dict): The upstream push endpoint addresses for log forwarding
    """
    # Merge role-based and unit-based addresses collection for nginx config
    # Every unit will get its own upstream for metric proxying
    upstreams_to_addresses.update(addresses_by_unit)

    # loki upstream to address mapper
    for loki_unit, address in loki_endpoints_by_unit.items():
        p = urlparse(address)
        upstreams_to_addresses[loki_unit] = {p.hostname}  # type: ignore

    # remote write upstream to address mapper
    if remote_write_endpoints_getter:
        for endpoint in remote_write_endpoints_getter():
            p = urlparse(endpoint["url"])
            remote_write_unit = p.hostname.split(".")[0]  # type: ignore
            upstreams_to_addresses[remote_write_unit] = {p.hostname}  # type: ignore

    # tracing upstream to address mapper (both charm and workload)
    tracing_configs = [
        ("charm", charm_tracing_receivers_urls),
        ("workload", workload_tracing_receivers_urls),
    ]

    for tracing_type, receivers_urls in tracing_configs:
        for protocol, address in receivers_urls.items():
            p = urlparse(address)
            upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{tracing_type}-{protocol}"
            upstreams_to_addresses[upstream_name] = {p.hostname}  # type: ignore


def configure(
    tls_available: bool,
    nginx_config: NginxConfig,
    worker_topology: WorkerTopology,
    workload_tracing_protocols: List[ReceiverProtocol],
    remote_write_endpoints_getter: RemoteWriteEndpointGetter,
    worker_metrics_port: int,
    charm_tracing_receivers_urls: Dict[str, str],
    workload_tracing_receivers_urls: Dict[str, str],
    loki_endpoints_by_unit: Dict[str, str],
    proxy_worker_telemetry_port: int,
) -> None:
    """Modify nginx configuration to proxy worker telemetry via the coordinator.

    This function updates the `nginx_config` object inplace to include the required nginx configurations that will route the telemetry data from the workers via the coordinator.

    Args:
        tls_available (bool): If TLS is enabled.
        nginx_config (NginxConfig): The nginx configuration of the coordinator to update with the required worker telemetry routes
        worker_topology (WorkerTopology): information about the workers in the cluster
        workload_tracing_protocols (list): List of tracing protocols used by the workers workloads to forward traces
        remote_write_endpoints_getter (callable): A function that returns the upstream remote_write endpoints
        worker_metrics_port (int): The port on which the workers expose their metrics
        charm_tracing_receivers_urls (dict): The upstream charm tracings urls per tracing protocol
        workload_tracing_receivers_urls (dict): The upstream workload tracing urls per tracing protocol
        loki_endpoints_by_unit (dict): The upstream push endpoint addresses for log forwarding
        proxy_worker_telemetry_port (int): The port on the coordinator that listens for worker telemetry data
    """
    _validate_proxy_worker_telemetry_setup(workload_tracing_protocols)
    _setup_proxy_worker_telemetry(
        tls_available=tls_available,
        nginx_config=nginx_config,
        worker_topology=worker_topology,
        worker_metrics_port=worker_metrics_port,
        remote_write_endpoints_getter=remote_write_endpoints_getter,
        charm_tracing_receivers_urls=charm_tracing_receivers_urls,
        workload_tracing_receivers_urls=workload_tracing_receivers_urls,
        loki_endpoints_by_unit=loki_endpoints_by_unit,
        proxy_worker_telemetry_port=proxy_worker_telemetry_port,
    )


def _validate_proxy_worker_telemetry_setup(
    workload_tracing_protocols: List[ReceiverProtocol],
) -> None:
    """Check if a valid proxy setup for worker telemetry is possible."""
    # if no workload protocol is defined, let the TracingEndpointRequirer handle this
    # FIXME: GRPC should be allowed. Create an issue and link here.
    # bail out for now, this is bad and we can't fix it.
    for protocol in workload_tracing_protocols:
        if "grpc" in protocol:
            raise RuntimeError(
                "bad config. This coordinator is requesting grpc workload tracing endpoints, "
                "but that won't work with the current telemetry proxy configuration."
            )


def _setup_proxy_worker_telemetry(
    nginx_config: NginxConfig,
    tls_available: bool,
    worker_topology: WorkerTopology,
    worker_metrics_port: int,
    charm_tracing_receivers_urls: Dict[str, str],
    workload_tracing_receivers_urls: Dict[str, str],
    loki_endpoints_by_unit: Dict[str, str],
    proxy_worker_telemetry_port: int,
    remote_write_endpoints_getter: RemoteWriteEndpointGetter,
) -> None:
    """Extend the nginx configuration with configurations required proxying worker telemetry.

    The proxying is done for worker telemetry that is both pulled and pushed.
    """
    # check if the worker telemetry can be validly proxied, if not log it as an error

    # Extend nginx config with worker metrics if enabled
    if worker_topology:
        telemetry_upstreams, telemetry_locations = _generate_worker_telemetry_nginx_config(
            worker_metrics_port=worker_metrics_port,
            worker_topology=worker_topology,
            remote_write_endpoints_getter=remote_write_endpoints_getter,
            charm_tracing_receivers_urls=charm_tracing_receivers_urls,
            workload_tracing_receivers_urls=workload_tracing_receivers_urls,
            loki_endpoints_by_unit=loki_endpoints_by_unit,
            proxy_worker_telemetry_port=proxy_worker_telemetry_port,
            tls_available=tls_available,
        )
        nginx_config.extend_upstream_configs(telemetry_upstreams)
        nginx_config.update_server_ports_to_locations(telemetry_locations, overwrite=False)


def _generate_worker_telemetry_nginx_config(
    worker_topology: List[Dict[str, str]],
    remote_write_endpoints_getter: RemoteWriteEndpointGetter,
    worker_metrics_port: int,
    charm_tracing_receivers_urls: Dict[str, str],
    workload_tracing_receivers_urls: Dict[str, str],
    loki_endpoints_by_unit: Dict[str, str],
    proxy_worker_telemetry_port: int,
    tls_available: bool,
) -> Tuple[List[NginxUpstream], Dict[int, List[NginxLocationConfig]]]:
    """Generate nginx upstreams and locations for proxying worker telemetry."""
    upstreams_worker_metrics, locations_worker_metrics = _generate_worker_metrics_nginx_config(
        worker_topology, worker_metrics_port=worker_metrics_port, tls_available=tls_available
    )
    upstreams_loki_endpoints, locations_loki_endpoints = _generate_loki_endpoints_nginx_config(
        loki_endpoints_by_unit=loki_endpoints_by_unit
    )
    upstreams_remote_write_endpoints, locations_remote_write_endpoints = (
        _generate_remote_write_endpoints_nginx_config(
            remote_write_endpoints_getter=remote_write_endpoints_getter
        )
    )
    upstreams_tracing_urls, locations_tracing_urls = _generate_tracing_urls_nginx_config(
        charm_tracing_receivers_urls=charm_tracing_receivers_urls,
        workload_tracing_receivers_urls=workload_tracing_receivers_urls,
    )

    upstreams: List[NginxUpstream] = [
        *upstreams_worker_metrics,
        *upstreams_loki_endpoints,
        *upstreams_remote_write_endpoints,
        *upstreams_tracing_urls,
    ]
    locations: Dict[int, List[NginxLocationConfig]] = {
        proxy_worker_telemetry_port: [  # type: ignore
            *locations_worker_metrics,
            *locations_loki_endpoints,
            *locations_remote_write_endpoints,
            *locations_tracing_urls,
        ]
    }

    return upstreams, locations


def _generate_nginx_config_from_spec(
    specs: List[_WorkerTelemetryNginxConfigSpec],
) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
    """Generate nginx upstreams and locations from the provided _WorkerTelemetryNginxConfigSpec list."""
    upstreams: List[NginxUpstream] = []
    locations: List[NginxLocationConfig] = []
    created_upstreams: Set[str] = set()

    for spec in specs:
        # Create upstream if we haven't already
        if spec.upstream_name not in created_upstreams:
            upstreams.append(
                NginxUpstream(
                    name=spec.upstream_name,
                    port=spec.upstream_port,
                    address_lookup_key=spec.upstream_lookup_key,
                )
            )
            created_upstreams.add(spec.upstream_name)

        # Create location config
        location_kwargs = {
            "path": spec.location_path,
            "backend": spec.upstream_name,
            "backend_url": spec.location_backend_url,
            "upstream_tls": spec.location_upstream_tls,
            "is_grpc": spec.is_grpc,
        }

        if spec.location_modifier:
            location_kwargs["modifier"] = spec.location_modifier

        if spec.location_rewrite:
            location_kwargs["rewrite"] = spec.location_rewrite  # type: ignore

        locations.append(NginxLocationConfig(**location_kwargs))  # type: ignore

    return upstreams, locations


def _generate_worker_metrics_nginx_config(
    worker_topology: List[Dict[str, str]], tls_available: bool, worker_metrics_port: int
) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
    """Generate nginx config for proxying worker metrics via the coordinator."""
    specs: List[_WorkerTelemetryNginxConfigSpec] = []

    for worker in worker_topology:
        unit_name = worker["unit"]
        unit_name_sanitized = unit_name.replace("/", "-")
        upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{unit_name_sanitized}"

        specs.append(
            _WorkerTelemetryNginxConfigSpec(
                upstream_name=upstream_name,
                upstream_port=worker_metrics_port,
                upstream_lookup_key=unit_name,
                location_path=PROXY_WORKER_TELEMETRY_PATHS["metrics"].format(
                    unit=unit_name_sanitized
                ),
                location_backend_url="/metrics",
                location_upstream_tls=tls_available,
                location_modifier="=",
            )
        )

    return _generate_nginx_config_from_spec(specs)


def _generate_remote_write_endpoints_nginx_config(
    remote_write_endpoints_getter: RemoteWriteEndpointGetter,
) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
    """Generate the nginx config for proxying remote write endpoints via the coordinator."""
    if not remote_write_endpoints_getter:
        return [], []

    specs: List[_WorkerTelemetryNginxConfigSpec] = []
    remote_write_endpoints: List[RemoteWriteEndpoint] = remote_write_endpoints_getter()

    for remote_write_endpoint in remote_write_endpoints:
        parsed_address = urlparse(remote_write_endpoint["url"])
        unit_name_sanitized = parsed_address.hostname.split(".")[0]  # type: ignore
        upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{unit_name_sanitized}"

        specs.append(
            _WorkerTelemetryNginxConfigSpec(
                upstream_name=upstream_name,
                upstream_port=parsed_address.port,  # type: ignore
                upstream_lookup_key=unit_name_sanitized,
                location_path=PROXY_WORKER_TELEMETRY_PATHS["remote-write"].format(
                    unit=unit_name_sanitized
                ),
                location_backend_url=parsed_address.path,
                location_upstream_tls=parsed_address.scheme.endswith("s"),
                location_modifier="=",
            )
        )

    return _generate_nginx_config_from_spec(specs)


def _generate_loki_endpoints_nginx_config(
    loki_endpoints_by_unit: Dict[str, str],
) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
    """Generate the nginx config for proxying loki endpoints via the coordinator."""
    specs: List[_WorkerTelemetryNginxConfigSpec] = []

    for unit_name, address in loki_endpoints_by_unit.items():
        parsed_address = urlparse(address)
        unit_name_sanitized = unit_name.replace("/", "-")
        upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{unit_name_sanitized}"

        specs.append(
            _WorkerTelemetryNginxConfigSpec(
                upstream_name=upstream_name,
                upstream_port=parsed_address.port,  # type: ignore
                upstream_lookup_key=unit_name,
                location_path=PROXY_WORKER_TELEMETRY_PATHS["logging"].format(
                    unit=unit_name_sanitized
                ),
                location_backend_url=parsed_address.path,
                location_upstream_tls=parsed_address.scheme.endswith("s"),
                location_modifier="=",
            )
        )

    return _generate_nginx_config_from_spec(specs)


def _generate_tracing_urls_nginx_config(
    charm_tracing_receivers_urls: Dict[str, str],
    workload_tracing_receivers_urls: Dict[str, str],
) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
    """Generate the nginx upstreams and locations for charm and workload tracing."""
    specs: List[_WorkerTelemetryNginxConfigSpec] = []

    tracing_configs = [
        ("charm", charm_tracing_receivers_urls, PROXY_WORKER_TELEMETRY_PATHS["charm-tracing"]),
        (
            "workload",
            workload_tracing_receivers_urls,
            PROXY_WORKER_TELEMETRY_PATHS["workload-tracing"],
        ),
    ]

    for tracing_type, receivers_urls, path_template in tracing_configs:
        for protocol, address in receivers_urls.items():
            parsed_address = urlparse(address)
            upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{tracing_type}-{protocol}"
            location_path = path_template.format(protocol=protocol)

            specs.append(
                _WorkerTelemetryNginxConfigSpec(
                    upstream_name=upstream_name,
                    upstream_port=parsed_address.port,  # type: ignore
                    upstream_lookup_key=upstream_name,
                    location_path=location_path,
                    location_backend_url=parsed_address.path,
                    location_upstream_tls=parsed_address.scheme.endswith("s"),
                    location_rewrite=[f"^{location_path}(.*)", "/$1", "break"],
                )
            )

    return _generate_nginx_config_from_spec(specs)


def proxy_loki_endpoints_by_unit(
    hostname: str,
    proxy_worker_telemetry_port: int,
    tls_available: bool,
    logging_relations: Iterable[ops.Relation],
) -> Dict[str, str]:
    """Return the loki endpoints proxied via the coordinator per loki unit.

    The proxy URL follows the following convention:
    {scheme}://{hostname}:{proxy_worker_telemetry_port}/proxy/loki/{loki_unit}/push

    Args:
        hostname (str): The hostname of the coordinator
        proxy_worker_telemetry_port (int): The port to listen for incoming tracing telemetry from the worker
        tls_available (bool): Is TLS enabled
        logging_relations (list): List of actual non-proxied remote write endpoints available to the cluster
    """
    endpoints: Dict[str, str] = {}
    for relation in logging_relations:
        for unit in relation.units:
            scheme = "https" if tls_available else "http"
            worker_tlm_path = PROXY_WORKER_TELEMETRY_PATHS["logging"]
            sanitized_worker_tlm_path = worker_tlm_path.format(unit=unit.name.replace("/", "-"))
            endpoints[unit.name] = (
                f"{scheme}://{hostname}:{proxy_worker_telemetry_port}{sanitized_worker_tlm_path}"
            )
    return endpoints


def proxy_remote_write_endpoints(
    hostname: str,
    proxy_worker_telemetry_port: int,
    tls_available: bool,
    endpoints: List[RemoteWriteEndpoint],
) -> Union[List[RemoteWriteEndpoint], None]:
    """Return the remote write endpoints proxied via the coordinator.

    The proxy URL follows the following convention:
    {scheme}://{hostname}:{proxy_worker_telemetry_port}/proxy/remote-write/{remote_write_unit}/write

    Args:
        hostname (str): The hostname of the coordinator
        proxy_worker_telemetry_port (int): The port to listen for incoming tracing telemetry from the worker
        tls_available (bool): Is TLS enabled
        endpoints (list): List of actual non-proxied remote write endpoints available to the cluster
    """
    proxied_endpoints: List[RemoteWriteEndpoint] = []

    for remote_write_endpoint in endpoints:
        parsed_address = urlparse(remote_write_endpoint["url"])
        unit = parsed_address.hostname.split(".")[0]  # type: ignore
        scheme = "https" if tls_available else "http"
        proxy_url = f"{scheme}://{hostname}:{proxy_worker_telemetry_port}{PROXY_WORKER_TELEMETRY_PATHS['remote-write'].format(unit=unit)}"
        proxied_endpoints.append(RemoteWriteEndpoint(url=proxy_url))

    return proxied_endpoints


def proxy_tracing_receivers_urls(
    hostname: str,
    proxy_worker_telemetry_port: int,
    tls_available: bool,
    tracing_target_type: str,
    protocols: List[str],  # should this be a literal instead?
) -> Dict[str, str]:
    """Return the tracing receivers urls proxied via the coordinator per tracing protocol (otel_http, otel_grpc, etc.).

    The proxy URL follows the following convention:
    {scheme}://{hostname}:{proxy_worker_telemetry_port}/proxy/{tracing_target_type}/{protocol}/

    Args:
        hostname (str): The hostname of the coordinator
        proxy_worker_telemetry_port (int): The port to listen for incoming tracing telemetry from the worker
        tls_available (bool): Is TLS enabled
        tracing_target_type (str): Type of target that is being traced. Supports "charm-tracing" and "workload-tracing"
        protocols (list): List of tracing protocols used by the worker
    """
    urls: Dict[str, str] = {}

    for protocol in protocols:
        scheme = "https" if tls_available else "http"
        proxy_url = f"{scheme}://{hostname}:{proxy_worker_telemetry_port}{PROXY_WORKER_TELEMETRY_PATHS[tracing_target_type].format(protocol=protocol)}"
        urls.update({protocol: proxy_url})

    return urls
