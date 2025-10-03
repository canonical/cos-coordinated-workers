#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Generic juju-doctor probe to test coordinated-workers deployments for consistency."""

from collections import Counter
from typing import Any, Dict, List

import yaml


def bundle(
    bundles: Dict[str, dict[str, Any]],
    *args,
    worker_charm: str,
    recommended_deployment: Dict[str, int],
    **kwargs,
):
    """Bundle assertion for coordinated workers recommended scale.

    >>> bundle({"recommended": example_recommended_bundle()}, worker_charm="tempo-worker-k8s", recommended_deployment=example_recommended_deployment())  # doctest: +ELLIPSIS

    >>> bundle({"degraded": example_degraded_bundle()}, worker_charm="tempo-worker-k8s", recommended_deployment=example_recommended_deployment())  # doctest: +ELLIPSIS
    Traceback (most recent call last):
    ...
    AssertionError: Errors found: tempo-worker-k8s ... should be scaled up by 2 ...
    """  # noqa: E501
    errors: List[str] = []

    n_all_roles = 0
    roles = Counter()
    for bndl in bundles.values():
        for app_name, app in bndl["applications"].items():
            charm = app["charm"]
            scale = app["scale"]
            if charm.startswith("local:"):
                # in bundle export, the charm name looks like: local:tempo-worker-k8s-1
                # for whatever reason
                charm = "-".join(charm.split(":")[1].split("-")[:-1])
            if charm != worker_charm:
                continue
            config = app.get("options", {})
            if not config:
                # all role: counts as one of each
                n_all_roles += scale
                continue

            has_role_set = False

            for option, value in config.items():
                if option.startswith("role-") and value is True:
                    if has_role_set:
                        errors.append(
                            f"{app_name} has more than one role- config option set to True"
                        )
                    role = option[len("role-") :]

                    if role == "all":
                        n_all_roles += scale
                    else:
                        roles[role] += scale

    # now we check if each recommended role, is satisfied by the explicitly counted roles
    for role in recommended_deployment:
        # if we have nodes with the role all, we lower the target bar for all other roles.
        n_units = roles.get(role, None)
        if not n_all_roles and n_units is None:
            errors.append(f"{worker_charm} deployment is missing required role: {role}")
            continue

        missing = recommended_deployment[role] - n_all_roles - (n_units or 0)
        if missing > 0:
            errors.append(
                f"{worker_charm} deployment should be scaled up by {missing} {role} units."
            )

    if errors:
        joined_errors = "\n".join(errors)
        raise AssertionError(f"Errors found: {joined_errors}")


# ==========================
# Helper functions
# ==========================


def example_recommended_bundle():
    """This is a sample bundle for testing the cluster consistency for coordinated workers.

    This output is based on a subset of the output of tempo
    test-self-monitoring-distributed-tls model.
    """
    return yaml.safe_load("""
bundle: kubernetes
applications:
  tempo:
    charm: tempo-coordinator-k8s
    scale: 1
    constraints: arch=amd64
  tempo-worker-compactor:
    charm: tempo-worker-k8s
    scale: 1
    options:
      role-all: false
      role-compactor: true
    constraints: arch=amd64
  tempo-worker-distributor:
    charm: tempo-worker-k8s
    scale: 1
    options:
      role-all: false
      role-distributor: true
    constraints: arch=amd64
  tempo-worker-ingester:
    charm: tempo-worker-k8s
    scale: 3
    options:
      role-all: false
      role-ingester: true
  tempo-worker-metrics-generator:
    charm: tempo-worker-k8s
    scale: 1
    options:
      role-all: false
      role-metrics-generator: true
  tempo-worker-querier:
    charm: tempo-worker-k8s
    scale: 1
    options:
      role-all: false
      role-querier: true
  tempo-worker-query-frontend:
    charm: tempo-worker-k8s
    scale: 1
    options:
      role-all: false
      role-query-frontend: true
""")


def example_degraded_bundle():
    """This is a sample bundle for testing the cluster consistency for coordinated workers.

    This is intentionally failing.
    """
    return yaml.safe_load("""
bundle: kubernetes
applications:
  tempo:
    charm: tempo-coordinator-k8s
    scale: 1
    constraints: arch=amd64
  tempo-worker-compactor:
    charm: tempo-worker-k8s
    scale: 1
    options:
      role-all: false
      role-compactor: true
    constraints: arch=amd64
  tempo-worker-distributor:
    charm: tempo-worker-k8s
    scale: 1
    options:
      role-all: false
      role-distributor: true
    constraints: arch=amd64
  tempo-worker-ingester:
    charm: tempo-worker-k8s
    scale: 1
    options:
      role-all: false
      role-ingester: true
  tempo-worker-metrics-generator:
    charm: tempo-worker-k8s
    scale: 1
    options:
      role-all: false
      role-metrics-generator: true
  tempo-worker-querier:
    charm: tempo-worker-k8s
    scale: 1
    options:
      role-all: false
      role-querier: true
  tempo-worker-query-frontend:
    charm: tempo-worker-k8s
    scale: 1
    options:
      role-all: false
      role-query-frontend: true
""")


def example_recommended_deployment():
    """Doctest input."""
    return {
        "querier": 1,
        "query-frontend": 1,
        "ingester": 3,
        "distributor": 1,
        "compactor": 1,
        "metrics-generator": 1,
    }
