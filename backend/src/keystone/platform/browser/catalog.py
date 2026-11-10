from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={"browser:use": "使用本人浏览器空间", "browser:stop": "停止本人浏览器运行"},
    audit_actions={
        "browser.workspace.register": {"owner_id"},
        "browser.run.open": {"workspace_id", "state"},
        "browser.run.stop": {"workspace_id", "state"},
        "browser.run.check": {"workspace_id", "state"},
        "browser.run.result": {"workspace_id", "state"},
        "browser.connection.open": {"workspace_id"},
        "browser.workspace.backup": {"state"},
        "browser.workspace.restore": {"backup_at"},
    },
)
