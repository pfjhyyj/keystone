from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={
        "environment:use": "使用本人交互环境",
        "environment:stop": "结束本人交互环境会话",
    },
    audit_actions={
        "environment.register": {"owner_id"},
        "environment.session.open": {"environment_id", "state"},
        "environment.session.stop": {"environment_id", "state"},
        "environment.session.check": {"environment_id", "state"},
        "environment.session.result": {"environment_id", "state"},
        "environment.connection.open": {"environment_id"},
        "environment.backup": {"state"},
        "environment.restore": {"backup_at"},
    },
)
