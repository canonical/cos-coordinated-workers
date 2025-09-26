import dataclasses
from typing import Callable, Dict, List, Optional, Set, Tuple, TypeAlias, Iterable, Union
from urllib.parse import urlparse

import ops

from charms.tempo_coordinator_k8s.v0.tracing import ReceiverProtocol

from coordinated_workers.interfaces.cluster import RemoteWriteEndpoint
from coordinated_workers.nginx import NginxConfig, NginxLocationConfig, NginxUpstream

WorkerTopology: TypeAlias = List[Dict[str, str]]
RemoteWriteEndpointGetter: TypeAlias = Optional[Callable[[], List[RemoteWriteEndpoint]]]


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

    http: int
    https: int


def configure_upstreams(
    upstreams_to_addresses: Dict[str, Set[str]],
    addresses_by_unit: Dict[str, Set[str]],
    remote_write_endpoints_getter: RemoteWriteEndpointGetter,
    charm_tracing_receivers_urls: Dict[str, str],
    workload_tracing_receivers_urls: Dict[str, str],
    loki_endpoints_by_unit: Dict[str, str],
) -> None:
    """Update the upstreams in the nginx config to include the required servers/clients that send/receive worker telemetry."""
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
    worker_telemetry_proxy_config: WorkerTelemetryProxyConfig,
    nginx_config: NginxConfig,
    worker_topology: WorkerTopology,
    workload_tracing_protocols: List[ReceiverProtocol],
    remote_write_endpoints_getter: RemoteWriteEndpointGetter,
    worker_metrics_port: int,
    charm_tracing_receivers_urls: Dict[str, str],
    workload_tracing_receivers_urls: Dict[str, str],
    loki_endpoints_by_unit: Dict[str, str],
        proxy_worker_telemetry_port:int
):
    """Modify nginx configuration to proxy worker telemetry."""
    _validate_proxy_worker_telemetry_setup(workload_tracing_protocols)
    _setup_proxy_worker_telemetry(
        nginx_config=nginx_config,
        worker_topology=worker_topology,
        worker_metrics_port=worker_metrics_port,
        remote_write_endpoints_getter=remote_write_endpoints_getter,
        charm_tracing_receivers_urls=charm_tracing_receivers_urls,
        workload_tracing_receivers_urls=workload_tracing_receivers_urls,
        loki_endpoints_by_unit=loki_endpoints_by_unit,
        proxy_worker_telemetry_port=proxy_worker_telemetry_port
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
    worker_topology: WorkerTopology,
    worker_metrics_port: int,
    charm_tracing_receivers_urls: Dict[str, str],
    workload_tracing_receivers_urls: Dict[str, str],
    loki_endpoints_by_unit: Dict[str, str],
    proxy_worker_telemetry_port: int,
    remote_write_endpoints_getter: RemoteWriteEndpointGetter,
) -> None:
    """Extends nginx configuration with configurations required proxying worker telemetry to and from the workers via nginx."""
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
) -> Tuple[List[NginxUpstream], Dict[int, List[NginxLocationConfig]]]:
    """Generate nginx upstreams and locations for proxying worker telemetry via nginx."""
    upstreams_worker_metrics, locations_worker_metrics = _generate_worker_metrics_nginx_config(
        worker_topology, worker_metrics_port=worker_metrics_port
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


def _generate_worker_metrics_nginx_config(
    worker_topology: List[Dict[str, str]], tls_available: bool, worker_metrics_port: int
) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
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
                port=worker_metrics_port,
                address_lookup_key=unit_name,
            )
        )

        locations.append(
            NginxLocationConfig(
                path=PROXY_WORKER_TELEMETRY_PATHS["metrics"].format(
                    unit=unit_name_sanitized
                ),
                backend=upstream_name,
                backend_url="/metrics",
                upstream_tls=tls_available,
                modifier="=",  # exact match, match only the metrics endpoint
                is_grpc=False,
            )
        )

    return upstreams, locations


def _generate_loki_endpoints_nginx_config(
    loki_endpoints_by_unit,
) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
    """Generate nginx upstreams and locations for loki endpoints routing."""
    upstreams: List[NginxUpstream] = []
    locations: List[NginxLocationConfig] = []

    for unit_name, address in loki_endpoints_by_unit.items():
        parsed_address = urlparse(address)

        unit_name_sanitized = unit_name.replace("/", "-")
        upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{unit_name_sanitized}"

        upstreams.append(
            NginxUpstream(
                name=upstream_name,
                port=parsed_address.port,  # type: ignore
                address_lookup_key=unit_name,
            )
        )

        locations.append(
            NginxLocationConfig(
                path=PROXY_WORKER_TELEMETRY_PATHS["logging"].format(
                    unit=unit_name_sanitized
                ),
                backend=upstream_name,
                backend_url=parsed_address.path,
                upstream_tls=parsed_address.scheme.endswith("s"),
                modifier="=",  # exact match, match only the push endpoint
                is_grpc=False,
            )
        )

    return upstreams, locations


def _generate_remote_write_endpoints_nginx_config(
    remote_write_endpoints_getter: RemoteWriteEndpointGetter,
) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
    """Generate nginx upstreams and locations for remote write endpoints routing."""
    upstreams: List[NginxUpstream] = []
    locations: List[NginxLocationConfig] = []

    if not remote_write_endpoints_getter:
        return upstreams, locations

    remote_write_endpoints: List[RemoteWriteEndpoint] = remote_write_endpoints_getter()

    for remote_write_endpoint in remote_write_endpoints:
        parsed_address = urlparse(remote_write_endpoint["url"])

        unit_name_sanitized = parsed_address.hostname.split(".")[0]  # type: ignore
        upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{unit_name_sanitized}"

        upstreams.append(
            NginxUpstream(
                name=upstream_name,
                port=parsed_address.port,  # type: ignore
                address_lookup_key=unit_name_sanitized,
            )
        )

        locations.append(
            NginxLocationConfig(
                path=PROXY_WORKER_TELEMETRY_PATHS["remote-write"].format(
                    unit=unit_name_sanitized
                ),
                backend=upstream_name,
                backend_url=parsed_address.path,
                upstream_tls=parsed_address.scheme.endswith("s"),
                modifier="=",  # exact match, match only the remote write path
                is_grpc=False,
            )
        )

    return upstreams, locations


def _generate_tracing_urls_nginx_config(
    charm_tracing_receivers_urls: Dict[str, str],
    workload_tracing_receivers_urls: Dict[str, str],
) -> Tuple[List[NginxUpstream], List[NginxLocationConfig]]:
    """Generate nginx upstreams and locations for routing charm and workload tracing."""
    upstreams: List[NginxUpstream] = []
    locations: List[NginxLocationConfig] = []
    created_upstreams: Set[str] = set()

    tracing_types = [
        (
            "charm",
            charm_tracing_receivers_urls,
            PROXY_WORKER_TELEMETRY_PATHS["charm-tracing"],
        ),
        (
            "workload",
            workload_tracing_receivers_urls,
            PROXY_WORKER_TELEMETRY_PATHS["workload-tracing"],
        ),
    ]

    for tracing_type, receivers_urls, path_template in tracing_types:
        for protocol, address in receivers_urls.items():
            parsed_address = urlparse(address)
            upstream_name = f"{PROXY_WORKER_TELEMETRY_UPSTREAM_PREFIX}-{tracing_type}-{protocol}"

            # Create upstream if we haven't already
            if upstream_name not in created_upstreams:
                upstreams.append(
                    NginxUpstream(
                        name=upstream_name,
                        port=parsed_address.port,  # type: ignore
                        address_lookup_key=upstream_name,
                    )
                )
                created_upstreams.add(upstream_name)

            # Create tracing location
            locations.append(
                NginxLocationConfig(
                    path=path_template.format(protocol=protocol),
                    backend=upstream_name,
                    backend_url=parsed_address.path,
                    rewrite=[
                        f"^{path_template.format(protocol=protocol)}(.*)",
                        "/$1",
                        "break",
                    ],  # strip the custom prefix before redirecting
                    upstream_tls=parsed_address.scheme.endswith("s"),
                    is_grpc=False,  # FIXME: GRPC must be allowed. Create an issue and link here.
                )
            )

    return upstreams, locations


def proxy_loki_endpoints_by_unit(
    logging_relations: Iterable[ops.Relation],
    tls_available: bool,
    hostname: str,
    proxy_worker_telemetry_port: int,
) -> Dict[str, str]:
    """Returns proxied loki endpoints published to the cluster provider for log forwarding via the proxy."""
    endpoints = {}
    for relation in logging_relations:
        for unit in relation.units:
            scheme = "https" if tls_available else "http"
            worker_tlm_path= PROXY_WORKER_TELEMETRY_PATHS['logging']
            sanitized_worker_tlm_path = worker_tlm_path.format(unit=unit.name.replace('/', '-'))
            endpoints[unit.name] = f"{scheme}://{hostname}:{proxy_worker_telemetry_port}{sanitized_worker_tlm_path}"
    return endpoints




def remote_write_endpoints(
        proxy_worker_telemetry_port:int,
        endpoints:List[RemoteWriteEndpoint],
        tls_available: bool,
        hostname: str,
) -> Union[List[RemoteWriteEndpoint], None]:
    """Returns proxy remote write endpoints published to the cluster provider for metrics forwarding via the proxy.

    Returns:
        A list of RemoteWriteEndpoint.
    """
    proxied_endpoints: List[RemoteWriteEndpoint] = []

    for remote_write_endpoint in endpoints:
        parsed_address = urlparse(remote_write_endpoint["url"])
        unit = parsed_address.hostname.split(".")[0]  # type: ignore
        scheme = "https" if tls_available else "http"
        proxy_url = f"{scheme}://{hostname}:{proxy_worker_telemetry_port}{PROXY_WORKER_TELEMETRY_PATHS["remote-write"].format(unit=unit)}"
        proxied_endpoints.append(RemoteWriteEndpoint(url=proxy_url))

    return proxied_endpoints


def tracing_receivers_urls(protocols:List[str],
                         tls_available:bool, hostname:str,
                         proxy_worker_telemetry_port:int,
                            endpoint:str,
                                 ) -> Dict[str, str]:
    """Returns proxy charm tracing receivers urls published to the cluster."""
    urls: Dict[str, str] = {}

    for protocol in protocols:
        scheme = "https" if tls_available else "http"
        proxy_url = f"{scheme}://{hostname}:{proxy_worker_telemetry_port}{PROXY_WORKER_TELEMETRY_PATHS[endpoint].format(protocol=protocol)}"
        urls.update({protocol: proxy_url})

    return urls