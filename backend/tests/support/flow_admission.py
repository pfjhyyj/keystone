"""Accept an empty trusted test Flow through the test business entry."""

from keystone.core.schemas import Input
from keystone.platform.flow.schemas.definitions import FlowDefinition


class Empty(Input):
    pass


def accept(client, *, request_key="resources"):
    definition = FlowDefinition(
        key="test.resources",
        name="Resources",
        description="Test only",
        revision="1",
        input_model=Empty,
        output_model=Empty,
        entrypoint="test.resources.run",
    )
    client.app.state.runtime.flow_definitions = {definition.key: definition}
    data = {"definition_key": definition.key, "request_key": request_key, "input": {}}
    return client.post("/api/v1/_test/review", json=data), data
