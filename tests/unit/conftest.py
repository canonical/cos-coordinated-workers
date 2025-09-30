from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
import tenacity


@pytest.fixture(autouse=True)
def patch_all(tmp_path: Path):
    with ExitStack() as stack:
        # so we don't have to wait for minutes:
        stack.enter_context(
            patch(
                "coordinated_workers.worker.Worker.SERVICE_START_RETRY_WAIT",
                new=tenacity.wait_none(),
            )
        )
        stack.enter_context(
            patch(
                "coordinated_workers.worker.Worker.SERVICE_START_RETRY_STOP",
                new=tenacity.stop_after_delay(1),
            )
        )
        stack.enter_context(
            patch(
                "coordinated_workers.worker.Worker.SERVICE_STATUS_UP_RETRY_WAIT",
                new=tenacity.wait_none(),
            )
        )
        stack.enter_context(
            patch(
                "coordinated_workers.worker.Worker.SERVICE_STATUS_UP_RETRY_STOP",
                new=tenacity.stop_after_delay(1),
            )
        )

        # Prevent the worker's _update_tls_certificates method to try and write our local filesystem
        stack.enter_context(
            patch("coordinated_workers.worker.ROOT_CA_CERT", new=tmp_path / "rootcacert")
        )
        stack.enter_context(
            patch(
                "coordinated_workers.worker.ROOT_CA_CERT_PATH",
                new=Path(tmp_path / "rootcacert"),
            )
        )

        stack.enter_context(
            patch(
                "coordinated_workers.coordinator.CONSOLIDATED_METRICS_ALERT_RULES_PATH",
                new=tmp_path / "consolidated_metrics_rules",
            )
        )

        stack.enter_context(
            patch(
                "coordinated_workers.coordinator.CONSOLIDATED_LOGS_ALERT_RULES_PATH",
                new=tmp_path / "consolidated_logs_rules",
            )
        )

        yield


@pytest.fixture(autouse=True)
def mock_lightkube_client(request):
    """Global mock for lightkube client to avoid lightkube calls in both Worker and Coordinator."""
    # Skip this fixture if the test has explicitly disabled it.
    # To use this feature in a test, mark it with @pytest.mark.disable_lightkube_client_autouse
    if "disable_lightkube_client_autouse" in request.keywords:
        yield
    else:
        with ExitStack() as stack:
            worker_mock = stack.enter_context(patch("coordinated_workers.worker.Client"))
            coordinator_mock = stack.enter_context(patch("coordinated_workers.coordinator.Client"))
            yield {"worker": worker_mock, "coordinator": coordinator_mock}


@pytest.fixture(autouse=True)
def mock_reconcile_charm_labels(request):
    """Global mock for reconcile_charm_labels to avoid lightkube calls in both Worker and Coordinator."""
    # Skip this fixture if the test has explicitly disabled it.
    # To use this feature in a test, mark it with @pytest.mark.disable_reconcile_charm_labels_autouse
    if "disable_reconcile_charm_labels_autouse" in request.keywords:
        yield
    else:
        with ExitStack() as stack:
            worker_mock = stack.enter_context(patch("coordinated_workers.worker.reconcile_charm_labels"))
            coordinator_mock = stack.enter_context(patch("coordinated_workers.coordinator.reconcile_charm_labels"))
            yield {"worker": worker_mock, "coordinator": coordinator_mock}
