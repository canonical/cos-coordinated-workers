#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
import logging

import ops
from ops.charm import CharmBase, CollectStatusEvent
from ops.main import main

from coordinated_workers.coordinator import Coordinator
from coordinated_workers.nginx import (
    NginxConfig,
    NginxLocationConfig,
    NginxUpstream,
)
from coordinator_config import ROLES_CONFIG

logger = logging.getLogger(__name__)


class CoordinatorTester(CharmBase):
    def __init__(self, framework):
        super().__init__(framework)

        self._nginx_container = self.unit.get_container("nginx")

        # keep this above the coordinator definition
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)

        self.coordinator = Coordinator(
            charm=self,
            roles_config=ROLES_CONFIG,
            external_url=self._internal_app_hostname,
            worker_metrics_port=2345,
            endpoints={
                "certificates": "certificates",
                "cluster": "cluster",
                "grafana-dashboards": "grafana-dashboard",
                "logging": "logging",
                "metrics": "metrics-endpoint",
                "s3": "s3",
                "charm-tracing": "self-charm-tracing",
                "workload-tracing": "self-workload-tracing",
                "send-datasource": None,
                "receive-datasource": "receive-datasource",
                "catalogue": "catalogue",
                "service-mesh": "service-mesh",
            },
            nginx_config=NginxConfig(
                server_name=self._internal_app_hostname,
                upstream_configs=[
                    NginxUpstream("worker-role-a", 8080, "role-a"),
                    NginxUpstream("worker-role-b", 8080, "role-b"),
                ],
                server_ports_to_locations={
                    8080: [
                        NginxLocationConfig(path="/role-a", backend="worker-role-a"),
                        NginxLocationConfig(path="/role-b", backend="worker-role-b"),
                    ]
                },
            ),
            # Fake data cannot be an empty string as that is interpreted as no data
            workers_config=lambda _: "fake: data",
            # set the resource request for the nginx container
            resources_requests=None,
            container_name="nginx",
            remote_write_endpoints=None,  # type: ignore
            worker_ports=None,
            workload_tracing_protocols=["otlp_http"],
            catalogue_item=None,
        )

        self._port = 8080
        self.unit.set_ports(self._port)

    def _on_collect_status(self, e: CollectStatusEvent) -> None:
        """Collect status handler to set the unit status."""
        e.add_status(ops.ActiveStatus("I'm active, I think?"))

    @property
    def _internal_app_hostname(self) -> str:
        """Return the locally addressable, FQDN based service address."""
        return f"http://{self.app.name}.{self.model.name}.svc.cluster.local"


if __name__ == "__main__":
    main(CoordinatorTester)
