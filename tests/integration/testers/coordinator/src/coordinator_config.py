"""Coordinator configuration."""

from enum import StrEnum, unique

from coordinated_workers.coordinator import ClusterRolesConfig


@unique
class Role(StrEnum):
    """Coordinator component role names."""

    a = "a"
    b = "b"

    @staticmethod
    def all_nonmeta():
        """Return all non-meta roles."""
        return {
            Role.a,
            Role.b,
        }


META_ROLES = {}

MINIMAL_DEPLOYMENT = {
    Role.a: 1,
    Role.b: 1,
}

RECOMMENDED_DEPLOYMENT = {
    Role.a: 1,
    Role.b: 1,
}


ROLES_CONFIG = ClusterRolesConfig(
    roles=set(Role),
    meta_roles=META_ROLES,
    minimal_deployment=MINIMAL_DEPLOYMENT,
    recommended_deployment=RECOMMENDED_DEPLOYMENT,
)
