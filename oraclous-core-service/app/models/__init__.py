# Register all ORM models in the SQLAlchemy mapper registry.
# Importing this package ensures every model class is loaded before mapper
# configuration runs, so string-based relationship references resolve correctly.
import app.models.tool_definition  # noqa: F401
import app.models.tool_instance  # noqa: F401
import app.models.workflow  # noqa: F401
import app.models.execution  # noqa: F401
import app.models.jobs  # noqa: F401
import app.models.capability_descriptor  # noqa: F401
