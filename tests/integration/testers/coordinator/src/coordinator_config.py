from enum import Enum, unique, StrEnum

from coordinated_workers.coordinator import ClusterRolesConfig


@unique
class Role(StrEnum):
    """Coordinator component role names."""
    a = "a"
    b = "b"

    @staticmethod
    def all_nonmeta():
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
    roles={role for role in Role},
    meta_roles=META_ROLES,
    minimal_deployment=MINIMAL_DEPLOYMENT,
    recommended_deployment=RECOMMENDED_DEPLOYMENT,
)