#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Utilities for working with telemetry correlation."""

import json
import logging
from typing import Dict, List, Optional

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
        endpoint: str,
        datasource_type: str,
        correlation_feature: str,
    ) -> Optional[GrafanaDatasource]:
        """Find the datasource (from datasource-exchange) that should be correlated with this charm over the given relation endpoint.

        Args:
            endpoint: The endpoint (e.g. "logging") on which the correlation will happen.
            datasource_type: The expected datasource type to correlate with (e.g. "loki").
            correlation_feature: The correlation feature being configured (e.g. "traces-to-logs").

        Returns:
            The correlated Grafana datasource if found, otherwise None.
        """
        all_dsx_relations = {
            relation.app.name: relation
            for relation in self._charm.model.relations.get(self._grafana_dsx_endpoint, [])
        }

        remote_apps_on_endpoint = {
            relation.app.name
            for relation in self._charm.model.relations.get(endpoint, [])
            if relation.app and relation.data
        }

        # relations that this charm connects to via both datasource-exchange and the given endpoint
        endpoint_dsx_relations = [
            all_dsx_relations[app_name]
            for app_name in set(all_dsx_relations).intersection(remote_apps_on_endpoint)
        ]

        # grafana UIDs that are connected to this charm.
        my_connected_grafana_uids = set(self._get_grafana_source_uids())

        endpoint_dsx_databags: List[DSExchangeAppData] = []
        for relation in sorted(endpoint_dsx_relations, key=lambda x: x.id):
            try:
                datasource = DSExchangeAppData.load(relation.data[relation.app])
                endpoint_dsx_databags.append(datasource)
            except DataValidationError:
                # load() already logs
                continue

        # filter the endpoint_dsx_databags with those that are connected to the same grafana instances this charm is connected to.
        matching_datasources = [
            datasource
            for databag in endpoint_dsx_databags
            for datasource in databag.datasources
            if datasource.grafana_uid in my_connected_grafana_uids
            and datasource.type == datasource_type
        ]

        if not matching_datasources:
            # take good care of logging exactly why this happening, as the logic is quite complex and debugging this will be hell
            missing_rels: List[str] = []
            if not remote_apps_on_endpoint:
                missing_rels.append(endpoint)
            if not my_connected_grafana_uids:
                missing_rels.append("grafana-source")
            if not all_dsx_relations:
                missing_rels.append("receive-datasource")

            if missing_rels and not endpoint_dsx_relations:
                logger.info(
                    "%s disabled. Missing relations: %s. "
                    "There are no receive-datasource relations with a '%s' that %s is also related to on '%s'.",
                    correlation_feature,
                    missing_rels,
                    datasource_type,
                    self._charm.app.name,
                    endpoint,
                )
            elif missing_rels:
                logger.info(
                    "%s disabled. Missing relations: %s.",
                    correlation_feature,
                    missing_rels,
                )
            elif not endpoint_dsx_relations:
                logger.info(
                    "%s disabled. There are no receive-datasource relations "
                    "with a '%s' that %s is related to on '%s'.",
                    correlation_feature,
                    datasource_type,
                    self._charm.app.name,
                    endpoint,
                )
            else:
                logger.info(
                    "%s disabled. receive-datasource relations exist, "
                    "but none of their datasources are connected to the same Grafana instances as %s.",
                    correlation_feature,
                    self._charm.app.name,
                )
            return None

        if len(matching_datasources) > 1:
            logger.info(
                "multiple eligible datasources found for %s: %s. Assuming they are equivalent.",
                correlation_feature,
                [ds.uid for ds in matching_datasources],
            )

        # At this point, we can assume any datasource is a valid datasource to use.
        return matching_datasources[0]

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
