# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helper utils for coordinated-workers."""

import importlib
from typing import List


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
