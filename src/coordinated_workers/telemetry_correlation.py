#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Utilities for working with telemetry correlation."""

import json
import logging
from typing import Dict, List, Optional, Set

import ops
from cosl.interfaces.datasource_exchange import (
    DSExchangeAppData,
    GrafanaDatasource,
)
from cosl.interfaces.utils import DataValidationError

logger = logging.getLogger(__name__)


class TelemetryCorrelation:
    """Manages telemetry correlations between this charm's datasource and those obtained through datasource_exchange."""

    def __init__(
        self,
        charm: ops.CharmBase,
        grafana_ds_endpoint: str,
        grafana_dsx_endpoint: str,
    ):
        self._charm = charm
        self._grafana_ds_endpoint = grafana_ds_endpoint
        self._grafana_dsx_endpoint = grafana_dsx_endpoint

    def find_correlated_datasource(
        self,
        datasource_type: str,
        correlation_feature: str,
        endpoint: Optional[str] = None,
    ) -> Optional[GrafanaDatasource]:
        """Find a datasource (from datasource_exchange) that should be correlated with this charm's datasource.

        The correlated datasource is one of type `datasource_type`, connected to the
        same grafana instance(s) as this charm. If `endpoint` is provided, the search is
        narrowed down to datasources that this charm is related to over this `endpoint`.

        Args:
            datasource_type: The type of the datasource to correlate with (e.g. "loki")
            correlation_feature: The correlation feature being configured (e.g. "traces-to-logs")
            endpoint: Optional relation endpoint (e.g. "send-remote-write") to narrow down
                the search to a datasource that this charm is related to over this endpoint

        Returns:
            The correlated Grafana datasource if found, otherwise None.
        """
        all_dsx_relations = {
            relation.app.name: relation
            for relation in self._charm.model.relations.get(self._grafana_dsx_endpoint, [])
        }

        remote_apps_on_endpoint: Set[str] = (
            {
                relation.app.name
                for relation in self._charm.model.relations.get(endpoint, [])
                if relation.app and relation.data
            }
            if endpoint
            else set()
        )

        # relations that this charm connects to via both datasource-exchange and the given endpoint, if provided
        filtered_dsx_relations = (
            [
                all_dsx_relations[app_name]
                for app_name in set(all_dsx_relations).intersection(remote_apps_on_endpoint)
            ]
            if endpoint
            else list(all_dsx_relations.values())
        )

        # grafana UIDs that are connected to this charm.
        my_connected_grafana_uids = set(self._get_grafana_source_uids())

        endpoint_dsx_databags: List[DSExchangeAppData] = []
        for relation in sorted(filtered_dsx_relations, key=lambda x: x.id):
            try:
                datasource = DSExchangeAppData.load(relation.data[relation.app])
                endpoint_dsx_databags.append(datasource)
            except DataValidationError:
                # load() already logs
                continue

        # filter datasources by the desired datasource type
        matching_type_datasources = [
            datasource
            for databag in endpoint_dsx_databags
            for datasource in databag.datasources
            if datasource.type == datasource_type
        ]

        # keep only the ones connected to the same Grafana instances
        matching_grafana_datasources = [
            ds for ds in matching_type_datasources if ds.grafana_uid in my_connected_grafana_uids
        ]

        if not matching_grafana_datasources:
            # take good care of logging exactly why this happening, as the logic is quite complex and debugging this will be hell
            missing_rels: List[str] = []
            if not remote_apps_on_endpoint and endpoint:
                missing_rels.append(endpoint)
            if not my_connected_grafana_uids:
                missing_rels.append("grafana-source")
            if not all_dsx_relations:
                missing_rels.append("receive-datasource")

            if missing_rels and not filtered_dsx_relations:
                logger.info(
                    "%s disabled. Missing relations: %s.",
                    correlation_feature,
                    missing_rels,
                )
            elif endpoint and not filtered_dsx_relations:
                logger.info(
                    "%s disabled. There are no '%s' relations "
                    "with a '%s' that %s is related to on '%s'.",
                    correlation_feature,
                    self._grafana_dsx_endpoint,
                    datasource_type,
                    self._charm.app.name,
                    endpoint,
                )
            elif not matching_type_datasources:
                logger.info(
                    "%s disabled. '%s' relations exist, "
                    "but none of the datasources are of the type %s.",
                    correlation_feature,
                    self._grafana_dsx_endpoint,
                    datasource_type,
                )
            else:
                logger.info(
                    "%s disabled. '%s' relations exist, "
                    "but none of the datasources are connected to the same Grafana instances as %s.",
                    correlation_feature,
                    self._grafana_dsx_endpoint,
                    self._charm.app.name,
                )
            return None

        if len(matching_grafana_datasources) > 1:
            logger.info(
                "multiple eligible datasources found for %s: %s. Assuming they are equivalent.",
                correlation_feature,
                [ds.uid for ds in matching_grafana_datasources],
            )

        # At this point, we can assume any datasource is a valid datasource to use.
        return matching_grafana_datasources[0]

    def _get_grafana_source_uids(self) -> Dict[str, Dict[str, str]]:
        """Helper method to retrieve the databags of any grafana-source relations.

        Duplicate implementation of GrafanaSourceProvider.get_source_uids() to use in the
        situation where we want to access relation data when the GrafanaSourceProvider object
        is not yet initialised.
        """
        uids: Dict[str, Dict[str, str]] = {}
        for rel in self._charm.model.relations.get(self._grafana_ds_endpoint, []):
            if not rel:
                continue
            app_databag = rel.data[rel.app]
            grafana_uid = app_databag.get("grafana_uid")
            if not grafana_uid:
                logger.warning(
                    "remote end is using an old grafana_datasource interface: "
                    "`grafana_uid` field not found."
                )
                continue

            uids[grafana_uid] = json.loads(app_databag.get("datasource_uids", "{}"))
        return uids
