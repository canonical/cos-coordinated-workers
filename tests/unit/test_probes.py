import shlex
import subprocess
from pathlib import Path

import pydantic
import pytest

from probes.cluster_consistency import bundle

RESOURCES = Path(__file__).parent / "resources"

RECOMMENDED_DEPLOYMENT = {
    "querier": 1,
    "query-frontend": 1,
    "ingester": 3,
    "distributor": 1,
    "compactor": 1,
    "metrics-generator": 1,
}
META_ROLES = {
    "all": [
        "querier",
        "query-frontend",
        "ingester",
        "distributor",
        "compactor",
        "metrics-generator",
    ]
}


def check_bundle(bndl, worker_charm):
    bundle(
        bundles={"test-bundle": bndl},
        worker_charm=worker_charm,
        recommended_deployment=RECOMMENDED_DEPLOYMENT,
        meta_roles=META_ROLES,
    )


@pytest.fixture(params=("tempo", "foobar-k8s"))
def worker_charm(request: pytest.FixtureRequest):
    return request.param


@pytest.fixture
def base_bundle(worker_charm):
    coordinator_name = "coordinator"
    worker_names = [f"mycharm-{role}" for role in RECOMMENDED_DEPLOYMENT]
    workers = {
        f"{worker_charm}-{role}": {
            "charm": worker_charm,
            "scale": 1 if role != "ingester" else 3,
            "options": {"role-all": False, f"role-{role}": True},
        }
        for role in RECOMMENDED_DEPLOYMENT
    }
    return {
        "bundle": "kubernetes",
        "applications": {
            coordinator_name: {"charm": "coordinator", "scale": 1, "options": {}},
            **workers,
        },
        "relations": [
            [f"{coordinator_name}:something-cluster", f"{worker}:something-cluster"]
            for worker in worker_names
        ],
    }


def test_good_bundle(base_bundle, worker_charm):
    check_bundle(base_bundle, worker_charm=worker_charm)


def test_bundle_less_ingesters(base_bundle, worker_charm):
    base_bundle["applications"][f"{worker_charm}-ingester"]["scale"] -= 1
    with pytest.raises(RuntimeError) as exc:
        check_bundle(base_bundle, worker_charm)
    assert exc.value.args[1] == [
        f"{worker_charm} deployment should be scaled up by 1 ingester units."
    ]


def test_bundle_missing_queriers(base_bundle, worker_charm):
    del base_bundle["applications"][f"{worker_charm}-querier"]
    with pytest.raises(RuntimeError) as exc:
        check_bundle(base_bundle, worker_charm)
    assert exc.value.args[1] == [f"{worker_charm} deployment is missing required role: querier"]


def test_bundle_all_roles(base_bundle, worker_charm):
    del base_bundle["applications"][f"{worker_charm}-querier"]
    del base_bundle["applications"][f"{worker_charm}-metrics-generator"]

    # replace with a scale-1 ALL worker
    worker = base_bundle["applications"].pop(f"{worker_charm}-query-frontend")
    worker["options"] = {"role-all": True}

    base_bundle["applications"][f"{worker_charm}-all"] = worker
    check_bundle(base_bundle, worker_charm)


def test_bundle_all_only(base_bundle, worker_charm):
    all_worker = base_bundle["applications"].pop(f"{worker_charm}-querier")
    # replace all applications with a single scale-3 ALL worker
    all_worker["scale"] = 3
    all_worker["options"] = {"role-all": True}

    base_bundle["applications"] = {f"{worker_charm}-all": all_worker}
    check_bundle(base_bundle, worker_charm)


def test_bundle_all_but_too_few(base_bundle, worker_charm):
    all_worker = base_bundle["applications"].pop(f"{worker_charm}-querier")
    # replace all applications with a single scale-2 ALL worker
    all_worker["scale"] = 2
    all_worker["options"] = {"role-all": True}

    base_bundle["applications"] = {f"{worker_charm}-all": all_worker}
    with pytest.raises(RuntimeError):
        check_bundle(base_bundle, worker_charm)


def test_meta_role_nonmatching():
    # this is valid: we're declaring a meta-role, but we're not using it
    bundle(
        bundles={
            "test-bundle": {
                "applications": {
                    "foo0": {"charm": "foo", "options": {"role-all": False, "role-a": True}},
                    "foo1": {"charm": "foo", "options": {"role-all": False, "role-b": True}},
                    "foo2": {"charm": "foo", "options": {"role-all": False, "role-b": True}},
                }
            }
        },
        worker_charm="foo",
        recommended_deployment={"a": 1, "b": 2},
        meta_roles={"c": ["a", "b"]},
    )


def test_meta_role_invalid():
    # this is invvalid: we're declaring a meta-role, but one of the roles it expands to is unknown
    with pytest.raises(
        pydantic.ValidationError,
        match="each meta_role must expand to a recommended_deployment role",
    ):
        bundle(
            bundles={
                "test-bundle": {
                    "applications": {
                        "foo0": {"charm": "foo", "options": {"role-all": False, "role-a": True}},
                        "foo1": {"charm": "foo", "options": {"role-all": False, "role-b": True}},
                        "foo2": {"charm": "foo", "options": {"role-all": False, "role-b": True}},
                    }
                }
            },
            worker_charm="foo",
            recommended_deployment={"a": 1, "b": 2},
            meta_roles={"c": ["a", "c"]},  # role c does not occur in recommended_deployment
        )


def test_charm_not_found_validation():
    with pytest.raises(RuntimeError, match="worker_charm 'bar' not found"):
        bundle(
            bundles={"test-bundle": {"applications": {"foo": {"charm": "foo"}}}},
            worker_charm="bar",
            recommended_deployment=RECOMMENDED_DEPLOYMENT,
            meta_roles=META_ROLES,
        )


def test_bundle_meta_roles():
    # one unit with "all" role, which means one per a,b,c role
    # two units with "meta1" role, which means +two per a,b role
    bndl = {
        "applications": {
            "foo": {
                "charm": "mycharm",
                "scale": 1,
                "options": {},  # implies: role-all=True
            },
            "bar": {
                "scale": 2,
                "charm": "mycharm",
                "options": {"role-meta1": True, "role-all": False},
            },
        }
    }
    # so we should be happy with this recommended deployment
    bundle(
        bundles={"test-bundle": bndl},  # type: ignore
        worker_charm="mycharm",
        recommended_deployment={"a": 3, "b": 3, "c": 1},
        meta_roles={"all": ["a", "b", "c"], "meta1": ["a", "b"]},
    )


def test_bundle_meta_roles_bad():
    # one unit with "all" role, which means one per a,b,c role
    # one unit with "meta1" role, which means +one per a,b role
    bndl = {
        "applications": {
            "foo": {
                "charm": "mycharm",
                "scale": 1,
                "options": {},  # implies: role-all=True
            },
            "bar": {
                "scale": 1,
                "charm": "mycharm",
                "options": {"role-meta1": True, "role-all": False},
            },
        }
    }
    # so we should be sad with this recommended deployment (need one more a)
    with pytest.raises(RuntimeError):
        bundle(
            bundles={"test-bundle": bndl},  # type: ignore
            worker_charm="mycharm",
            recommended_deployment={"a": 3, "b": 2, "c": 1},
            meta_roles={"all": ["a", "b", "c"], "meta1": ["a", "b"]},
        )


def test_ruleset():
    # this is the most end-to-end test we have: verify that the reusable probe works when
    # used with an actual bundle and a tempo-like ruleset.
    cmd = "juju-doctor check -p file://./resources/ruleset.yaml --bundle ./resources/bundle-reference.yaml -v"
    subprocess.run(shlex.split(cmd), check=True)
