"""Test-only harness. Never installed or registered by the application."""


def register():
    from . import workflow  # noqa: F401


def install():
    from keystone import composition
    from support.business_catalog import install_catalog
    from support.flow_fixture.contracts import PACKAGE

    install_catalog()
    composition.FLOW_PACKAGES = (PACKAGE,)
    composition.execution_version.cache_clear()
