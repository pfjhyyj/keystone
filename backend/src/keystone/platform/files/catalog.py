"""Lightweight permission and audit declarations owned by this module."""

from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={
        "file:create": "上传文件",
        "file:inspect": "查看文件元数据",
        "file:content_read": "下载文件内容",
        "file:manage": "管理文件与授权",
    },
    audit_actions={
        "file.upload.start": {"initial_grants"},
        "file.upload.complete": {"size_bytes"},
        "file.upload.failure": {"reason"},
        "file.upload.residue_cleanup": set(),
        "file.update": {"before", "status", "after", "name"},
        "file.delete.request": set(),
        "file.delete.complete": {"grants_removed", "grant_scope"},
        "file.cleanup.failure": {"reason"},
        "file.grant": {"grant_id", "added", "action", "subject_type", "role_id", "user_id"},
        "file.reference": {"added", "owner", "slot"},
    },
)
