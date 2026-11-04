"""Compatibility imports for platform integration tests."""

from dataclasses import replace

from keystone.examples.document_review.contracts import PACKAGE as _PACKAGE
from keystone.examples.document_review.contracts import (
    Judgment as Judgment,
)
from keystone.examples.document_review.contracts import (
    ReviewInput as ReviewInput,
)
from keystone.examples.document_review.contracts import (
    ReviewOutput as ReviewOutput,
)
from keystone.examples.document_review.contracts import (
    definition as definition,
)

definition = replace(
    definition,
    required_permissions=(
        "model:invoke",
        "test_files.asset:create",
        "test_files.asset:content_read",
    ),
)
PACKAGE = replace(_PACKAGE, definitions=(definition,))
