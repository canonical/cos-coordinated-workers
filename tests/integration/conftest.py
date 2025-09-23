"""Pytest configuration and shared fixtures for integration tests.

For shared and importable helpers, use helpers.py instead.
"""

import functools
import logging
import os
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Union

import pytest
import yaml
from helpers import PackedCharm
from pytest_jubilant import pack

logger = logging.getLogger(__name__)
store = defaultdict(str)


REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
COORDINATED_WORKER_PACKAGE_SRC = REPO_ROOT / "src/coordinated_workers"


def copy_coordinated_worker_source(destination: Union[str, Path]):
    """Copy the coordinated worker package to the destination directory, deleting any existing destination files first.

    This is useful for any tester charms needing an up-to-date copy of the coordinated_workers package.
    """
    source = COORDINATED_WORKER_PACKAGE_SRC
    destination = Path(destination).resolve()

    logging.info(
        f"Copying coordinated_worker package from {source} to {destination}, deleting anything that exists at"
        f" destination first"
    )

    try:
        shutil.rmtree(destination)
    except FileNotFoundError:
        # destination didn't exist anyway
        pass

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Use dirs_exist_ok=False to ensure we don't copy over anything we didn't mean to.  But in practice, the above
    # code should have deleted anything that was there anyway.
    shutil.copytree(src=source, dst=destination, dirs_exist_ok=False)


def timed_memoizer(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        fname = func.__qualname__
        logger.info("Started: %s" % fname)
        start_time = datetime.now()
        if fname in store.keys():
            ret = store[fname]
        else:
            logger.info("Return for {} not cached".format(fname))
            ret = func(*args, **kwargs)
            store[fname] = ret
        logger.info("Finished: {} in: {} seconds".format(fname, datetime.now() - start_time))
        return ret

    return wrapper


def tester_charm_builder(tester_path: Path) -> PackedCharm:
    """Build a tester charm from the given path.

    The tester charm will have the coordinated_workers package copied into its src directory so it uses the latest
    unpublished version of the package.
    """
    if not tester_path.is_dir():
        raise ValueError(f"tester_path {tester_path} is not a directory")
    tester_charm_name = tester_path.name
    tester_coordinated_worker_source = tester_path / "src/coordinated_workers"
    charm_path_env_var = f"CHARM_PATH_{tester_charm_name.upper()}"

    if charm_file := os.environ.get(charm_path_env_var):
        logger.info(f"Using existing tester charm {tester_charm_name} from {charm_file}")
        charm = charm_file
    else:
        # Copy the coordinated_workers package into the tester charm so it uses the latest, unpublished version of the
        # package.  This is copied into the tester's `src` dir because that is in the PYTHONPATH by default ahead of
        # standard packages.  The charm code will use coordinated_worker imports from here instead of the regular
        # package.
        copy_coordinated_worker_source(destination=tester_coordinated_worker_source)
        logger.info(f"Packing tester charm {tester_charm_name} from {tester_path}")
        charm = pack(tester_path)

    resources = get_charm_resources(tester_path / "charmcraft.yaml")

    return PackedCharm(
        charm=str(charm),
        resources=resources,
    )


def get_charm_resources(charmcraft_file: Union[str, Path]) -> dict[str, str]:
    """Get the resources from a charmcraft.yaml file."""
    charmcraft_file_text = Path(charmcraft_file).read_text()
    resources = yaml.safe_load(charmcraft_file_text)["resources"]
    return {name: resource["upstream-source"] for name, resource in resources.items()}


@pytest.fixture(scope="module")
@timed_memoizer
def coordinator_charm() -> PackedCharm:
    return tester_charm_builder(REPO_ROOT / "tests/integration/testers/coordinator")


@pytest.fixture(scope="module")
@timed_memoizer
def worker_charm() -> PackedCharm:
    return tester_charm_builder(REPO_ROOT / "tests/integration/testers/worker")
