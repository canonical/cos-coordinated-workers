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
    """Manages telemetry correlations between this charm's datasource and those obtained through grafana_datasource_exchange."""

    def __init__(
        self,
        app_name: str,
        grafana_source_relations: List[ops.Relation],
        datasource_exchange_relations: List[ops.Relation],
    ):
        self._app_name = app_name
        self._grafana_source_relations = grafana_source_relations
        self._datasource_exchange_relations = datasource_exchange_relations

    def find_correlated_datasource(
        self,
        datasource_type: str,
        correlation_feature: str,
        endpoint_relations: Optional[List[ops.Relation]] = None,
    ) -> Optional[GrafanaDatasource]:
        """Find a datasource (from grafana_datasource_exchange) that should be correlated with this charm's datasource.

        The correlated datasource is one of type `datasource_type`, connected to the
        same grafana instance(s) as this charm. If `endpoint_relations` is provided, the search is
        narrowed down to datasources that this charm is also related to through those specific `endpoint_relations`.

        Args:
            datasource_type: The type of the datasource to correlate with (e.g. "loki")
            correlation_feature: The correlation feature being configured (e.g. "traces-to-logs")
            endpoint_relations: Optional extra filter to narrow the search to datasources integrated
                with this charm also through the specified endpoint relations. If not provided, this filter
                is ignored when choosing the correlated datasource.

        Returns:
            The correlated Grafana datasource if found, otherwise None.
        """
        dsx_relations = [
            rel for rel in self._datasource_exchange_relations if rel.app and rel.data
        ]
        endpoint_name = endpoint_relations[0].name if endpoint_relations else ""
        endpoint_remote_apps = set()
        filtered_dsx_relations = dsx_relations
        if endpoint_relations:
            # apps this charm is integrated with over the extra given endpoint
            endpoint_remote_apps: Set[str] = {
                relation.app.name
                for relation in endpoint_relations
                if relation.app and relation.data
            }
            # relations that this charm connects to via both datasource-exchange and the extra given endpoint
            filtered_dsx_relations = [
                rel for rel in dsx_relations if rel.app.name in endpoint_remote_apps
            ]

        # grafana UIDs that are connected to this charm.
        my_connected_grafana_uids = set(self._get_grafana_source_uids())

        filtered_dsx_databags: List[DSExchangeAppData] = []
        for relation in sorted(filtered_dsx_relations, key=lambda x: x.id):
            try:
                datasource = DSExchangeAppData.load(relation.data[relation.app])
                filtered_dsx_databags.append(datasource)
            except DataValidationError:
                # load() already logs
                continue

        # filter datasources by the desired datasource type
        matching_type_datasources = [
            datasource
            for databag in filtered_dsx_databags
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
            if not my_connected_grafana_uids:
                missing_rels.append("grafana_datasource")
            if not dsx_relations:
                missing_rels.append("grafana_datasource_exchange")
            if endpoint_relations and not endpoint_remote_apps:
                missing_rels.append(endpoint_name)

            if missing_rels and not filtered_dsx_relations:
                logger.info(
                    "%s disabled. Missing relations: %s.",
                    correlation_feature,
                    missing_rels,
                )
            elif endpoint_relations and not filtered_dsx_relations:
                logger.info(
                    "%s disabled. There are no grafana_datasource_exchange relations "
                    "with a '%s' that %s is related to on '%s'.",
                    correlation_feature,
                    datasource_type,
                    self._app_name,
                    endpoint_name,
                )
            elif not matching_type_datasources:
                logger.info(
                    "%s disabled. There are grafana_datasource_exchange relations, "
                    "but none of the datasources are of the type %s.",
                    correlation_feature,
                    datasource_type,
                )
            else:
                logger.info(
                    "%s disabled. There are grafana_datasource_exchange relations, "
                    "but none of the datasources are connected to the same Grafana instances as %s.",
                    correlation_feature,
                    self._app_name,
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
        for rel in self._grafana_source_relations:
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
