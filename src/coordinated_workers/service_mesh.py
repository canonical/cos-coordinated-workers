#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Coordinator helper extensions to configure service mesh policies for the coordinator."""

import logging
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Union, cast

import ops
from charms.istio_beacon_k8s.v0.service_mesh import (  # type: ignore
    AppPolicy,
    Endpoint,
    MeshPolicy,
    Method,
    PolicyResourceManager,
    PolicyTargetType,
    ServiceMeshConsumer,
    UnitPolicy,
)
from lightkube import Client

from coordinated_workers.interfaces.cluster import ClusterProvider

if TYPE_CHECKING:
    from coordinated_workers.coordinator import _EndpointMapping  # type: ignore


def _get_unit_mesh_policy_for_cluster_application(
    source_application: str,
    cluster_model: str,
    target_selector_labels: Dict[str, str],
) -> MeshPolicy:
    """Return a mesh policy that grants access for the specified cluster application that targets all the units with the given label selector."""
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
    """Return a mesh policy that grants access for the specified application to the coordinator application."""
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
        lightkube_client=Client(field_manager=f"{charm.app.name}-{charm.model.name}"),  # type: ignore
        labels={
            "app.kubernetes.io/instance": f"{charm.app.name}-{charm.model.name}",
            "kubernetes-resource-handler-scope": f"{charm.app.name}-{charm.model.name}-cluster-internal",
        },
        logger=logger,
    )


def initialize(
    endpoints: "_EndpointMapping",
    charm: ops.CharmBase,
    nginx_exporter_port: int,
    proxy_worker_telemetry_paths: Dict[str, str],
    proxy_worker_telemetry_port: Optional[int],
    charm_mesh_policies: Optional[List[Union[AppPolicy, UnitPolicy]]],
) -> Optional[ServiceMeshConsumer]:
    """Instantiate service mesh consumer for the coordinator."""
    mesh: Optional[ServiceMeshConsumer] = None
    charm_mesh_policies = charm_mesh_policies if charm_mesh_policies else []
    if all(
        (
            mesh_relation_name := endpoints.get("service-mesh"),
            provide_cmr_mesh_name := endpoints.get("service-mesh-provide-cmr-mesh"),
            require_cmr_mesh_name := endpoints.get("service-mesh-require-cmr-mesh"),
        )
    ):
        default_policies: List[Union[AppPolicy, UnitPolicy]] = [
            # UnitPolicy for metrics-endpoint allows scrapers to scrape Coordinator's nginx pod
            UnitPolicy(
                relation=endpoints["metrics"],
                ports=[nginx_exporter_port],
            )
        ]

        if proxy_worker_telemetry_port:
            default_policies.append(
                # AppPolicy for metrics-endpoint allows scrapers to scrape through Coordinator's proxy to the
                # workers
                AppPolicy(
                    relation=endpoints["metrics"],
                    endpoints=[
                        Endpoint(
                            ports=[proxy_worker_telemetry_port]
                            if proxy_worker_telemetry_port
                            else [],
                            methods=[Method.get],
                            paths=[
                                proxy_worker_telemetry_paths["metrics"].replace("{unit}", "{*}")
                            ],
                        )
                    ],
                )
            )

        mesh = ServiceMeshConsumer(  # type: ignore
            charm,
            mesh_relation_name=cast(str, mesh_relation_name),
            cross_model_mesh_provides_name=cast(str, provide_cmr_mesh_name),
            cross_model_mesh_requires_name=cast(str, require_cmr_mesh_name),
            policies=default_policies + charm_mesh_policies,  # type: ignore
        )
    elif any(
        (
            endpoints.get("service-mesh"),
            endpoints.get("service-mesh-provide-cmr-mesh"),
            endpoints.get("service-mesh-require-cmr-mesh"),
        )
    ):
        raise ValueError(
            "If any of 'service-mesh', 'service-mesh-provide-cmr-mesh' or "
            "'service-mesh-require-cmr-mesh' endpoints are provided, all of them must be."
        )
    return mesh


def reconcile_cluster_internal_mesh_policies(
    mesh: Optional[ServiceMeshConsumer],
    cluster: ClusterProvider,
    charm: ops.CharmBase,
    target_selector_labels: Dict[str, str],
    logger: logging.Logger,
) -> None:
    """Reconcile all the cluster internal mesh policies."""
    if not mesh:  # If mesh is None, we have no service-mesh endpoint in the charm.
        return
    mesh_type = mesh.mesh_type()
    prm = _get_policy_resource_manager(charm, logger)
    if mesh_type:
        # if mesh_type exists, the charm is connected to a service mesh charm. reconcile the cluster internal policies.
        policies = _get_cluster_internal_mesh_policies(
            charm,
            cluster,
            target_selector_labels,
        )
        prm.reconcile(policies, mesh_type)  # type: ignore
    else:
        # if mesh_type is None, there is no active service-mesh relation. silently purge all policies, if any.
        prm.delete()
