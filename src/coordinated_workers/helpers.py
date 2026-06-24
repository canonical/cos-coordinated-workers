# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helper utils for coordinated-workers."""

import importlib
from typing import FrozenSet, List, Type

import ops

# Events that should not trigger reconciliation in either the Worker or the Coordinator.
# Pebble check events fire when a workload's readiness/liveness check fails or recovers.
# Reconciling on them offers no benefit and can lead to a restart loop when the workload is
# still starting up (e.g. replaying a WAL). Additionally, in a check-failed event the check
# may have already recovered by the time the handler runs, making the reconcile semantically
# incorrect. See https://github.com/canonical/cos-coordinated-workers/issues/159
NON_RECONCILABLE_EVENTS: FrozenSet[Type[ops.EventBase]] = frozenset(
    {
        ops.PebbleCheckFailedEvent,
        ops.PebbleCheckRecoveredEvent,
    }
)


def check_libs_installed(*path: str):
    """Attempt to import these charm libs and raise an error if it fails."""
    libs_not_found: List[str] = []
    for charm_lib_path in path:
        try:
            importlib.import_module(charm_lib_path)
        except ModuleNotFoundError:
            libs_not_found.append(charm_lib_path)

    if libs_not_found:
        install_script = "\n".join(f"charmcraft fetch-lib {libname}" for libname in libs_not_found)
        raise RuntimeError(
            f"Unmet dependencies: the coordinator charm base is missing some charm libs. \
            Please install them with: \n\n{install_script}"
        )
