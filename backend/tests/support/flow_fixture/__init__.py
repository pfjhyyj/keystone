"""Test-only harness. Never installed or registered by the application."""


def register():
    from . import workflow  # noqa: F401


def install():
    from keystone import composition
    from keystone.examples.document_review.contracts import PACKAGE

    composition.FLOW_PACKAGES = (PACKAGE,)
    composition.execution_version.cache_clear()
