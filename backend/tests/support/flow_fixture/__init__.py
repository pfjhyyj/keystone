"""Test-only harness. Never installed or registered by the application."""


def register():
    from . import workflow  # noqa: F401


def install():
    from keystone import composition

    from .contracts import definition

    composition.FLOW_DEFINITIONS = (definition,)
    composition.register_workflows = register
