from sqlalchemy import delete, select

from keystone.core.errors import ServiceError
from keystone.platform.identity.models import (
    Role,
    RolePermission,
    User,
    UserRole,
)
from keystone.platform.identity.schemas import role_view
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.identity.services.authorization import (
    ADMIN_KEY,
    manageable,
    permission_catalog,
    protect_last_admin,
    role_permissions,
)


class RoleService:
    def __init__(self, context: AccessContext):
        self.context = context

    async def permissions(self):
        async with self.context.access("role:read") as (session, _):
            return [
                {"key": key, "name": name, "description": name}
                for key, name in permission_catalog(session).items()
            ]

    async def role_view(self, session, role):
        return role_view(role, await role_permissions(session, role))

    async def list_roles(self, offset, limit):
        async with self.context.access("role:read") as (session, _):
            catalog = permission_catalog(session)
            roles = await session.scalars(
                select(Role).order_by(Role.key).offset(offset).limit(limit)
            )
            roles = roles.all()
            if not roles:
                return []
            permissions = {role.id: set() for role in roles}
            grants = await session.execute(
                select(RolePermission.role_id, RolePermission.permission_key).where(
                    RolePermission.role_id.in_(permissions)
                )
            )
            for role_id, permission in grants:
                if permission in catalog:
                    permissions[role_id].add(permission)
            return [
                role_view(role, catalog if role.key == ADMIN_KEY else permissions[role.id])
                for role in roles
            ]

    async def save_role(self, data, identifier=None):
        operation = "update" if identifier else "create"
        action = f"identity.role.{operation}"
        async with self.context.access(
            f"role:{operation}",
            write=True,
            action=action,
            target="identity.role",
            target_id=identifier,
        ) as (session, auth):
            auth.grantable(data.permissions)
            previous = set()
            if identifier:
                role = await self.context.existing(session, Role, identifier)
                if role.key == ADMIN_KEY:
                    raise ServiceError(409, "builtin_role", "内置管理员角色不可编辑")
                previous = set(
                    await session.scalars(
                        select(RolePermission.permission_key).where(
                            RolePermission.role_id == identifier
                        )
                    )
                )
                auth.grantable((previous & auth.declared_permissions) | data.permissions)
                details = {
                    "before": {
                        "name": role.name,
                        "description": role.description,
                        "permissions": sorted(previous),
                    },
                    "after": {
                        "name": data.name,
                        "description": data.description,
                        "permissions": sorted(data.permissions),
                    },
                }
                role.name, role.description = data.name, data.description
                self.context.touch(role, auth)
                await session.execute(
                    delete(RolePermission).where(
                        RolePermission.role_id == identifier,
                        RolePermission.permission_key.in_(previous - data.permissions),
                    )
                )
            else:
                if data.key == ADMIN_KEY:
                    raise ServiceError(409, "builtin_role", "管理员角色标识已保留")
                role = Role(
                    key=data.key,
                    name=data.name,
                    description=data.description,
                    created_by=auth.user.id,
                    updated_by=auth.user.id,
                )
                session.add(role)
                await session.flush()
                details = {
                    "key": role.key,
                    "name": role.name,
                    "permissions": sorted(data.permissions),
                }
            session.add_all(
                RolePermission(role_id=role.id, permission_key=p, created_by=auth.user.id)
                for p in sorted(data.permissions - previous)
            )
            await self.context.log(session, auth, action, "identity.role", role.id, details)
            return await self.role_view(session, role)

    async def prepare_role_deletion(self, session, auth, identifier):
        """Identity checks inside the caller's exclusive identity transaction."""
        role = await self.context.existing(session, Role, identifier)
        if role.key == ADMIN_KEY:
            raise ServiceError(409, "builtin_role", "内置管理员角色不可删除")
        permissions = set(
            await session.scalars(
                select(RolePermission.permission_key).where(RolePermission.role_id == identifier)
            )
        )
        auth.grantable(permissions & auth.declared_permissions)
        if await session.scalar(
            select(UserRole.user_id).where(UserRole.role_id == identifier).limit(1)
        ):
            raise ServiceError(409, "role_in_use", "请先解除角色分配")
        return role, permissions

    async def delete_role(self, session, auth, role, permissions):
        """Called after all domain checks, using the same transaction and lock."""
        await self.context.log(
            session,
            auth,
            "identity.role.delete",
            "identity.role",
            role.id,
            {"key": role.key, "permissions": sorted(permissions)},
        )
        await session.delete(role)

    async def assign_role(self, user_id, role_id, assigned):
        action = "identity.role.assign"
        async with self.context.access(
            "role:assign", write=True, action=action, target="identity.user", target_id=user_id
        ) as (session, auth):
            user = await self.context.existing(session, User, user_id)
            role = await self.context.existing(session, Role, role_id)
            await manageable(session, auth, user)
            auth.grantable(await role_permissions(session, role))
            if role.key == ADMIN_KEY and not auth.administrator:
                raise ServiceError(403, "administrator_required", "只有内置管理员可分配管理员角色")
            existing = await session.get(UserRole, (user_id, role_id))
            if assigned and existing is None:
                session.add(UserRole(user_id=user_id, role_id=role_id, created_by=auth.user.id))
            elif not assigned and existing is not None:
                if role.key == ADMIN_KEY:
                    await protect_last_admin(session, user)
                await session.delete(existing)
            else:
                return
            self.context.touch(user, auth)
            await self.context.log(
                session,
                auth,
                action,
                "identity.user",
                user_id,
                {"role_id": str(role_id), "assigned": assigned},
            )
