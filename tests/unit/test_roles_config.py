import pytest

from coordinated_workers.coordinator import ClusterRolesConfig, ClusterRolesConfigError


def test_meta_role_keys_not_in_roles():
    """Meta roles keys must be a subset of roles."""
    # WHEN `meta_roles` has a key that is not specified in `roles`
    # THEN instantiation raises a ClusterRolesConfigError
    with pytest.raises(ClusterRolesConfigError, match="meta keys") as exc_info:
        ClusterRolesConfig(
            roles={"read"},
            meta_roles={"I AM NOT A SUBSET OF ROLES": {"read"}},
            minimal_deployment={"read"},
        )
    assert "meta keys" in str(exc_info.value)
    assert "not a subset of" in str(exc_info.value)


def test_meta_role_values_not_in_roles():
    """Meta roles values must be a subset of roles."""
    # WHEN `meta_roles` has a value that is not specified in `roles`
    # THEN instantiation raises a ClusterRolesConfigError
    with pytest.raises(ClusterRolesConfigError, match="meta values") as exc_info:
        ClusterRolesConfig(
            roles={"read"},
            meta_roles={"read": {"I AM NOT A SUBSET OF ROLES"}},
            minimal_deployment={"read"},
        )
    assert "meta values" in str(exc_info.value)
    assert "not a subset of" in str(exc_info.value)


def test_minimal_deployment_roles_not_in_roles():
    """Minimal deployment roles must be a subset of roles."""
    # WHEN `minimal_deployment` has a value that is not specified in `roles`
    # THEN instantiation raises a ClusterRolesConfigError
    with pytest.raises(ClusterRolesConfigError, match="minimal deployment") as exc_info:
        ClusterRolesConfig(
            roles={"read"},
            meta_roles={"read": {"read"}},
            minimal_deployment={"I AM NOT A SUBSET OF ROLES"},
        )
    assert "minimal deployment" in str(exc_info.value)
    assert "not a subset of" in str(exc_info.value)
