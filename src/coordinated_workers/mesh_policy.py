#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Coordinator helper extensions to configure service mesh policies for the coordinator."""

import logging
from typing import Dict, List, Optional, Set

import ops
from charms.istio_beacon_k8s.v0.service_mesh import (  # type: ignore
    MeshPolicy,
    PolicyResourceManager,
    PolicyTargetType,
    ServiceMeshConsumer,
)
from lightkube import Client

from coordinated_workers.interfaces.cluster import ClusterProvider


def _get_unit_mesh_policy_for_cluster_application(
    source_application: str,
    cluster_model: str,
    target_selector_labels: Dict[str, str],
) -> MeshPolicy:
    """Return mesh policy that grant access for the specified cluster application to target all cluster units."""
    # NOTE: The following policy assumes that the coordinator and worker will always be in the same model.
    return MeshPolicy(
        source_namespace=cluster_model,
        source_app_name=source_application,
        target_namespace=cluster_model,
        target_selector_labels=target_selector_labels,
        target_type=PolicyTargetType.unit,
    )


def _get_app_mesh_policy_for_cluster_application(
    source_application: str,
    charm: ops.CharmBase,
) -> MeshPolicy:
    """Return app mesh policy that grant access for the specified cluster application to target the coordinator application."""
    # NOTE: The following policy assumes that the coordinator and worker will always be in the same model.
    return MeshPolicy(
        source_namespace=charm.model.name,
        source_app_name=source_application,
        target_namespace=charm.model.name,
        target_app_name=charm.app.name,
        target_type=PolicyTargetType.app,
    )


def _get_cluster_internal_mesh_policies(
    charm: ops.CharmBase,
    cluster: ClusterProvider,
    target_selector_labels: Dict[str, str],
) -> List[MeshPolicy]:
    """Return all the required cluster internal mesh policies."""
    mesh_policies: List[MeshPolicy] = []
    # Coordinator -> Every unit in the cluster
    mesh_policies.append(
        _get_unit_mesh_policy_for_cluster_application(
            charm.app.name,
            charm.model.name,
            target_selector_labels,
        )
    )
    cluster_apps: Set[str] = {
        worker_unit["application"] for worker_unit in cluster.gather_topology()
    }
    for worker_app in cluster_apps:
        # Workers -> Every unit in the cluster
        mesh_policies.append(
            _get_unit_mesh_policy_for_cluster_application(
                worker_app,
                charm.model.name,
                target_selector_labels,
            )
        )
        # Workers -> Coordinator application in the cluster
        mesh_policies.append(
            _get_app_mesh_policy_for_cluster_application(
                worker_app,
                charm,
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
        prm.reconcile(policies, mesh_type)
    else:
        # if mesh_type is None, there is no active service-mesh relation. silently purge all policies, if any.
        prm.delete()
