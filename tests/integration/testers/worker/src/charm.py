#!/usr/bin/env python3
# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Worker Charm"""
import logging

from ops.charm import CharmBase
from ops.pebble import Layer

from coordinated_workers.worker import Worker

container_name = "echoserver"

class WorkerCharm(CharmBase):
    def __init__(self, *args):
        super().__init__(*args)
        logging.error("WorkerCharm __init__")
        self.worker = Worker(
            charm=self,
            # name of the container the worker is operating on AND of the executable
            name=container_name,
            pebble_layer=self.pebble_layer,
            endpoints={"cluster": "cluster"},
            readiness_check_endpoint=None,
            resources_requests=None,
            # container we want to resource-patch
            container_name=container_name,
        )

    def pebble_layer(self, worker: Worker) -> Layer:
        layer = Layer(
            # Start a listener for each defined port
            {
                "summary": "echo server layer",
                "description": "pebble config layer for echo server",
                "services": {
                    container_name: {
                        "override": "replace",
                        "command": "/bin/echo-server",
                        "startup": "enabled",
                        # TODO: Set this from Cluster?
                        "environment": {"PORT": 8080},
                    },
                },
            }
        )
        return layer


if __name__ == "__main__":  # pragma: nocover
    import ops
    ops.main(WorkerCharm)
