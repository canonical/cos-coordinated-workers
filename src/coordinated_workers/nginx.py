# Copyright 2023 Canonical
# See LICENSE file for licensing details.

r"""## Overview.

This module provides a set of utilities for generating Nginx configurations and managing Nginx workloads.
Used by the coordinator to load-balance and group the workers.

- `NginxLocationModifier`: An enum representing valid Nginx `location` block modifiers (e.g., `=`, `~`, `^~`).
Should be used to populate an `NginxLocationConfig` object.

- `NginxLocationConfig`: A class that defines a single Nginx `location` block, including path matching, modifier, custom headers, .etc.

- `NginxUpstream`: A class that describes an Nginx `upstream` block — specifying an upstream `name`, `port`, and `worker_role` used to resolve backend endpoints.

- `NginxConfig`: A class that builds a full Nginx configuration to be used by the coordinator to load-balance traffic across the workers.

- `Nginx`: A helper class for managing the Nginx container workload (e.g., pebble service lifecycle, config reloads).

- `NginxPrometheusExporter`: A helper class for managing the Nginx Prometheus exporter container workload (e.g., pebble service lifecycle, config reloads).

- `is_ipv6_enabled()`: A utility function to check whether IPv6 is enabled on the container's network interfaces.

## Usage
### Nginx Config Generation

To generate an Nginx configuration for a charm, instantiate the `NginxConfig` class with the required inputs:

1. `server_name`: The name of the server (e.g. charm fqdn), which is used to identify the server in Nginx configurations.
2. `upstream_configs`: List of `NginxUpstream` used to generate Nginx `upstream` blocks.
3. `server_ports_to_locations`: Mapping from server ports to a list of `NginxLocationConfig`.

#### Use `NginxConfig` in the context of the shared `Coordinator` object

A coordinator charm may instantiate the `NginxConfig` in its constructor as follows:
    from coordinated_workers.nginx import NginxConfig, NginxUpstream, NginxLocationConfig
    ...

    class CoordinatorCharm(CharmBase):
        ...
        def __init__(self, *args):
            super().__init__(*args)
            ...
            self.coordinator = Coordinator(
                ...
                nginx_config=NginxConfig(
                    server_name=self.hostname,
                    upstream_configs=self._nginx_upstreams(),
                    server_ports_to_locations=self._server_ports_to_locations(),
                )
            )
        ...
        @property
        def hostname(self) -> str:
            return socket.getfqdn()

        @property
        def _nginx_locations(self) -> List[NginxLocationConfig]:
            return [
                NginxLocationConfig(path="/loki/api/v1/rules", backend="backend",modifier="="),
                NginxLocationConfig(path="/prometheus", backend="backend",modifier="="),
                NginxLocationConfig(path="/api/v1/rules", backend="backend", backend_url="/loki/api/v1/rules",modifier="~"),
            ]

        def _nginx_upstreams(self) -> List[NginxUpstream]:
            # WORKER_ROLES is a list of worker roles that we want to route traffic to
            for upstream in WORKER_ROLES:
                # WORKER_PORT is the port the worker services are running on
                upstreams.append(NginxUpstream(upstream, WORKER_PORT, upstream))
                return upstreams

        def _server_ports_to_locations(self) -> Dict[int, List[NginxLocationConfig]]:
            # NGINX_PORT is the port an nginx server is running on
            # Note that: you can define multiple server blocks, each running on a different port
            return {NGINX_PORT: self._nginx_locations}

Passing the populated `NginxConfig` to the shared `Coordinator` will:
1. generate the full Nginx configuration
2. write the config to a file inside the `nginx` container
3. start the `nginx` pebble service to run with that config file

#### Use `NginxConfig` as a standalone

Any charm can instantiate `NginxConfig` to generate its own Nginx configuration as follows:
    from coordinated_workers.nginx import NginxConfig, NginxUpstream, NginxLocationConfig
    ...

    class AnyCharm(CharmBase):
        ...
        def __init__(self, *args):
            super().__init__(*args)
            ...
            self._container = self.unit.get_container("nginx")
            self._nginx = NginxConfig(
                server_name=self.hostname,
                upstream_configs=self._nginx_upstreams(),
                server_ports_to_locations=self._server_ports_to_locations(),
            )
            ...
            self._reconcile()


        ...
        @property
        def hostname(self) -> str:
            return socket.getfqdn()

        @property
        def _nginx_locations(self) -> List[NginxLocationConfig]:
            return [
                NginxLocationConfig(path="/api/v1", backend="upstream1",modifier="~"),
                NginxLocationConfig(path="/status", backend="upstream2",modifier="="),
            ]

        @property
        def _upstream_addresses(self) -> Dict[str, Set[str]]:
            # a mapping from an upstream "role" to the set of addresses that belong to this upstream
            return {
                "upstream1": {"address1", "address2"},
                "upstream2": {"address3", "address4"},
            }

        @property
        def _tls_available(self) -> bool:
            # return if the Nginx config should have TLS enabled
            pass

        def _reconcile(self):
            if self._container.can_connect():
                new_config: str = self._nginx.get_config(self._upstream_addresses, self._tls_available)
                should_restart: bool = self._has_config_changed(new_config)
                self._container.push(self.config_path, new_config, make_dirs=True)
                self._container.add_layer("nginx", self.layer, combine=True)
                self._container.autostart()

                if should_restart:
                    logger.info("new nginx config: restarting the service")
                    self.reload()

        def _nginx_upstreams(self) -> List[NginxUpstream]:
            # UPSTREAMS is a list of backend services that we want to route traffic to
            for upstream in UPSTREAMS:
                # UPSTREAMS_PORT is the port the backend services are running on
                upstreams.append(NginxUpstream(upstream, UPSTREAMS_PORT, upstream))
                return upstreams

        def _server_ports_to_locations(self) -> Dict[int, List[NginxLocationConfig]]:
            # NGINX_PORT is the port an nginx server is running on
            # Note that: you can define multiple server blocks, each running on a different port
            return {NGINX_PORT: self._nginx_locations}

"""

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Set, TypedDict, cast

import crossplane  # type: ignore
from opentelemetry import trace
from ops import CharmBase, pebble

from coordinated_workers.models import TLSConfig

logger = logging.getLogger(__name__)

# TODO: should we add these to _NginxMapping and make them configurable / accessible?
NGINX_DIR = "/etc/nginx"
NGINX_CONFIG = f"{NGINX_DIR}/nginx.conf"
KEY_PATH = f"{NGINX_DIR}/certs/server.key"
CERT_PATH = f"{NGINX_DIR}/certs/server.cert"
CA_CERT_PATH = "/usr/local/share/ca-certificates/ca.cert"

_NginxMapping = TypedDict(
    "_NginxMapping", {"nginx_port": int, "nginx_exporter_port": int}, total=True
)
NginxMappingOverrides = TypedDict(
    "NginxMappingOverrides", {"nginx_port": int, "nginx_exporter_port": int}, total=False
)
DEFAULT_OPTIONS: _NginxMapping = {
    "nginx_port": 8080,
    "nginx_exporter_port": 9113,
}
RESOLV_CONF_PATH = "/etc/resolv.conf"

_tracer = trace.get_tracer("nginx.tracer")


# Define valid Nginx `location` block modifiers.
# cfr. https://www.digitalocean.com/community/tutorials/nginx-location-directive#nginx-location-directive-syntax
NginxLocationModifier = Literal[
    "",  # prefix match
    "=",  # exact match
    "~",  # case-sensitive regex match
    "~*",  # case-insensitive regex match
    "^~",  # prefix match that disables further regex matching
]


@dataclass
class NginxLocationConfig:
    """Represents a `location` block in an Nginx configuration.

    For example, NginxLocationConfig('/', 'foo', backend_url="/api/v1" headers={'a': 'b'}, modifier=EXACT, is_grpc=True, use_tls=True)
    would result in:
        location = / {
            set $backend grpcs://foo/api/v1;
            grpc_pass $backend;
            proxy_connect_timeout 5s;
            proxy_set_header a b;
        }
    """

    path: str
    """The location path (e.g., '/', '/api') to match incoming requests."""
    backend: str
    """The name of the upstream service to route requests to (e.g., defined in an `upstream` block)."""
    backend_url: str = ""
    """An optional URL path to append when forwarding to the upstream (e.g., '/v1')."""
    headers: Dict[str, str] = field(default_factory=lambda: cast(Dict[str, str], {}))
    """Custom headers to include in the proxied request."""
    modifier: NginxLocationModifier = ""
    """The Nginx location modifier."""
    is_grpc: bool = False
    """Whether to use gRPC proxying (i.e. `grpc_pass` instead of `proxy_pass`)."""
    upstream_tls: Optional[bool] = None
    """Whether to connect to the upstream over TLS (e.g., https:// or grpcs://)
    If None, it will inherit the TLS setting from the server block that the location is part of.
    """


@dataclass
class NginxUpstream:
    """Represents metadata needed to construct an Nginx `upstream` block."""

    name: str
    """Name of the upstream block."""
    port: int
    """Port number that all backend servers in this upstream listen on.

    Our coordinators assume that all servers under an upstream share the same port.
    """
    worker_role: str
    """The worker role that corresponds to this upstream.

    This role will be used to look up workers (backend server) addresses for this upstream.
    """
    ignore_worker_role: bool = False
    """If True, overrides `worker_role` and routes to all available backend servers.

    Use this when the upstream should be generic and include any available backend.
    """


class NginxConfig:
    """Responsible for building the Nginx configuration used by the coordinators."""

    _worker_processes = "5"
    _pid = "/tmp/nginx.pid"
    _worker_rlimit_nofile = "8192"
    _worker_connections = "4096"
    _proxy_read_timeout = "300"
    _supported_tls_versions = ["TLSv1", "TLSv1.1", "TLSv1.2", "TLSv1.3"]
    _ssl_ciphers = ["HIGH:!aNULL:!MD5"]
    _proxy_connect_timeout = "5s"

    def __init__(
        self,
        server_name: str,
        upstream_configs: List[NginxUpstream],
        server_ports_to_locations: Dict[int, List[NginxLocationConfig]],
        enable_health_check: bool = False,
        enable_status_page: bool = False,
    ):
        """Constructor for an Nginx config generator object.

        Args:
            server_name: The name of the server (e.g. coordinator fqdn), which is used to identify the server in Nginx configurations.
            upstream_configs: List of Nginx upstream metadata configurations used to generate Nginx `upstream` blocks.
            server_ports_to_locations: Mapping from server ports to a list of Nginx location configurations.
            enable_health_check: If True, adds a `/` location that returns a basic 200 OK response.
            enable_status_page: If True, adds a `/status` location that enables `stub_status` for basic Nginx metrics.

        Example:
            .. code-block:: python
            NginxConfig(
            server_name = "tempo-coordinator-0.tempo-coordinator-endpoints.model.svc.cluster.local",
            upstreams = [
                NginxUpstream(name="zipkin", port=9411, worker_role="distributor"),
            ],
            server_ports_to_locations = {
                9411: [
                    NginxLocationConfig(
                        path="/",
                        backend="zipkin"
                    )
                ]
            })
        """
        self._server_name = server_name
        self._upstream_configs = upstream_configs
        self._server_ports_to_locations = server_ports_to_locations
        self._enable_health_check = enable_health_check
        self._enable_status_page = enable_status_page
        self._dns_IP_address = self._get_dns_ip_address()
        self._ipv6_enabled = is_ipv6_enabled()

    def get_config(self, upstreams_to_addresses: Dict[str, Set[str]], listen_tls: bool) -> str:
        """Render the Nginx configuration as a string.

        Args:
            upstreams_to_addresses: A dictionary mapping each upstream name to a set of addresses associated with that upstream.
            listen_tls: Whether Nginx should listen for incoming traffic over TLS.
        """
        full_config = self._prepare_config(upstreams_to_addresses, listen_tls)
        return crossplane.build(full_config)  # type: ignore

    def _prepare_config(
        self, upstreams_to_addresses: Dict[str, Set[str]], listen_tls: bool
    ) -> List[Dict[str, Any]]:
        upstreams = self._upstreams(upstreams_to_addresses)
        # extract the upstream name
        backends = [upstream["args"][0] for upstream in upstreams]
        # build the complete configuration
        full_config = [
            {"directive": "worker_processes", "args": [self._worker_processes]},
            {"directive": "error_log", "args": ["/dev/stderr", "error"]},
            {"directive": "pid", "args": [self._pid]},
            {"directive": "worker_rlimit_nofile", "args": [self._worker_rlimit_nofile]},
            {
                "directive": "events",
                "args": [],
                "block": [{"directive": "worker_connections", "args": [self._worker_connections]}],
            },
            {
                "directive": "http",
                "args": [],
                "block": [
                    # upstreams (load balancing)
                    *upstreams,
                    # temp paths
                    {
                        "directive": "client_body_temp_path",
                        "args": ["/tmp/client_temp"],
                    },
                    {"directive": "proxy_temp_path", "args": ["/tmp/proxy_temp_path"]},
                    {"directive": "fastcgi_temp_path", "args": ["/tmp/fastcgi_temp"]},
                    {"directive": "uwsgi_temp_path", "args": ["/tmp/uwsgi_temp"]},
                    {"directive": "scgi_temp_path", "args": ["/tmp/scgi_temp"]},
                    # logging
                    {"directive": "default_type", "args": ["application/octet-stream"]},
                    {
                        "directive": "log_format",
                        "args": [
                            "main",
                            '$remote_addr - $remote_user [$time_local]  $status "$request" $body_bytes_sent "$http_referer" "$http_user_agent" "$http_x_forwarded_for"',
                        ],
                    },
                    *self._log_verbose(verbose=False),
                    {"directive": "sendfile", "args": ["on"]},
                    {"directive": "tcp_nopush", "args": ["on"]},
                    *self._resolver(),
                    # TODO: add custom http block for the user to config?
                    {
                        "directive": "map",
                        "args": ["$http_x_scope_orgid", "$ensured_x_scope_orgid"],
                        "block": [
                            {"directive": "default", "args": ["$http_x_scope_orgid"]},
                            {"directive": "", "args": ["anonymous"]},
                        ],
                    },
                    {"directive": "proxy_read_timeout", "args": [self._proxy_read_timeout]},
                    # server block
                    *self._build_servers_config(backends, listen_tls),
                ],
            },
        ]
        return full_config

    def _log_verbose(self, verbose: bool = True) -> List[Dict[str, Any]]:
        if verbose:
            return [{"directive": "access_log", "args": ["/dev/stderr", "main"]}]
        return [
            {
                "directive": "map",
                "args": ["$status", "$loggable"],
                "block": [
                    {"directive": "~^[23]", "args": ["0"]},
                    {"directive": "default", "args": ["1"]},
                ],
            },
            {"directive": "access_log", "args": ["/dev/stderr"]},
        ]

    def _resolver(
        self,
        custom_resolver: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        # pass a custom resolver, such as kube-dns.kube-system.svc.cluster.local.
        if custom_resolver:
            return [{"directive": "resolver", "args": [custom_resolver]}]

        # by default, fetch the DNS resolver address from /etc/resolv.conf
        return [
            {
                "directive": "resolver",
                "args": [self._dns_IP_address],
            }
        ]

    @staticmethod
    def _get_dns_ip_address() -> str:
        """Obtain DNS ip address from /etc/resolv.conf."""
        resolv = Path(RESOLV_CONF_PATH).read_text()
        for line in resolv.splitlines():
            if line.startswith("nameserver"):
                # assume there's only one
                return line.split()[1].strip()
        raise RuntimeError("cannot find nameserver in /etc/resolv.conf")

    def _upstreams(self, upstreams_to_addresses: Dict[str, Set[str]]) -> List[Any]:
        nginx_upstreams: List[Any] = []

        for upstream_config in self._upstream_configs:
            if upstream_config.ignore_worker_role:
                # include all available addresses
                addresses: Optional[Set[str]] = set()
                for address_set in upstreams_to_addresses.values():
                    addresses.update(address_set)
            else:
                addresses = upstreams_to_addresses.get(upstream_config.worker_role)

            # don't add an upstream block if there are no addresses
            if addresses:
                upstream_config_name = upstream_config.name
                nginx_upstreams.append(
                    {
                        "directive": "upstream",
                        "args": [upstream_config_name],
                        "block": [
                            # enable dynamic DNS resolution for upstream servers.
                            # since K8s pods IPs are dynamic, we need this config to allow
                            # nginx to re-resolve the DNS name without requiring a config reload.
                            # cfr. https://www.f5.com/company/blog/nginx/dns-service-discovery-nginx-plus#:~:text=second%20method
                            {
                                "directive": "zone",
                                "args": [f"{upstream_config_name}_zone", "64k"],
                            },
                            *[
                                {
                                    "directive": "server",
                                    "args": [f"{addr}:{upstream_config.port}", "resolve"],
                                }
                                for addr in addresses
                            ],
                        ],
                    }
                )

        return nginx_upstreams

    def _build_servers_config(
        self, backends: List[str], listen_tls: bool = False
    ) -> List[Dict[str, Any]]:
        servers: List[Dict[str, Any]] = []
        for port, locations in self._server_ports_to_locations.items():
            server_config = self._build_server_config(
                port,
                locations,
                backends,
                listen_tls,
            )
            if server_config:
                servers.append(server_config)
        return servers

    def _build_server_config(
        self,
        port: int,
        locations: List[NginxLocationConfig],
        backends: List[str],
        listen_tls: bool = False,
    ) -> Dict[str, Any]:
        auth_enabled = False
        is_grpc = any(loc.is_grpc for loc in locations)
        nginx_locations = self._locations(locations, is_grpc, backends, listen_tls)
        server_config = {}
        if len(nginx_locations) > 0:
            server_config = {
                "directive": "server",
                "args": [],
                "block": [
                    *self._listen(port, ssl=listen_tls, http2=is_grpc),
                    *self._basic_auth(auth_enabled),
                    {
                        "directive": "proxy_set_header",
                        "args": ["X-Scope-OrgID", "$ensured_x_scope_orgid"],
                    },
                    {"directive": "server_name", "args": [self._server_name]},
                    *(
                        [
                            {"directive": "ssl_certificate", "args": [CERT_PATH]},
                            {"directive": "ssl_certificate_key", "args": [KEY_PATH]},
                            {
                                "directive": "ssl_protocols",
                                "args": self._supported_tls_versions,
                            },
                            {
                                "directive": "ssl_ciphers",
                                "args": self._ssl_ciphers,
                            },
                        ]
                        if listen_tls
                        else []
                    ),
                    *nginx_locations,
                ],
            }

        return server_config

    def _locations(
        self,
        locations: List[NginxLocationConfig],
        grpc: bool,
        backends: List[str],
        listen_tls: bool,
    ) -> List[Dict[str, Any]]:
        nginx_locations: List[Dict[str, Any]] = []

        if self._enable_health_check:
            nginx_locations.append(
                {
                    "directive": "location",
                    "args": ["=", "/"],
                    "block": [
                        {
                            "directive": "return",
                            "args": ["200", "'OK'"],
                        },
                        {
                            "directive": "auth_basic",
                            "args": ["off"],
                        },
                    ],
                },
            )
        if self._enable_status_page:
            nginx_locations.append(
                {
                    "directive": "location",
                    "args": ["=", "/status"],
                    "block": [
                        {
                            "directive": "stub_status",
                            "args": [],
                        },
                    ],
                },
            )

        for location in locations:
            # don't add a location block if the upstream backend doesn't exist in the config
            if location.backend in backends:
                # if upstream_tls is explicitly set for this location, use that; otherwise, use the server's listen_tls setting.
                tls = location.upstream_tls if location.upstream_tls is not None else listen_tls
                s = "s" if tls else ""
                protocol = f"grpc{s}" if grpc else f"http{s}"
                nginx_locations.append(
                    {
                        "directive": "location",
                        "args": (
                            [location.path]
                            if location.modifier == ""
                            else [location.modifier, location.path]
                        ),
                        "block": [
                            {
                                "directive": "set",
                                "args": [
                                    "$backend",
                                    f"{protocol}://{location.backend}{location.backend_url}",
                                ],
                            },
                            {
                                "directive": "grpc_pass" if grpc else "proxy_pass",
                                "args": ["$backend"],
                            },
                            # if a server is down, no need to wait for a long time to pass on the request to the next available server
                            {
                                "directive": "proxy_connect_timeout",
                                "args": [self._proxy_connect_timeout],
                            },
                            # add headers if any
                            *(
                                [
                                    {"directive": "proxy_set_header", "args": [key, val]}
                                    for key, val in location.headers.items()
                                ]
                                if location.headers
                                else []
                            ),
                        ],
                    }
                )

        return nginx_locations

    def _basic_auth(self, enabled: bool) -> List[Optional[Dict[str, Any]]]:
        if enabled:
            return [
                {"directive": "auth_basic", "args": ['"workload"']},
                {
                    "directive": "auth_basic_user_file",
                    "args": ["/etc/nginx/secrets/.htpasswd"],
                },
            ]
        return []

    def _listen(self, port: int, ssl: bool, http2: bool) -> List[Dict[str, Any]]:
        directives: List[Dict[str, Any]] = []
        directives.append(
            {"directive": "listen", "args": self._listen_args(port, False, ssl, http2)}
        )
        if self._ipv6_enabled:
            directives.append(
                {
                    "directive": "listen",
                    "args": self._listen_args(port, True, ssl, http2),
                }
            )
        return directives

    def _listen_args(self, port: int, ipv6: bool, ssl: bool, http2: bool) -> List[str]:
        args: List[str] = []
        if ipv6:
            args.append(f"[::]:{port}")
        else:
            args.append(f"{port}")
        if ssl:
            args.append("ssl")
        if http2:
            args.append("http2")
        return args


class Nginx:
    """Helper class to manage the nginx workload."""

    config_path = NGINX_CONFIG
    _name = "nginx"
    options: _NginxMapping = DEFAULT_OPTIONS

    def __init__(
        self,
        charm: CharmBase,
        config_getter: Callable[[bool], str],
        tls_config_getter: Callable[[], Optional[TLSConfig]],
        options: Optional[NginxMappingOverrides] = None,
    ):
        self._charm = charm
        self._config_getter = config_getter
        self._tls_config_getter = tls_config_getter
        self._container = self._charm.unit.get_container("nginx")
        self.options.update(options or {})

    @property
    def are_certificates_on_disk(self) -> bool:
        """Return True if the certificates files are on disk."""
        return (
            self._container.can_connect()
            and self._container.exists(CERT_PATH)
            and self._container.exists(KEY_PATH)
            and self._container.exists(CA_CERT_PATH)
        )

    def _configure_tls(self, private_key: str, server_cert: str, ca_cert: str) -> None:
        """Save the certificates file to disk and run update-ca-certificates."""
        with _tracer.start_as_current_span("write ca cert"):
            # push CA cert to charm container
            Path(CA_CERT_PATH).parent.mkdir(parents=True, exist_ok=True)
            Path(CA_CERT_PATH).write_text(ca_cert)

        if self._container.can_connect():
            # Read the current content of the files (if they exist)
            current_server_cert = (
                self._container.pull(CERT_PATH).read() if self._container.exists(CERT_PATH) else ""
            )
            current_private_key = (
                self._container.pull(KEY_PATH).read() if self._container.exists(KEY_PATH) else ""
            )
            current_ca_cert = (
                self._container.pull(CA_CERT_PATH).read()
                if self._container.exists(CA_CERT_PATH)
                else ""
            )

            if (
                current_server_cert == server_cert
                and current_private_key == private_key
                and current_ca_cert == ca_cert
            ):
                # No update needed
                return
            self._container.push(KEY_PATH, private_key, make_dirs=True)
            self._container.push(CERT_PATH, server_cert, make_dirs=True)
            self._container.push(CA_CERT_PATH, ca_cert, make_dirs=True)

            # TODO: uncomment when/if the nginx image contains the ca-certificates package
            # self._container.exec(["update-ca-certificates", "--fresh"])

    def _delete_certificates(self) -> None:
        """Delete the certificate files from disk and run update-ca-certificates."""
        with _tracer.start_as_current_span("delete ca cert"):
            if Path(CA_CERT_PATH).exists():
                Path(CA_CERT_PATH).unlink(missing_ok=True)

        if self._container.can_connect():
            for path in (CERT_PATH, KEY_PATH, CA_CERT_PATH):
                if self._container.exists(path):
                    self._container.remove_path(path, recursive=True)

            # TODO: uncomment when/if the nginx image contains the ca-certificates package
            # self._container.exec(["update-ca-certificates", "--fresh"])

    def _has_config_changed(self, new_config: str) -> bool:
        """Return True if the passed config differs from the one on disk."""
        if not self._container.can_connect():
            logger.debug("Could not connect to Nginx container")
            return False

        try:
            current_config = self._container.pull(self.config_path).read()
        except (pebble.ProtocolError, pebble.PathError) as e:
            logger.warning(
                "Could not check the current nginx configuration due to "
                "a failure in retrieving the file: %s",
                e,
            )
            return False

        return current_config != new_config

    def reconcile(self):
        """Configure pebble layer and restart if necessary."""
        if self._container.can_connect():
            self._reconcile_tls_config()
            self._reconcile_nginx_config()

    def _reconcile_tls_config(self):
        if tls_config := self._tls_config_getter():
            self._configure_tls(
                server_cert=tls_config.server_cert,
                ca_cert=tls_config.ca_cert,
                private_key=tls_config.private_key,
            )
        else:
            self._delete_certificates()

    def _reconcile_nginx_config(self):
        new_config = self._config_getter(self.are_certificates_on_disk)
        should_restart = self._has_config_changed(new_config)
        self._container.push(self.config_path, new_config, make_dirs=True)  # type: ignore
        self._container.add_layer("nginx", self.layer, combine=True)
        self._container.autostart()

        if should_restart:
            logger.info("new nginx config: restarting the service")
            # Reload the nginx config without restarting the service
            self._container.exec(["nginx", "-s", "reload"])

    @property
    def layer(self) -> pebble.Layer:
        """Return the Pebble layer for Nginx."""
        return pebble.Layer(
            {
                "summary": "nginx layer",
                "description": "pebble config layer for Nginx",
                "services": {
                    "nginx": {
                        "override": "replace",
                        "summary": "nginx",
                        "command": "nginx -g 'daemon off;'",
                        "startup": "enabled",
                    }
                },
            }
        )


class NginxPrometheusExporter:
    """Helper class to manage the nginx prometheus exporter workload."""

    options: _NginxMapping = DEFAULT_OPTIONS

    def __init__(self, charm: CharmBase, options: Optional[NginxMappingOverrides] = None) -> None:
        self._charm = charm
        self._container = self._charm.unit.get_container("nginx-prometheus-exporter")
        self.options.update(options or {})

    def reconcile(self):
        """Configure pebble layer and restart if necessary."""
        if self._container.can_connect():
            self._container.add_layer("nginx-prometheus-exporter", self.layer, combine=True)
            self._container.autostart()

    @property
    def are_certificates_on_disk(self) -> bool:
        """Return True if the certificates files are on disk."""
        return (
            self._container.can_connect()
            and self._container.exists(CERT_PATH)
            and self._container.exists(KEY_PATH)
            and self._container.exists(CA_CERT_PATH)
        )

    @property
    def layer(self) -> pebble.Layer:
        """Return the Pebble layer for Nginx Prometheus exporter."""
        scheme = "https" if self.are_certificates_on_disk else "http"  # type: ignore
        return pebble.Layer(
            {
                "summary": "nginx prometheus exporter layer",
                "description": "pebble config layer for Nginx Prometheus exporter",
                "services": {
                    "nginx-prometheus-exporter": {
                        "override": "replace",
                        "summary": "nginx prometheus exporter",
                        "command": f"nginx-prometheus-exporter --no-nginx.ssl-verify --web.listen-address=:{self.options['nginx_exporter_port']}  --nginx.scrape-uri={scheme}://127.0.0.1:{self.options['nginx_port']}/status",
                        "startup": "enabled",
                    }
                },
            }
        )


def is_ipv6_enabled() -> bool:
    """Check if IPv6 is enabled on the container's network interfaces."""
    try:
        output = subprocess.run(
            ["ip", "-6", "address", "show"], check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError:
        # if running the command failed for any reason, assume ipv6 is not enabled.
        return False
    return bool(output.stdout)
