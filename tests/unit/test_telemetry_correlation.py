#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import json
from typing import Dict, List

import ops
import pytest
from ops.testing import Context, Relation, State

from coordinated_workers.telemetry_correlation import TelemetryCorrelation


@pytest.fixture()
def charm():
    class MyCorrelationCharm(ops.CharmBase):
        META = {
            "name": "foo-app",
            "requires": {
                "my-custom-endpoint": {"interface": "custom_endpoint"},
            },
            "provides": {
                "my-grafana-source": {"interface": "grafana_dashboard"},
                "my-ds-exchange": {"interface": "grafana_datasource_exchange"},
            },
        }

        def __init__(self, framework: ops.Framework):
            super().__init__(framework)
            self._telemetry_correlation = TelemetryCorrelation(
                app_name=self.app.name,
                grafana_source_relations=self.model.relations["my-grafana-source"],
                datasource_exchange_relations=self.model.relations["my-ds-exchange"],
            )

        def get_correlated_datasource(self, endpoint_relations=None):
            return self._telemetry_correlation.find_correlated_datasource(
                datasource_type="prometheus",
                correlation_feature="traces-to-metrics",
                endpoint_relations=endpoint_relations,
            )

    return MyCorrelationCharm


@pytest.fixture(scope="function")
def context(charm):
    return Context(charm_type=charm, meta=charm.META)


def _grafana_source_relation(
    remote_name: str = "grafana1",
    datasource_uids: Dict[str, str] = {"tempo/0": "1234"},
    grafana_uid: str = "grafana_1",
):
    return Relation(
        "my-grafana-source",
        remote_app_name=remote_name,
        remote_app_data={
            "datasource_uids": json.dumps(datasource_uids),
            "grafana_uid": grafana_uid,
        },
    )


def _grafana_datasource_exchange_relation(
    remote_name: str = "remote",
    datasources: List[Dict[str, str]] = [
        {"type": "prometheus", "uid": "prometheus_1", "grafana_uid": "grafana_1"}
    ],
):
    return Relation(
        "my-ds-exchange",
        remote_app_name=remote_name,
        remote_app_data={"datasources": json.dumps(datasources)},
    )


def _custom_endpoint_relation(
    remote_name: str = "remote",
):
    return Relation(
        "my-custom-endpoint",
        remote_app_name=remote_name,
    )


@pytest.mark.parametrize(
    "has_dsx, has_grafana_source",
    [
        (True, False),
        (False, True),
        (False, False),
    ],
)
def test_no_matching_datasource_with_missing_rels(
    has_dsx,
    has_grafana_source,
    context,
):
    # GIVEN an incomplete list of relations that are mandatory for telemetry correlation
    relations = []
    if has_grafana_source:
        relations.append(_grafana_source_relation())
    if has_dsx:
        relations.append(_grafana_datasource_exchange_relation())

    state_in = State(
        relations=relations,
    )

    # WHEN we fire any event
    with context(context.on.update_status(), state_in) as mgr:
        mgr.run()
        charm = mgr.charm
        correlated_datasource = charm.get_correlated_datasource()
        # THEN no matching datasources are found
        assert not correlated_datasource


def test_no_matching_datasource_with_the_wrong_ds_type(context):
    # GIVEN a relation over grafana-source
    # AND a relation over ds-exchange to a remote app with the wrong ds type

    relations = {
        _grafana_source_relation(),
        _grafana_datasource_exchange_relation(
            datasources=[{"type": "loki", "uid": "loki_1", "grafana_uid": "grafana_1"}]
        ),
    }

    state_in = State(relations=relations)

    # WHEN we fire any event
    with context(context.on.update_status(), state_in) as mgr:
        mgr.run()
        charm = mgr.charm
        correlated_datasource = charm.get_correlated_datasource()
        # THEN no matching datasources are found
        assert not correlated_datasource


def test_no_matching_datasource_with_the_wrong_grafana(context):
    # GIVEN a relation over grafana-source to a remote app "grafana1"
    # AND a relation over ds-exchange to a remote app that is connected to a different grafana "grafana2"
    relations = {
        _grafana_source_relation(grafana_uid="grafana1"),
        _grafana_datasource_exchange_relation(
            datasources=[{"type": "prometheus", "uid": "prometheus_1", "grafana_uid": "grafana2"}]
        ),
    }

    state_in = State(relations=relations)

    # WHEN we fire any event
    with context(context.on.update_status(), state_in) as mgr:
        mgr.run()
        charm = mgr.charm
        correlated_datasource = charm.get_correlated_datasource()
        # THEN no matching datasources are found
        assert not correlated_datasource


def test_no_matching_datasource_with_endpoint(context):
    # GIVEN a relation over grafana-source
    # AND a relation over ds-exchange to a remote app "remote"
    # AND a custom-endpoint relation to a different remote app "remote2"
    relations = {
        _grafana_source_relation(),
        _grafana_datasource_exchange_relation(remote_name="remote"),
        _custom_endpoint_relation(remote_name="remote2"),
    }

    state_in = State(relations=relations)

    # WHEN we fire any event
    with context(context.on.update_status(), state_in) as mgr:
        mgr.run()
        charm = mgr.charm
        # AND we call find_correlated_datasource with additional endpoint_relations to filter datasources with
        correlated_datasource = charm.get_correlated_datasource(
            endpoint_relations=charm.model.relations["my-custom-endpoint"]
        )
        # THEN no matching datasources are found
        assert not correlated_datasource


def test_matching_datasource_found(context):
    # GIVEN a relation over grafana-source
    # AND a relation over ds-exchange to the same remote that is connected to the same grafana as this charm
    relations = {
        _grafana_source_relation(),
        _grafana_datasource_exchange_relation(),
    }

    state_in = State(relations=relations)

    # WHEN we fire any event
    with context(context.on.update_status(), state_in) as mgr:
        mgr.run()
        charm = mgr.charm
        correlated_datasource = charm.get_correlated_datasource()
        # THEN we find a matching datasource
        assert correlated_datasource
        # AND this datasource.uid matches the one we obtain from ds-exchange
        assert correlated_datasource.uid == "prometheus_1"


def test_matching_datasource_found_with_endpoint(context):
    # GIVEN a relation over grafana-source
    # AND a relation over ds-exchange to the same remote that is connected to the same grafana as this charm
    # AND a relation over custom-endpoint to the same remote that this charm is connected to over ds-exchange
    relations = {
        _grafana_source_relation(),
        _grafana_datasource_exchange_relation(),
        _custom_endpoint_relation(),
    }

    state_in = State(relations=relations)

    # WHEN we fire any event
    with context(context.on.update_status(), state_in) as mgr:
        mgr.run()
        charm = mgr.charm
        # AND we call find_correlated_datasource with additional endpoint_relations to filter datasources with
        correlated_datasource = charm.get_correlated_datasource(
            endpoint_relations=charm.model.relations["my-custom-endpoint"]
        )
        # THEN we find a matching datasource
        assert correlated_datasource
        # AND this datasource.uid matches the one we obtain from ds-exchange
        assert correlated_datasource.uid == "prometheus_1"


def test_multiple_matching_datasource_found(context):
    # GIVEN a relation over grafana-source
    # AND a relation over ds-exchange to "remote1" that is connected to the same grafana as this charm
    # AND a relation over ds-exchange to "remote2" that is connected to the same grafana as this charm
    relations = {
        _grafana_source_relation(),
        _grafana_datasource_exchange_relation(
            remote_name="remote1",
            datasources=[
                {"type": "prometheus", "uid": "prometheus_1", "grafana_uid": "grafana_1"}
            ],
        ),
        _grafana_datasource_exchange_relation(
            remote_name="remote2",
            datasources=[
                {"type": "prometheus", "uid": "prometheus_2", "grafana_uid": "grafana_1"}
            ],
        ),
    }

    state_in = State(relations=relations)

    # WHEN we fire any event
    with context(context.on.update_status(), state_in) as mgr:
        mgr.run()
        charm = mgr.charm
        correlated_datasource = charm.get_correlated_datasource()
        # THEN we find a matching datasource
        assert correlated_datasource
        # AND we assume all matching datasources are identical so we fetch the first one we obtain from ds-exchange
        assert correlated_datasource.uid == "prometheus_1"
