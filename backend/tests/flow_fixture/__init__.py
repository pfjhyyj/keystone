"""Test-only harness. Never installed or registered by the application."""


def register():
    from flow_fixture import workflow  # noqa: F401


def install():
    from flow_fixture.contracts import definition
    from keystone import composition

    composition.FLOW_DEFINITIONS = (definition,)
    composition.register_workflows = register
