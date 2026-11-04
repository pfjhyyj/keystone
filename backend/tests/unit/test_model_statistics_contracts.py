"""Statistics request shape is validated without a database."""

import pytest
from pydantic import ValidationError

from keystone.platform.model_access.schemas.statistics import StatisticsQuery


@pytest.mark.parametrize(
    "params",
    [
        {"bucket": "second"},
        {"group_by": "secret"},
        {"missing": "credentials"},
        {"user_id": "bad"},
        {"offset": 100001},
    ],
)
def test_statistics_reject_invalid_query_fields(params):
    with pytest.raises(ValidationError):
        StatisticsQuery.model_validate(params)
