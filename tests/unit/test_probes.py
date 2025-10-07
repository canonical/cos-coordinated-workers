import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

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
        "querier", "query-frontend", "ingester", "distributor", "compactor", "metrics-generator"
    ]
}


def check_bundle(bundle_yaml):
    bundle(
        bundles={"test-bundle": bundle_yaml},
        worker_charm="tempo-worker-k8s",
        recommended_deployment=RECOMMENDED_DEPLOYMENT,
        meta_roles=META_ROLES
    )


def test_good_bundle():
    bundle_yaml = yaml.safe_load((RESOURCES / "bundle-reference.yaml").read_text())
    check_bundle(bundle_yaml)


def test_bundle_less_ingesters():
    bundle_yaml = yaml.safe_load((RESOURCES / "bundle-reference.yaml").read_text())
    bundle_yaml["applications"]["tempo-worker-ingester"]["scale"] -= 1
    with pytest.raises(RuntimeError) as exc:
        check_bundle(bundle_yaml)
    assert exc.value.args[1] == [
        "tempo-worker-k8s deployment should be scaled up by 1 ingester units."
    ]


def test_bundle_missing_queriers():
    bundle_yaml = yaml.safe_load((RESOURCES / "bundle-reference.yaml").read_text())
    del bundle_yaml["applications"]["tempo-worker-querier"]
    with pytest.raises(RuntimeError) as exc:
        check_bundle(bundle_yaml)
    assert exc.value.args[1] == ["tempo-worker-k8s deployment is missing required role: querier"]


def test_bundle_all_roles():
    bundle_yaml = yaml.safe_load((RESOURCES / "bundle-reference.yaml").read_text())
    del bundle_yaml["applications"]["tempo-worker-querier"]
    del bundle_yaml["applications"]["tempo-worker-metrics-generator"]

    # replace with a scale-1 ALL worker
    worker = bundle_yaml["applications"].pop("tempo-worker-query-frontend")
    worker["options"] = {"role-all": True}

    bundle_yaml["applications"]["tempo-worker-all"] = worker
    check_bundle(bundle_yaml)


def test_bundle_all_only():
    bundle_yaml = yaml.safe_load((RESOURCES / "bundle-reference.yaml").read_text())
    all_worker = bundle_yaml["applications"].pop("tempo-worker-querier")
    # replace all applications with a single scale-3 ALL worker
    all_worker["scale"] = 3
    all_worker["options"] = {"role-all": True}

    bundle_yaml["applications"] = {"tempo-worker-all": all_worker}
    check_bundle(bundle_yaml)


def test_bundle_all_but_too_few():
    bundle_yaml = yaml.safe_load((RESOURCES / "bundle-reference.yaml").read_text())
    all_worker = bundle_yaml["applications"].pop("tempo-worker-querier")
    # replace all applications with a single scale-2 ALL worker
    all_worker["scale"] = 2
    all_worker["options"] = {"role-all": True}

    bundle_yaml["applications"] = {"tempo-worker-all": all_worker}
    with pytest.raises(RuntimeError):
        check_bundle(bundle_yaml)


def test_bundle_meta_roles():
    # one unit with "all" role, which means one per a,b,c role
    # two units with "meta1" role, which means +two per a,b role
    bndl = {
        "applications":{
            "foo": {
                "charm": "mycharm",
                "scale": 1,
                "options": {} # implies: role-all=True
            },
            "bar": {
                "scale": 2,
                "charm": "mycharm",
                "options": {"role-meta1": True, "role-all":False}
            },
        }}
    # so we should be happy with this recommended deployment
    bundle(
        bundles={"test-bundle": bndl},  # type: ignore
        worker_charm="mycharm",
        recommended_deployment={"a": 3, "b": 3, "c": 1},
        meta_roles={
            "all": ["a", "b", "c"],
            "meta1": ["a", "b"]
        },
    )

def test_bundle_meta_roles_bad():
    # one unit with "all" role, which means one per a,b,c role
    # one unit with "meta1" role, which means +one per a,b role
    bndl = {
        "applications":{
            "foo": {
                "charm": "mycharm",
                "scale": 1,
                "options": {} # implies: role-all=True
            },
            "bar": {
                "scale": 1,
                "charm": "mycharm",
                "options": {"role-meta1": True, "role-all":False}
            },
        }}
    # so we should be sad with this recommended deployment (need one more a)
    with pytest.raises(RuntimeError):
        bundle(
            bundles={"test-bundle": bndl},  # type: ignore
            worker_charm="mycharm",
            recommended_deployment={"a": 3, "b": 2, "c": 1},
            meta_roles={
                "all": ["a", "b", "c"],
                "meta1": ["a", "b"]
            },
        )


def test_ruleset():
    # this is the most end-to-end test we have: verify that the reusable probe works when
    # used with an actual bundle and a tempo-like ruleset.
    cmd = "juju-doctor check -p file://./resources/ruleset.yaml --bundle ./resources/bundle-reference.yaml -v"
    subprocess.run(shlex.split(cmd), check=True)
