#!/usr/bin/env python3
# Copyright 2025 Canonical
# See LICENSE file for licensing details.

"""Shared utilities for the coordinator -> worker "cluster" interface.

As this relation is cluster-internal and not intended for third-party charms to interact with
`-coordinator-k8s`, its only user will be the -worker-k8s charm. As such,
it does not live in a charm lib as most other relation endpoint wrappers do.
"""

import collections
import json
import logging
from typing import (
    Any,
    Callable,
    Counter,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)
from urllib.parse import urlparse

import cosl
import cosl.interfaces.utils
import ops
import pydantic
import yaml
from ops import EventSource, Object, ObjectEvents, RelationCreatedEvent, SecretNotFoundError
from typing_extensions import TypedDict

log = logging.getLogger("_cluster")

DEFAULT_ENDPOINT_NAME = "-cluster"


# =============
# | Interface |
# =============


class RemoteWriteEndpoint(TypedDict):
    """Type of the remote write endpoints to be passed to the worker through cluster relation data."""

    url: str


class ConfigReceivedEvent(ops.EventBase):
    """Event emitted when the "-cluster" provider has shared a new  config."""

    config: Dict[str, Any]
    """The  config."""

    def __init__(self, handle: ops.framework.Handle, config: Dict[str, Any]):
        super().__init__(handle)
        self.config = config

    def snapshot(self) -> Dict[str, Any]:
        """Used by the framework to serialize the event to disk.

        Not meant to be called by charm code.
        """
        return {"config": json.dumps(self.config)}

    def restore(self, snapshot: Dict[str, Any]):
        """Used by the framework to deserialize the event from disk.

        Not meant to be called by charm code.
        """
        self.relation = json.loads(snapshot["config"])  # noqa


class ClusterError(Exception):
    """Base class for exceptions raised by this module."""


class DatabagAccessPermissionError(ClusterError):
    """Raised when a follower attempts to write leader settings."""


class _Topology(pydantic.BaseModel):
    """Juju topology information."""

    application: str
    unit: str
    charm_name: str


class ClusterRequirerAppData(cosl.interfaces.utils.DatabagModel):
    """App data that the worker sends to the coordinator."""

    role: str


class ClusterRequirerUnitData(cosl.interfaces.utils.DatabagModel):
    """Unit data the worker sends to the coordinator."""

    juju_topology: _Topology
    address: str


class ClusterProviderAppData(cosl.interfaces.utils.DatabagModel):
    """App data that the coordinator sends to the worker."""

    ### worker node configuration
    worker_config: str
    """The whole worker workload configuration, whatever it is. E.g. yaml-encoded things."""

    ### self-monitoring stuff
    loki_endpoints: Optional[Dict[str, str]] = None
    """Endpoints to which the workload (and the worker charm) can push logs to."""
    charm_tracing_receivers: Optional[Dict[str, str]] = None
    """Endpoints to which the the worker charm can push charm traces to."""
    workload_tracing_receivers: Optional[Dict[str, str]] = None
    """Endpoints to which the the worker can push workload traces to."""
    remote_write_endpoints: Optional[List[RemoteWriteEndpoint]] = None
    """Endpoints to which the workload (and the worker charm) can push metrics to."""
    worker_ports: Optional[List[int]] = None
    """Ports that the worker should open. If not provided, the worker will open all the legacy ones."""

    ### TLS stuff
    ca_cert: Optional[str] = None
    server_cert: Optional[str] = None
    privkey_secret_id: Optional[str] = None
    s3_tls_ca_chain: Optional[str] = None


class TLSData(NamedTuple):
    """Section of the cluster data that concerns TLS information."""

    ca_cert: Optional[str]
    server_cert: Optional[str]
    privkey_secret_id: Optional[str]
    s3_tls_ca_chain: Optional[str]


class ClusterChangedEvent(ops.EventBase):
    """Event emitted when any "-cluster" relation event fires."""


class ClusterRemovedEvent(ops.EventBase):
    """Event emitted when the relation with the "-cluster" provider has been severed.

    Or when the relation data has been wiped.
    """


class ClusterProviderEvents(ObjectEvents):
    """Events emitted by the ClusterProvider "-cluster" endpoint wrapper."""

    changed = EventSource(ClusterChangedEvent)


class ClusterRequirerEvents(ObjectEvents):
    """Events emitted by the ClusterRequirer "-cluster" endpoint wrapper."""

    config_received = EventSource(ConfigReceivedEvent)
    created = EventSource(RelationCreatedEvent)
    removed = EventSource(ClusterRemovedEvent)


class ClusterProvider(Object):
    """``-cluster`` provider endpoint wrapper."""

    on = ClusterProviderEvents()  # type: ignore

    def __init__(
        self,
        charm: ops.CharmBase,
        roles: FrozenSet[str],
        meta_roles: Optional[Mapping[str, Iterable[str]]] = None,
        key: Optional[str] = None,
        endpoint: str = DEFAULT_ENDPOINT_NAME,
        worker_ports: Optional[Callable[[str], Sequence[int]]] = None,
    ):
        super().__init__(charm, key or endpoint)
        self._charm = charm
        self._roles = roles
        self._meta_roles = meta_roles or {}
        self._worker_ports = worker_ports
        self.juju_topology = cosl.JujuTopology.from_charm(self._charm)

        # filter out common unhappy relation states
        self._relations: List[ops.Relation] = [
            rel for rel in self.model.relations[endpoint] if (rel.app and rel.data)
        ]

        # we coalesce all -cluster-relation-* events into a single cluster-changed API.
        # the coordinator uses a common exit hook reconciler, that's why.
        self.framework.observe(self._charm.on[endpoint].relation_created, self._on_cluster_changed)
        self.framework.observe(self._charm.on[endpoint].relation_joined, self._on_cluster_changed)
        self.framework.observe(self._charm.on[endpoint].relation_changed, self._on_cluster_changed)
        self.framework.observe(
            self._charm.on[endpoint].relation_departed, self._on_cluster_changed
        )
        self.framework.observe(self._charm.on[endpoint].relation_broken, self._on_cluster_changed)

    def _on_cluster_changed(self, _: ops.EventBase) -> None:
        self.on.changed.emit()

    def grant_privkey(self, label: str) -> Optional[str]:
        """Grant the secret containing the privkey, if it exists, to all relations, and return the secret ID."""
        try:
            secret = self.model.get_secret(label=label)
        except SecretNotFoundError:
            # it might be the case that we're trying to access the secret on relation created/joined
            # while it actually gets created on relation changed
            log.debug("secret with label %s not found", label)
            return None

        for relation in self._relations:
            secret.grant(relation)
        # can't return secret.id because secret was obtained by label, and so
        # we don't have an ID unless we fetch it
        return secret.get_info().id

    def publish_data(
        self,
        worker_config: str,
        ca_cert: Optional[str] = None,
        server_cert: Optional[str] = None,
        s3_tls_ca_chain: Optional[str] = None,
        privkey_secret_id: Optional[str] = None,
        loki_endpoints: Optional[Dict[str, str]] = None,
        charm_tracing_receivers: Optional[Dict[str, str]] = None,
        workload_tracing_receivers: Optional[Dict[str, str]] = None,
        remote_write_endpoints: Optional[List[RemoteWriteEndpoint]] = None,
    ) -> None:
        """Publish the config to all related worker clusters."""
        for relation in self._relations:
            if relation and self._remote_data_ready(relation):
                # obtain the worker ports for this relation, given the role advertised by the remote
                if worker_ports := self._worker_ports:
                    _worker_ports = list(
                        worker_ports(ClusterRequirerAppData.load(relation.data[relation.app]).role)
                    )
                else:
                    _worker_ports = None

                local_app_databag = ClusterProviderAppData(
                    worker_config=worker_config,
                    loki_endpoints=loki_endpoints,
                    ca_cert=ca_cert,
                    server_cert=server_cert,
                    privkey_secret_id=privkey_secret_id,
                    charm_tracing_receivers=charm_tracing_receivers,
                    workload_tracing_receivers=workload_tracing_receivers,
                    remote_write_endpoints=remote_write_endpoints,
                    s3_tls_ca_chain=s3_tls_ca_chain,
                    worker_ports=_worker_ports,
                )
                local_app_databag.dump(relation.data[self.model.app])

    @property
    def has_workers(self) -> bool:
        """Return True if the coordinator is connected to any worker."""
        # we use the presence of relations instead of addresses, because we want this
        # check to fail early
        return bool(self._relations)

    def _expand_roles(self, role_string: str) -> Set[str]:
        """Expand the meta-roles from a comma-separated list of roles."""
        expanded_roles: Set[str] = set()
        for role in role_string.split(","):
            if role in self._meta_roles:
                expanded_roles.update(self._meta_roles[role])
            else:
                expanded_roles.update({role})
        return expanded_roles

    def gather_addresses_by_role(self) -> Dict[str, Set[str]]:
        """Go through the worker's unit databags to collect all the addresses published by the units, by role."""
        data: Dict[str, Set[str]] = collections.defaultdict(set)
        for relation in self._relations:
            if not relation.app:
                log.debug(f"skipped {relation} as .app is None")
                continue

            try:
                worker_app_data = ClusterRequirerAppData.load(relation.data[relation.app])
            except cosl.interfaces.utils.DataValidationError as e:
                log.info(f"invalid databag contents: {e}")
                continue

            for worker_unit in relation.units:
                try:
                    worker_data = ClusterRequirerUnitData.load(relation.data[worker_unit])
                    unit_address = worker_data.address
                    for role in self._expand_roles(worker_app_data.role):
                        data[role].add(unit_address)
                except cosl.interfaces.utils.DataValidationError as e:
                    log.info(f"invalid databag contents: {e}")
                    continue
        return data

    def gather_addresses(self) -> Tuple[str, ...]:
        """Go through the worker's unit databags to collect all the addresses published by the units."""
        data: Set[str] = set()
        addresses_by_role = self.gather_addresses_by_role()
        for _, address_set in addresses_by_role.items():
            data.update(address_set)

        return tuple(sorted(data))

    def gather_roles(self) -> Dict[str, int]:
        """Go through the worker's app databags and sum the available application roles."""
        data: Counter[str] = collections.Counter()
        for relation in self._relations:
            if relation.app:
                remote_app_databag = relation.data[relation.app]
                try:
                    worker_role: str = ClusterRequirerAppData.load(remote_app_databag).role
                except cosl.interfaces.utils.DataValidationError as e:
                    log.error(f"invalid databag contents: {e}")
                    continue

                # the number of units with each role is the number of remote units
                role_n = len(relation.units)  # exclude this unit
                for role in self._expand_roles(worker_role):
                    data[role] += role_n

        dct = dict(data)
        return dct

    def gather_topology(self) -> List[Dict[str, str]]:
        """Gather Juju topology by unit."""
        data: List[Dict[str, str]] = []
        for relation in self._relations:
            if not relation.app:
                continue

            for worker_unit in relation.units:
                try:
                    worker_data = ClusterRequirerUnitData.load(relation.data[worker_unit])
                    unit_address = worker_data.address
                except cosl.interfaces.utils.DataValidationError as e:
                    log.info(f"invalid databag contents: {e}")
                    continue
                worker_topology = {
                    "address": unit_address,
                    "application": worker_data.juju_topology.application,
                    "unit": worker_data.juju_topology.unit,
                    "charm_name": worker_data.juju_topology.charm_name,
                }
                data.append(worker_topology)

        return data

    def get_address_from_role(self, role: str) -> Optional[str]:
        """Get datasource address."""
        addresses_by_role = self.gather_addresses_by_role()
        if address_set := addresses_by_role.get(role, None):
            return address_set.pop()
        return None

    def _remote_data_ready(self, relation: ops.Relation) -> bool:
        """Verify that each worker unit and the worker leader have published their data to the cluster relation.

        - unit address is published
        - roles are published
        """
        if not relation.app or not relation.units or not relation.data:
            return False

        # check if unit data is published
        for worker_unit in relation.units:
            try:
                ClusterRequirerUnitData.load(relation.data[worker_unit])
            except cosl.interfaces.utils.DataValidationError:
                return False

        # check if app data is published
        if self._charm.unit.is_leader():
            try:
                ClusterRequirerAppData.load(relation.data[relation.app])
            except cosl.interfaces.utils.DataValidationError:
                return False

        return True


class ClusterRequirer(Object):
    """``-cluster`` requirer endpoint wrapper."""

    on = ClusterRequirerEvents()  # type: ignore

    def __init__(
        self,
        charm: ops.CharmBase,
        key: Optional[str] = None,
        endpoint: str = DEFAULT_ENDPOINT_NAME,
    ):
        super().__init__(charm, key or endpoint)
        self._charm = charm
        self.juju_topology = cosl.JujuTopology.from_charm(self._charm)

        relation = self.model.get_relation(endpoint)
        self.relation: Optional[ops.Relation] = (
            relation if relation and relation.app and relation.data else None
        )

        self.framework.observe(
            self._charm.on[endpoint].relation_changed,
            self._on_cluster_relation_changed,  # type: ignore
        )
        self.framework.observe(
            self._charm.on[endpoint].relation_created,
            self._on_cluster_relation_created,  # type: ignore
        )
        self.framework.observe(
            self._charm.on[endpoint].relation_broken,
            self._on_cluster_relation_broken,  # type: ignore
        )

    def _on_cluster_relation_broken(self, _event: ops.RelationBrokenEvent):
        self.on.removed.emit()

    def _on_cluster_relation_created(self, event: ops.RelationCreatedEvent):
        self.on.created.emit(relation=event.relation, app=event.app, unit=event.unit)

    def _on_cluster_relation_changed(self, _event: ops.RelationChangedEvent):
        # to prevent the event from firing if the relation is in an unhealthy state (breaking...)
        if self.relation:
            new_config = self.get_worker_config()
            if new_config:
                self.on.config_received.emit(new_config)

            # if we have published our data, but we receive an empty/invalid config,
            # then the remote end must have removed it.
            elif self.is_published():
                self.on.removed.emit()

    def is_published(self):
        """Verify that the local side has done all they need to do.

        - unit address is published
        - roles are published
        """
        relation = self.relation
        if not relation:
            return False

        unit_data = relation.data[self._charm.unit]
        app_data = relation.data[self._charm.app]

        try:
            ClusterRequirerUnitData.load(unit_data)
            if self._charm.unit.is_leader():
                ClusterRequirerAppData.load(app_data)
        except cosl.interfaces.utils.DataValidationError as e:
            log.info(f"invalid databag contents: {e}")
            return False
        return True

    def publish_unit_address(self, url: str):
        """Publish this unit's URL via the unit databag."""
        try:
            urlparse(url)
        except Exception as e:
            raise ValueError(f"{url} is an invalid url") from e

        databag_model = ClusterRequirerUnitData(
            juju_topology=dict(self.juju_topology.as_dict()),  # type: ignore
            address=url,
        )
        relation = self.relation
        if relation:
            unit_databag = relation.data[self.model.unit]  # type: ignore # all checks are done in __init__
            databag_model.dump(unit_databag)

    def publish_app_roles(self, roles: Iterable[str]):
        """Publish this application's roles via the application databag."""
        if not self._charm.unit.is_leader():
            raise DatabagAccessPermissionError("only the leader unit can publish roles.")

        relation = self.relation
        if relation:
            databag_model = ClusterRequirerAppData(role=",".join(roles))
            databag_model.dump(relation.data[self.model.app])

    def _get_data_from_coordinator(self) -> Optional[ClusterProviderAppData]:
        """Fetch the contents of the doordinator databag."""
        data: Optional[ClusterProviderAppData] = None
        relation = self.relation
        # TODO: does this need a leader guard ??? maybe?
        if relation:
            try:
                databag = relation.data[relation.app]  # type: ignore # all checks are done in __init__
                coordinator_databag = ClusterProviderAppData.load(databag)
                data = coordinator_databag
            except cosl.interfaces.utils.DataValidationError as e:
                log.info(f"invalid databag contents: {e}")
                return None  # explicit is better than implicit

        return data

    def get_worker_config(self) -> Dict[str, Any]:
        """Fetch the worker config from the coordinator databag."""
        data = self._get_data_from_coordinator()
        if data:
            return yaml.safe_load(data.worker_config)
        return {}

    def get_worker_ports(self) -> Optional[Tuple[int, ...]]:
        """Obtain, from the cluster relation, the ports that the worker should be opening."""
        data = self._get_data_from_coordinator()
        if data and data.worker_ports:
            return tuple(data.worker_ports)
        return

    def get_loki_endpoints(self) -> Dict[str, str]:
        """Fetch the loki endpoints from the coordinator databag."""
        data = self._get_data_from_coordinator()
        if data:
            return data.loki_endpoints or {}
        return {}

    def get_tls_data(self, allow_none: bool = False) -> Optional[TLSData]:
        """Fetch certificates and the private key secrets id for the worker config."""
        data = self._get_data_from_coordinator()
        if not data:
            return None

        if (
            not data.ca_cert or not data.server_cert or not data.privkey_secret_id
        ) and not allow_none:
            return None

        return TLSData(
            ca_cert=data.ca_cert,
            server_cert=data.server_cert,
            privkey_secret_id=data.privkey_secret_id,
            s3_tls_ca_chain=data.s3_tls_ca_chain,
        )

    def get_charm_tracing_receivers(self) -> Dict[str, str]:
        """Fetch the charm tracing receivers from the coordinator databag."""
        data = self._get_data_from_coordinator()
        if data:
            return data.charm_tracing_receivers or {}
        return {}

    def get_workload_tracing_receivers(self) -> Dict[str, str]:
        """Fetch the workload tracing receivers from the coordinator databag."""
        data = self._get_data_from_coordinator()
        if data:
            return data.workload_tracing_receivers or {}
        return {}

    def get_remote_write_endpoints(self) -> List[RemoteWriteEndpoint]:
        """Fetch the remote write endpoints from the coordinator databag."""
        data = self._get_data_from_coordinator()
        if data:
            return data.remote_write_endpoints or []
        return []
