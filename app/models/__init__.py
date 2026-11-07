"""ORM models.

Importing this package registers every table on ``Base.metadata``, which is what
Alembic autogenerate and the test fixtures rely on.
"""

from app.db.base import Base
from app.models.click import Click
from app.models.link import Link
from app.models.user import User

__all__ = ["Base", "Click", "Link", "User"]
