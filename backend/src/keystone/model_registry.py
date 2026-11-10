"""The same explicit ORM imports for application startup and Alembic metadata."""

from keystone.core.database import Base


def load_models():
    from keystone.platform.audit import models as audit_models  # noqa: F401
    from keystone.platform.browser import models as browser_models  # noqa: F401
    from keystone.platform.dictionary import models as dictionary_models  # noqa: F401
    from keystone.platform.files import models as file_models  # noqa: F401
    from keystone.platform.flow import models as flow_models  # noqa: F401
    from keystone.platform.identity import models as identity_models  # noqa: F401
    from keystone.platform.model_access import models as model_access_models  # noqa: F401
    from keystone.worker import models as worker_models  # noqa: F401

    # Add explicit business models imports here; never import controllers or workflows.
    return Base.metadata
