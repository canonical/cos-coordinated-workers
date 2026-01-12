"""A collection of helper functions for tests."""
from contextlib import ExitStack, nullcontext
from unittest.mock import PropertyMock, patch

from coordinated_workers.coordinator import Coordinator, TLSConfig

MOCK_CERTS_DATA = "<TLS_STUFF>"
MOCK_TLS_CONFIG = TLSConfig(MOCK_CERTS_DATA, MOCK_CERTS_DATA, MOCK_CERTS_DATA)


def tls_mock(tmp_path, enabled=True):
    """A mock for TLS certs, i.e. coordinator.tls_config."""
    if not enabled:
        return nullcontext()

    stack = ExitStack()
    stack.enter_context(
        patch.object(
            Coordinator,
            "tls_config",
            new_callable=PropertyMock,
            return_value=MOCK_TLS_CONFIG,
        )
    )
    stack.enter_context(patch("coordinated_workers.nginx.CA_CERT_PATH", new=tmp_path / "rootcacert"))
    return stack
