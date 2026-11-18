from alembic import context

from app.db import engine
from app.models import Base
from app.settings import settings


def run_migrations() -> None:
    if context.is_offline_mode():
        context.configure(url=settings().database_url, target_metadata=Base.metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    with engine().connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
