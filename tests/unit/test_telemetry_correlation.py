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
                "my-correlation-endpoint": {"interface": "custom_endpoint"},
            },
            "provides": {
                "my-grafana-source": {"interface": "grafana_dashboard"},
                "my-ds-exchange-provide": {"interface": "grafana_datasource_exchange"},
            },
        }

        def __init__(self, framework: ops.Framework):
            super().__init__(framework)
            self._telemetry_correlation = TelemetryCorrelation(
                charm=self,
                grafana_ds_endpoint="my-grafana-source",
                grafana_dsx_endpoint="my-ds-exchange-provide",
            )

        def get_correlated_datasource(self, datasource_type="prometheus"):
            return self._telemetry_correlation.find_correlated_datasource(
                endpoint="my-correlation-endpoint",
                datasource_type=datasource_type,
                correlation_feature=datasource_type,
            )

    return MyCorrelationCharm


@pytest.fixture(scope="function")
def context(charm):
    return Context(charm_type=charm, meta=charm.META)


def _grafana_source_relation(
    remote_name: str = "remote",
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
        "my-ds-exchange-provide",
        remote_app_name=remote_name,
        remote_app_data={"datasources": json.dumps(datasources)},
    )


def _correlation_endpoint_relation(
    remote_name: str = "remote",
):
    return Relation(
        "my-correlation-endpoint",
        remote_app_name=remote_name,
    )


@pytest.mark.parametrize(
    "has_dsx, has_grafana_source, has_correlation_endpoint",
    [
        (True, True, False),
        (True, False, True),
        (False, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (False, False, False),
    ],
)
def test_no_matching_datasource_with_missing_rels(
    has_grafana_source,
    has_correlation_endpoint,
    has_dsx,
    context,
):
    # GIVEN an incomplete list of relations that are mandatory for telemetry correlation
    relations = []
    if has_grafana_source:
        relations.append(_grafana_source_relation())
    if has_dsx:
        relations.append(_grafana_datasource_exchange_relation())
    if has_correlation_endpoint:
        relations.append(_correlation_endpoint_relation())

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


def test_no_matching_datasource_with_the_wrong_dsx(context):
    # GIVEN a relation over correlation-endpoint to a remote app "remote"
    relations = {_grafana_source_relation(), _correlation_endpoint_relation(remote_name="remote")}
    # AND a relation over ds-exchange to a different remote app "remote2"
    relations.add(_grafana_datasource_exchange_relation(remote_name="remote2"))

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
    relations = {
        _grafana_source_relation(grafana_uid="grafana1"),
        _correlation_endpoint_relation(),
    }
    # AND a relation over ds-exchange to a remote app that is connected to a different grafana "grafana2"
    relations.add(
        _grafana_datasource_exchange_relation(
            datasources=[{"type": "prometheus", "uid": "prometheus_1", "grafana_uid": "grafana2"}]
        )
    )

    state_in = State(relations=relations)

    # WHEN we fire any event
    with context(context.on.update_status(), state_in) as mgr:
        mgr.run()
        charm = mgr.charm
        correlated_datasource = charm.get_correlated_datasource()
        # THEN no matching datasources are found
        assert not correlated_datasource


def test_matching_datasource_found(context):
    # GIVEN a relation over grafana-source
    # AND a relation over correlation-endpoint to a remote
    # AND a relation over ds-exchange to the same remote that is connected to the same grafana as this charm
    relations = {
        _grafana_source_relation(),
        _correlation_endpoint_relation(),
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


def test_multiple_matching_datasource_found(context):
    # GIVEN a relation over grafana-source
    # AND a relation over correlation-endpoint to a remote "remote1"
    # AND a relation over correlation-endpoint to another remote "remote2"
    # AND a relation over ds-exchange to "remote1" that is connected to the same grafana as this charm
    # AND a relation over ds-exchange to "remote2" that is connected to the same grafana as this charm
    relations = {
        _grafana_source_relation(),
        _correlation_endpoint_relation(remote_name="remote1"),
        _correlation_endpoint_relation(remote_name="remote2"),
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
