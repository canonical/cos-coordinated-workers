#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Generic juju-doctor probe to test coordinated-workers deployments for consistency."""

from collections import Counter
from typing import Any, Dict, List, Sequence


def status(bundles, *args, **kwargs):
    """Verify the juju status report."""
    assert True


def bundle(
    bundles: Dict[str, Dict[str, Any]],
    *args,
    worker_charm: str,
    recommended_deployment: Dict[str, int],
    meta_roles: Dict[str, Sequence[str]] = None,
    **kwargs,
):
    """Verify the juju export-bundle report.

    Example usage::

        name: MyRuleSet - coordinated-workers deployment validator
        probes:
          - name: MyProbe
            type: scriptlet
            url: https://github.com/canonical/cos-coordinated-workers//probes/cluster_consistency.py
            with:
              - worker_charm: my-worker-k8s
                recommended_deployment:
                  querier: 1
                  query-frontend: 1
                  ingester: 3
                  distributor: 1
                  compactor: 1
                  metrics-generator: 1
                meta_roles:
                  read: [querier, query-frontend, ingester]
                  write: [distributor]
    """
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
                # ASSUME: no config means the 'all' role is enabled (and no other is) as that is the default
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

                    # expand meta roles
                    if meta_roles and role in meta_roles:
                        for _role in meta_roles[role]:
                            roles[_role] += scale
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
        raise RuntimeError(f"Errors found: {joined_errors}", errors)


def show_unit(bundles, *args, **kwargs):
    """Verify the juju show-unit report."""
    assert True
