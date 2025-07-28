# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Data models."""

from dataclasses import dataclass


@dataclass
class TLSConfig:
    """TLS configuration received by the coordinator over the `certificates` relation.

    This is an internal object that we use as facade so that the individual Coordinator charms don't have to know the API of the charm libs that implements the relation interface.
    """

    server_cert: str
    ca_cert: str
    private_key: str
