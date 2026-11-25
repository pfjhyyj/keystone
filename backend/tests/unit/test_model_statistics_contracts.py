"""Statistics request shape is validated without a database."""

import pytest
from pydantic import ValidationError

from keystone.platform.model_access.schemas.statistics import StatisticsQuery


@pytest.mark.parametrize(
    "params",
    [{"group_by": "secret"}, {"offset": 100001}],
)
def test_statistics_reject_invalid_query_fields(params):
    with pytest.raises(ValidationError):
        StatisticsQuery.model_validate(params)


def test_flow_definition_dimension_and_bounded_series():
    query = StatisticsQuery.model_validate(
        {"group_by": "flow_definition", "missing": "flow_definition", "series": True, "limit": 20}
    )
    assert query.filters()["definition_key"] is None
    with pytest.raises(ValidationError):
        StatisticsQuery.model_validate({"series": True})  # default page of 50 groups
