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
