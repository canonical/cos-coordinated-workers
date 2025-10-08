#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Coordinator helper extensions to configure service mesh policies for the coordinator."""

import dataclasses
import logging
from typing import Dict, List, Optional, Set, Union

import ops
from charms.istio_beacon_k8s.v0.service_mesh import (  # type: ignore
    AppPolicy,
    MeshPolicy,
    PolicyResourceManager,
    PolicyTargetType,
    ServiceMeshConsumer,
    UnitPolicy,
)
from lightkube import Client

from coordinated_workers.interfaces.cluster import ClusterProvider


@dataclasses.dataclass
class CharmPolicies:
    """Charm specific mesh policies.

    Args:
        cluster_external: Policies that control incoming traffic to the coordinator. Managed by the service mesh charm.
        cluster_internal: Policies that control cluster internal traffic. Managed by the coordinator.
    """

    cluster_external: Optional[List[Union[AppPolicy, UnitPolicy]]] = None
    cluster_internal: Optional[List[MeshPolicy]] = None


def _get_mesh_policies_for_cluster_application(
    source_application: str,
    cluster_model: str,
    target_selector_labels: Dict[str, str],
    charm: ops.CharmBase,
    worker: bool = False,
) -> List[MeshPolicy]:
    """Return mesh policy that grant access for the specified cluster application to target all cluster units."""
    # NOTE: The following policy assumes that the coordinator and worker will always be in the same model.
    mesh_policy: List[MeshPolicy] = [
        MeshPolicy(
            source_namespace=cluster_model,
            source_app_name=source_application,
            target_namespace=cluster_model,
            target_selector_labels=target_selector_labels,
            target_type=PolicyTargetType.unit,
        )
    ]
    if worker:
        # for worker applications, allow the worker units to also access the coordinator's service URL.
        # This is required for proxying telemetry and self-monitoring.
        mesh_policy.append(
            MeshPolicy(
                source_namespace=cluster_model,
                source_app_name=source_application,
                target_namespace=cluster_model,
                target_app_name=charm.app.name,
                target_type=PolicyTargetType.app,
            )
        )
    return mesh_policy


def _get_cluster_internal_mesh_policies(
    charm: ops.CharmBase,
    cluster: ClusterProvider,
    target_selector_labels: Dict[str, str],
) -> List[MeshPolicy]:
    """Return all the required cluster internal mesh policies."""
    mesh_policies: List[MeshPolicy] = []
    # Coordinator -> Everything in the cluster
    mesh_policies.extend(
        _get_mesh_policies_for_cluster_application(
            charm.app.name,
            charm.model.name,
            target_selector_labels,
            charm,
        )
    )
    cluster_apps: Set[str] = {
        worker_unit["application"] for worker_unit in cluster.gather_topology()
    }
    # Workers -> Everything in the cluster
    for worker_app in cluster_apps:
        mesh_policies.extend(
            _get_mesh_policies_for_cluster_application(
                worker_app,
                charm.model.name,
                target_selector_labels,
                charm,
                worker=True,
            )
        )
    return mesh_policies


def _get_policy_resource_manager(
    charm: ops.CharmBase, logger: logging.Logger
) -> PolicyResourceManager:
    """Return a PolicyResourceManager for the given mesh_type."""
    return PolicyResourceManager(
        charm=charm,
        lightkube_client=Client(field_manager=charm.app.name),  # type: ignore
        labels={
            "app.kubernetes.io/instance": f"{charm.app.name}",
            "kubernetes-resource-handler-scope": f"{charm.app.name}-cluster-internal",
        },
        logger=logger,
    )


def reconcile(
    mesh: Optional[ServiceMeshConsumer],
    cluster: ClusterProvider,
    charm: ops.CharmBase,
    target_selector_labels: Dict[str, str],
    logger: logging.Logger,
    charm_policies: Optional[CharmPolicies],
) -> None:
    """Reconcile all the cluster internal mesh policies."""
    if not mesh:  # If mesh is None, we have no service-mesh endpoint in the charm.
        return
    mesh_type = mesh.mesh_type()  # type: ignore
    prm = _get_policy_resource_manager(charm, logger)
    if mesh_type:
        # if mesh_type exists, the charm is connected to a service mesh charm. reconcile the cluster interal policies.
        policies = _get_cluster_internal_mesh_policies(
            charm,
            cluster,
            target_selector_labels,
        )
        if charm_policies and charm_policies.cluster_internal:
            policies.extend(charm_policies.cluster_internal)
        prm.reconcile(policies, mesh_type)
    else:
        # if mesh_type is None, there is no active service-mesh relation. silently purge all policies, if any.
        prm.delete()
