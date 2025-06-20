import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import ops
import pytest
import yaml
from ops import BlockedStatus, testing
from scenario import Secret
from scenario.context import CharmEvents
from scenario.errors import UncaughtCharmError

from coordinated_workers.worker import (
    CERT_FILE,
    CLIENT_CA_FILE,
    CONFIG_FILE,
    KEY_FILE,
    S3_TLS_CA_CHAIN_FILE,
    Worker,
)
from tests.unit.test_worker_status import k8s_patch


class MyCharm(ops.CharmBase):
    layer = ops.pebble.Layer("")

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.worker = Worker(
            self,
            "foo",
            lambda _: self.layer,
            {"cluster": "cluster"},
            readiness_check_endpoint="http://localhost:3200/ready",
        )


def test_no_roles_error():
    # Test that a charm that defines NO 'role-x' config options, when run,
    # raises a WorkerError

    # WHEN you define a charm with no role-x config options
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={},
    )

    # IF the charm executes any event
    # THEN the charm raises an error
    with pytest.raises(testing.errors.UncaughtCharmError):
        ctx.run(ctx.on.update_status(), testing.State(containers={testing.Container("foo")}))


@pytest.mark.parametrize(
    "roles_active, roles_inactive, expected",
    (
        (
            ["read", "write", "ingester", "all"],
            ["alertmanager"],
            ["read", "write", "ingester", "all"],
        ),
        (["read", "write"], ["alertmanager"], ["read", "write"]),
        (["read"], ["alertmanager", "write", "ingester", "all"], ["read"]),
        ([], ["read", "write", "ingester", "all", "alertmanager"], []),
    ),
)
def test_roles_from_config(roles_active, roles_inactive, expected):
    # Test that a charm that defines any 'role-x' config options, when run,
    # correctly determines which ones are enabled through the Worker

    # WHEN you define a charm with a few role-x config options
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={
            "options": {
                f"role-{r}": {"type": "boolean", "default": "false"}
                for r in (roles_active + roles_inactive)
            }
        },
    )

    # AND the charm runs with a few of those set to true, the rest to false
    with ctx(
        ctx.on.update_status(),
        testing.State(
            containers={testing.Container("foo")},
            config={
                **{f"role-{r}": False for r in roles_inactive},
                **{f"role-{r}": True for r in roles_active},
            },
        ),
    ) as mgr:
        # THEN the Worker.roles method correctly returns the list of only those that are set to true
        assert set(mgr.charm.worker.roles) == set(expected)


@patch.object(Worker, "is_ready", new=lambda _: True)
def test_worker_restarts_if_some_service_not_up(tmp_path):
    # GIVEN a worker with some services
    MyCharm.layer = ops.pebble.Layer(
        {
            "services": {
                "foo": {
                    "summary": "foos all the things",
                    "description": "bar",
                    "startup": "enabled",
                    "override": "merge",
                    "command": "ls -la",
                },
                "bar": {
                    "summary": "bars the foos",
                    "description": "bar",
                    "startup": "enabled",
                    "command": "exit 1",
                },
                "baz": {
                    "summary": "bazzes all of the bars",
                    "description": "bar",
                    "startup": "enabled",
                    "command": "echo hi",
                },
            }
        }
    )
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    # WHEN the charm receives any event and there are no changes to the config or the layer,
    #  but some of the services are down
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("some: yaml")
    container = testing.Container(
        "foo",
        can_connect=True,
        mounts={"local": testing.Mount(location=CONFIG_FILE, source=cfg)},
        execs={
            testing.Exec(("update-ca-certificates", "--fresh")),
            testing.Exec(("/bin/foo", "-version"), stdout="foo"),
        },
        service_statuses={
            "foo": ops.pebble.ServiceStatus.INACTIVE,
            "bar": ops.pebble.ServiceStatus.ACTIVE,
            "baz": ops.pebble.ServiceStatus.INACTIVE,
        },
    )
    state_out = ctx.run(ctx.on.pebble_ready(container), testing.State(containers={container}))

    # THEN the charm restarts all the services that are down
    container_out = state_out.get_container("foo")
    service_statuses = container_out.service_statuses.values()
    assert all(svc is ops.pebble.ServiceStatus.ACTIVE for svc in service_statuses), [
        stat.value for stat in service_statuses
    ]


@patch.object(Worker, "is_ready", new=lambda _: True)
def test_worker_does_not_restart_external_services(tmp_path):
    # GIVEN a worker with some services and a layer with some other services
    MyCharm.layer = ops.pebble.Layer(
        {
            "services": {
                "foo": {
                    "summary": "foos all the things",
                    "override": "merge",
                    "description": "bar",
                    "startup": "enabled",
                    "command": "ls -la",
                }
            }
        }
    )
    other_layer = ops.pebble.Layer(
        {
            "services": {
                "bar": {
                    "summary": "bars the foos",
                    "description": "bar",
                    "startup": "enabled",
                    "command": "exit 1",
                },
                "baz": {
                    "summary": "bazzes all of the bars",
                    "description": "bar",
                    "startup": "enabled",
                    "command": "echo hi",
                },
            }
        }
    )

    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    # WHEN the charm receives any event and there are no changes to the config or the layer,
    #  but some of the services are down
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("some: yaml")
    container = testing.Container(
        "foo",
        execs={
            testing.Exec(("update-ca-certificates", "--fresh")),
            testing.Exec(("/bin/foo", "-version"), stdout="foo"),
        },
        can_connect=True,
        mounts={"local": testing.Mount(location=CONFIG_FILE, source=cfg)},
        layers={"foo": MyCharm.layer, "bar": other_layer},
        service_statuses={
            # layer foo has some inactive
            "foo": ops.pebble.ServiceStatus.INACTIVE,
            # layer bar has some inactive
            "bar": ops.pebble.ServiceStatus.ACTIVE,
            "baz": ops.pebble.ServiceStatus.INACTIVE,
        },
    )
    state_out = ctx.run(ctx.on.pebble_ready(container), testing.State(containers={container}))

    # THEN the charm restarts all the services that are down
    container_out = state_out.get_container("foo")
    assert container_out.service_statuses == {
        # layer foo service is now active
        "foo": ops.pebble.ServiceStatus.ACTIVE,
        # layer bar services is unchanged
        "bar": ops.pebble.ServiceStatus.ACTIVE,
        "baz": ops.pebble.ServiceStatus.INACTIVE,
    }


def test_worker_raises_if_service_restart_fails_for_too_long(tmp_path):
    # GIVEN a worker with some services
    MyCharm.layer = ops.pebble.Layer(
        {
            "services": {
                "foo": {
                    "summary": "foos all the things",
                    "description": "bar",
                    "startup": "enabled",
                    "command": "ls -la",
                },
            }
        }
    )
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("some: yaml")
    container = testing.Container(
        "foo",
        can_connect=True,
        mounts={"local": testing.Mount(location=CONFIG_FILE, source=cfg)},
        service_statuses={
            "foo": ops.pebble.ServiceStatus.INACTIVE,
        },
    )

    def raise_change_error(*args):
        raise ops.pebble.ChangeError("something", MagicMock())

    with ExitStack() as stack:
        # WHEN service restart fails
        stack.enter_context(patch("ops.model.Container.restart", new=raise_change_error))

        # THEN the charm errors out
        # technically an ops.pebble.ChangeError but the context manager doesn't catch it for some reason
        stack.enter_context(pytest.raises(Exception))
        ctx.run(ctx.on.pebble_ready(container), testing.State(containers={container}))


@pytest.mark.parametrize(
    "remote_databag, expected",
    (
        (
            {
                "remote_write_endpoints": json.dumps([{"url": "test-url.com"}]),
                "worker_config": json.dumps("test"),
            },
            [{"url": "test-url.com"}],
        ),
        ({"remote_write_endpoints": json.dumps(None), "worker_config": json.dumps("test")}, []),
        (
            {
                "remote_write_endpoints": json.dumps(
                    [{"url": "test-url.com"}, {"url": "test2-url.com"}]
                ),
                "worker_config": json.dumps("test"),
            },
            [{"url": "test-url.com"}, {"url": "test2-url.com"}],
        ),
    ),
)
def test_get_remote_write_endpoints(remote_databag, expected):
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    container = testing.Container(
        "foo",
        execs={testing.Exec(("update-ca-certificates", "--fresh"))},
        can_connect=True,
    )
    relation = testing.Relation(
        "cluster",
        remote_app_data=remote_databag,
    )
    with ctx(
        ctx.on.relation_changed(relation),
        testing.State(containers={container}, relations={relation}),
    ) as mgr:
        charm = mgr.charm
        mgr.run()
        assert charm.worker.cluster.get_remote_write_endpoints() == expected


@patch.object(Worker, "is_ready", new=lambda _: True)
@pytest.mark.parametrize(
    "ports",
    ([10, 11, 42], [2, 1232]),
)
def test_worker_ports(ports):
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    relation = testing.Relation(
        "cluster",
        remote_app_data={
            "worker_config": json.dumps(yaml.safe_dump({"testing": "config"})),
            "worker_ports": json.dumps(ports),
        },
    )
    with patch("ops.model.Unit.set_ports") as set_ports_patch:
        ctx.run(
            ctx.on.relation_changed(relation),
            testing.State(
                containers={testing.Container("foo", can_connect=True)}, relations={relation}
            ),
        )
        set_ports_patch.assert_called_with(*ports)


def test_config_preprocessor():
    # GIVEN a charm with a config preprocessor
    new_config = {"modified": "config"}

    class MyWorker(Worker):
        @property
        def _worker_config(self):
            # mock config processor that entirely replaces the config with another,
            # normally one would call super and manipulate
            return new_config

    class MyCharm(ops.CharmBase):
        layer = ops.pebble.Layer({"services": {"foo": {"command": ["bar"]}}})

        def __init__(self, framework: ops.Framework):
            super().__init__(framework)
            self.worker = MyWorker(
                self,
                "foo",
                lambda _: self.layer,
                {"cluster": "cluster"},
            )

    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={
            "options": {
                "role-all": {"type": "boolean", "default": "true"},
                "role-none": {"type": "boolean", "default": "false"},
            }
        },
    )

    # WHEN the charm writes the config to disk
    state_out = ctx.run(
        ctx.on.config_changed(),
        testing.State(
            config={"role-all": True},
            containers={
                testing.Container(
                    "foo",
                    can_connect=True,
                    execs={testing.Exec(("update-ca-certificates", "--fresh"))},
                )
            },
            relations={
                testing.Relation(
                    "cluster",
                    remote_app_data={
                        "worker_config": json.dumps(yaml.safe_dump({"original": "config"}))
                    },
                )
            },
        ),
    )

    # THEN the data gets preprocessed
    fs = Path(str(state_out.get_container("foo").get_filesystem(ctx)) + CONFIG_FILE)
    assert fs.read_text() == yaml.safe_dump(new_config)


@patch.object(Worker, "_update_worker_config", MagicMock(return_value=False))
@patch.object(Worker, "_set_pebble_layer", MagicMock(return_value=False))
@patch.object(Worker, "restart")
def test_worker_does_not_restart(restart_mock, tmp_path):
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    relation = testing.Relation(
        "cluster",
        remote_app_data={
            "worker_config": json.dumps("some: yaml"),
        },
    )
    # WHEN the charm receives any event and there are no changes to the config or the layer,
    #  but some of the services are down
    container = testing.Container(
        "foo",
        can_connect=True,
    )
    ctx.run(ctx.on.update_status(), testing.State(containers={container}, relations={relation}))

    assert not restart_mock.called


@patch.object(Worker, "_update_worker_config", MagicMock(return_value=False))
@patch.object(Worker, "_set_pebble_layer", MagicMock(return_value=False))
@patch.object(Worker, "restart")
def test_worker_does_not_restart_on_no_cert_changed(restart_mock, tmp_path):
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    secret = testing.Secret(
        {"private-key": "private"},
        label="private_id",
        owner="app",
    )
    relation = testing.Relation(
        "cluster",
        remote_app_data={
            "worker_config": json.dumps("some: yaml"),
            "ca_cert": json.dumps("ca"),
            "server_cert": json.dumps("cert"),
            "privkey_secret_id": json.dumps(secret.id),
            "s3_tls_ca_chain": json.dumps("s3_ca"),
        },
    )

    cert = tmp_path / "cert.cert"
    key = tmp_path / "key.key"
    client_ca = tmp_path / "client_ca.cert"
    s3_ca_chain = tmp_path / "s3_ca_chain.cert"
    root_ca_mocked_path = tmp_path / "rootcacert"

    cert.write_text("cert")
    key.write_text("private")
    client_ca.write_text("ca")
    s3_ca_chain.write_text("s3_ca")

    container = testing.Container(
        "foo",
        can_connect=True,
        execs={testing.Exec(("update-ca-certificates", "--fresh"))},
        mounts={
            "cert": testing.Mount(location=CERT_FILE, source=cert),
            "key": testing.Mount(location=KEY_FILE, source=key),
            "client_ca": testing.Mount(location=CLIENT_CA_FILE, source=client_ca),
            "s3_ca_chain": testing.Mount(location=S3_TLS_CA_CHAIN_FILE, source=s3_ca_chain),
            "root_ca": testing.Mount(location=root_ca_mocked_path, source=client_ca),
        },
    )

    ctx.run(
        ctx.on.update_status(),
        testing.State(leader=True, containers={container}, relations={relation}, secrets={secret}),
    )

    assert restart_mock.call_count == 0


@k8s_patch(is_ready=False)
@patch.object(Worker, "_update_config")
def test_worker_no_reconcile_when_patch_not_ready(_update_config_mock):
    class MyCharmWithResources(ops.CharmBase):
        layer = ops.pebble.Layer("")

        def __init__(self, framework: ops.Framework):
            super().__init__(framework)
            self.worker = Worker(
                self,
                "foo",
                lambda _: self.layer,
                {"cluster": "cluster"},
                readiness_check_endpoint="http://localhost:3200/ready",
                resources_requests=lambda _: {"cpu": "50m", "memory": "50Mi"},
                container_name="charm",
            )

    ctx = testing.Context(
        MyCharmWithResources,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )

    ctx.run(
        ctx.on.update_status(),
        testing.State(leader=True, containers={testing.Container("foo")}),
    )

    assert not _update_config_mock.called


@patch.object(Worker, "_update_worker_config", MagicMock(return_value=False))
@patch.object(Worker, "_set_pebble_layer", MagicMock(return_value=False))
@patch.object(Worker, "restart")
def test_worker_certs_update(restart_mock, tmp_path):
    # GIVEN a worker with no cert files on disk, and a cluster relation giving us some cert data
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    secret = testing.Secret(
        {"private-key": "private"},
        label="private_id",
        owner="app",
    )
    relation = testing.Relation(
        "cluster",
        remote_app_data={
            "worker_config": json.dumps("some: yaml"),
            "ca_cert": json.dumps("ca"),
            "server_cert": json.dumps("cert"),
            "privkey_secret_id": json.dumps(secret.id),
            "s3_tls_ca_chain": json.dumps("s3_ca"),
        },
    )

    cert = tmp_path / "cert.cert"
    key = tmp_path / "key.key"
    client_ca = tmp_path / "client_ca.cert"
    s3_ca_chain = tmp_path / "s3_ca_chain.cert"

    container = testing.Container(
        "foo",
        can_connect=True,
        execs={testing.Exec(("update-ca-certificates", "--fresh"))},
        mounts={
            "cert": testing.Mount(location=CERT_FILE, source=cert),
            "key": testing.Mount(location=KEY_FILE, source=key),
            "client_ca": testing.Mount(location=CLIENT_CA_FILE, source=client_ca),
            "s3_ca_chain": testing.Mount(location=S3_TLS_CA_CHAIN_FILE, source=s3_ca_chain),
        },
    )

    # WHEN the charm receives any event
    ctx.run(
        ctx.on.update_status(),
        testing.State(leader=True, containers={container}, relations={relation}, secrets={secret}),
    )

    # THEN the worker writes all tls data to the right locations on the container filesystem
    assert cert.read_text() == "cert"
    assert key.read_text() == "private"
    assert client_ca.read_text() == "ca"
    assert s3_ca_chain.read_text() == "s3_ca"

    # AND the worker restarts the workload
    assert restart_mock.call_count == 1


@patch.object(Worker, "_update_worker_config", MagicMock(return_value=False))
@patch.object(Worker, "_set_pebble_layer", MagicMock(return_value=False))
@patch.object(Worker, "restart")
@pytest.mark.parametrize("s3_ca_on_disk", (True, False))
def test_worker_certs_update_only_s3(restart_mock, tmp_path, s3_ca_on_disk):
    # GIVEN a worker with a tls-encrypted s3 bucket
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    relation = testing.Relation(
        "cluster",
        remote_app_data={
            "worker_config": json.dumps("some: yaml"),
            "s3_tls_ca_chain": json.dumps("s3_ca"),
        },
    )

    cert = tmp_path / "cert.cert"
    key = tmp_path / "key.key"
    client_ca = tmp_path / "client_ca.cert"
    s3_ca_chain = tmp_path / "s3_ca_chain.cert"
    if s3_ca_on_disk:
        s3_ca_chain.write_text("s3_ca")

    container = testing.Container(
        "foo",
        can_connect=True,
        execs={testing.Exec(("update-ca-certificates", "--fresh"))},
        mounts={
            "cert": testing.Mount(location=CERT_FILE, source=cert),
            "key": testing.Mount(location=KEY_FILE, source=key),
            "client_ca": testing.Mount(location=CLIENT_CA_FILE, source=client_ca),
            "s3_ca_chain": testing.Mount(location=S3_TLS_CA_CHAIN_FILE, source=s3_ca_chain),
        },
    )

    # WHEN the charm receives any event
    ctx.run(
        ctx.on.update_status(),
        testing.State(leader=True, containers={container}, relations={relation}),
    )

    # THEN the worker writes all tls data to the right locations on the container filesystem
    assert not cert.exists()
    assert not key.exists()
    assert not client_ca.exists()
    assert s3_ca_chain.read_text() == "s3_ca"

    # AND the worker restarts the workload IF it was not on disk already
    assert restart_mock.call_count == (0 if s3_ca_on_disk else 1)


@patch.object(Worker, "restart")
@patch.object(Worker, "stop")
@pytest.mark.parametrize("tls", (True, False))
def test_stop_called_on_no_cluster(stop_mock, restart_mock, tmp_path, tls):
    # GIVEN a worker who's all happy to begin with
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    cert = tmp_path / "cert.cert"
    key = tmp_path / "key.key"
    client_ca = tmp_path / "client_ca.cert"
    s3_ca_chain = tmp_path / "s3_ca_chain.cert"

    if tls:
        s3_ca_chain.write_text("something_tls")
        cert.write_text("something_tls")
        key.write_text("something_tls")
        client_ca.write_text("something_tls")

    container = testing.Container(
        "foo",
        can_connect=True,
        execs={testing.Exec(("update-ca-certificates", "--fresh"))},
        mounts={
            "cert": testing.Mount(location=CERT_FILE, source=cert),
            "key": testing.Mount(location=KEY_FILE, source=key),
            "client_ca": testing.Mount(location=CLIENT_CA_FILE, source=client_ca),
            "s3_ca_chain": testing.Mount(location=S3_TLS_CA_CHAIN_FILE, source=s3_ca_chain),
        },
    )

    # WHEN the charm receives any event
    ctx.run(
        ctx.on.update_status(),
        testing.State(leader=True, containers={container}),
    )

    fs = container.get_filesystem(ctx)
    # THEN the worker wipes all certificates if they are there
    assert not fs.joinpath(CERT_FILE).exists()
    assert not fs.joinpath(KEY_FILE).exists()
    assert not fs.joinpath(CLIENT_CA_FILE).exists()
    assert not fs.joinpath(S3_TLS_CA_CHAIN_FILE).exists()

    # AND the worker stops the workload instead of restarting it
    assert not restart_mock.called
    assert stop_mock.called


@patch.object(Worker, "is_ready", new=lambda _: False)
def test_worker_stop_all_services_if_not_ready(tmp_path):
    # GIVEN a worker with some services
    MyCharm.layer = ops.pebble.Layer(
        {
            "services": {
                "foo": {
                    "summary": "foos all the things",
                    "description": "bar",
                    "startup": "enabled",
                    "override": "merge",
                    "command": "ls -la",
                },
                "bar": {
                    "summary": "bars the foos",
                    "description": "bar",
                    "startup": "enabled",
                    "command": "exit 1",
                },
                "baz": {
                    "summary": "bazzes all of the bars",
                    "description": "bar",
                    "startup": "enabled",
                    "command": "echo hi",
                },
            }
        }
    )
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    # WHEN the charm receives any event, but it is not ready
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("some: yaml")
    container = testing.Container(
        "foo",
        layers={"base": MyCharm.layer},
        can_connect=True,
        mounts={"local": testing.Mount(location=CONFIG_FILE, source=cfg)},
        execs={
            testing.Exec(("update-ca-certificates", "--fresh")),
            testing.Exec(("/bin/foo", "-version"), stdout="foo"),
        },
        service_statuses={
            "foo": ops.pebble.ServiceStatus.ACTIVE,
            "bar": ops.pebble.ServiceStatus.ACTIVE,
            "baz": ops.pebble.ServiceStatus.INACTIVE,
        },
    )
    state_out = ctx.run(ctx.on.pebble_ready(container), testing.State(containers={container}))

    # THEN the charm restarts all the services that are down
    container_out = state_out.get_container("foo")
    service_statuses = container_out.service_statuses.values()
    assert all(svc is ops.pebble.ServiceStatus.INACTIVE for svc in service_statuses), [
        stat.value for stat in service_statuses
    ]


@patch("socket.getfqdn")
def test_invalid_url(mock_socket_fqdn):
    # Test that when socket returns an invalid url as a Fully Qualified Domain Name,
    #   ClusterRequirer.publish_unit_address raises a ValueError exception

    # GIVEN a properly configured charm
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )

    # AND ClusterRequirer is passed an invalid url as FQDN
    mock_socket_fqdn.return_value = "http://www.invalid-]url.com"

    # WHEN the charm executes any event
    # THEN the charm raises an error with the appropriate cause
    with pytest.raises(UncaughtCharmError) as exc:
        ctx.run(ctx.on.update_status(), testing.State(containers={testing.Container("foo")}))
    assert isinstance(exc.value.__cause__, ValueError)


@pytest.mark.parametrize(
    "remote_databag, expected",
    (
        (
            {
                "charm_tracing_receivers": json.dumps({"url": "test-url.com"}),
                "worker_config": json.dumps("test"),
            },
            {"url": "test-url.com"},
        ),
        (
            {"charm_tracing_receivers": json.dumps(None), "worker_config": json.dumps("test")},
            {},
        ),
    ),
)
def test_get_charm_tracing_receivers(remote_databag, expected):
    MyCharm.layer = ops.pebble.Layer(
        {
            "services": {
                "foo": {
                    "summary": "foos all the things",
                    "description": "bar",
                    "startup": "enabled",
                    "override": "merge",
                    "command": "ls -la",
                }
            }
        }
    )

    # Test that when a relation changes the correct charm_tracing_receivers
    #   are returned by the ClusterRequirer

    # GIVEN a charm with a relation
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    container = testing.Container(
        "foo",
        execs={testing.Exec(("update-ca-certificates", "--fresh"))},
        can_connect=True,
    )

    relation = testing.Relation(
        "cluster",
        remote_app_data=remote_databag,
    )

    # WHEN the relation changes
    with ctx(
        ctx.on.relation_changed(relation),
        testing.State(containers={container}, relations={relation}),
    ) as mgr:
        charm = mgr.charm
        # THEN the charm tracing receivers are picked up correctly
        assert charm.worker.cluster.get_charm_tracing_receivers() == expected


@pytest.mark.parametrize("tls", (False, True))
def test_charm_tracing_config(tls):
    MyCharm.layer = ops.pebble.Layer(
        {
            "services": {
                "foo": {
                    "summary": "foos all the things",
                    "description": "bar",
                    "startup": "enabled",
                    "override": "merge",
                    "command": "ls -la",
                }
            }
        }
    )
    # GIVEN a charm with a cluster relation (with or without TLS data in it)
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    container = testing.Container(
        "foo",
        execs={testing.Exec(("update-ca-certificates", "--fresh"))},
        can_connect=True,
    )
    mock_certs_data = json.dumps("<TLS_STUFF>")

    secret = Secret({"private-key": "verysecret"})
    tls_data = (
        {
            "ca_cert": mock_certs_data,
            "server_cert": mock_certs_data,
            "privkey_secret_id": json.dumps(secret.id),
        }
        if tls
        else {}
    )
    relation = testing.Relation(
        "cluster",
        remote_app_data={
            "charm_tracing_receivers": json.dumps(
                {"otlp_http": f"http{'s' if tls else ''}://some-url"}
            ),
            "worker_config": json.dumps("test"),
            **tls_data,
        },
    )

    # WHEN any event occurs
    with patch("ops_tracing.set_destination") as p:
        ctx.run(
            ctx.on.update_status(),
            testing.State(
                containers={container}, secrets={secret} if tls else {}, relations={relation}
            ),
        )

    # THEN set_destination gets called with the expected data
    p.assert_called_with(
        url=f"http{'s' if tls else ''}://some-url/v1/traces", ca="<TLS_STUFF>" if tls else None
    )


@pytest.mark.parametrize(
    "remote_databag, expected",
    (
        (
            {
                "workload_tracing_receivers": json.dumps({"url": "test-url.com"}),
                "worker_config": json.dumps("test"),
            },
            {"url": "test-url.com"},
        ),
        (
            {"workload_tracing_receivers": json.dumps(None), "worker_config": json.dumps("test")},
            {},
        ),
    ),
)
def test_get_workload_tracing_receivers(remote_databag, expected):
    # Test that when a relation changes the correct workload_tracing_receivers
    #   are returned by the ClusterRequirer

    # GIVEN a charm with a relation
    ctx = testing.Context(
        MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    container = testing.Container(
        "foo",
        execs={testing.Exec(("update-ca-certificates", "--fresh"))},
        can_connect=True,
    )

    relation = testing.Relation(
        "cluster",
        remote_app_data=remote_databag,
    )

    # WHEN the relation changes
    with ctx(
        ctx.on.relation_changed(relation),
        testing.State(containers={container}, relations={relation}),
    ) as mgr:
        charm = mgr.charm
        # THEN the charm tracing receivers are picked up correctly
        assert charm.worker.cluster.get_workload_tracing_receivers() == expected


@pytest.mark.parametrize(
    "event",
    (
        CharmEvents.update_status(),
        CharmEvents.start(),
        CharmEvents.install(),
        CharmEvents.relation_changed,
    ),
)
def test_worker_blocks_on_tls_misconfigured(event):
    class _MyCharm(MyCharm):
        layer = ops.pebble.Layer({"services": {"foo": {"command": ["bar"]}}})

    ctx = testing.Context(
        _MyCharm,
        meta={
            "name": "foo",
            "requires": {"cluster": {"interface": "cluster"}},
            "containers": {"foo": {"type": "oci-image"}},
        },
        config={"options": {"role-all": {"type": "boolean", "default": True}}},
    )
    container = testing.Container(
        "foo",
        execs={testing.Exec(("update-ca-certificates", "--fresh"))},
        can_connect=True,
    )

    # GIVEN the coordinator is giving us AT LEAST ONE https endpoint for charm_tracing
    #  OR workload tracing, but no cert
    relation = testing.Relation(
        "cluster",
        remote_app_data={
            "charm_tracing_receivers": json.dumps(
                {
                    "jaeger_thrift_http": "https://198.18.0.0:14268",
                    "otlp_http": "http://198.18.0.0:4318",
                }
            ),
            "workload_tracing_receivers": json.dumps(
                {
                    "jaeger_thrift_http": "http://198.18.0.0:14268",
                    "otlp_http": "http://198.18.0.0:4318",
                }
            ),
            "worker_config": json.dumps("test"),
        },
    )

    if callable(event):
        event = event(relation)

    # WHEN any event occurs
    state_out = ctx.run(
        event,
        testing.State(containers={container}, relations={relation}),
    )
    # THEN the charm sets blocked
    assert isinstance(state_out.unit_status, BlockedStatus)
    assert "TLS misconfigured" in state_out.unit_status.message
