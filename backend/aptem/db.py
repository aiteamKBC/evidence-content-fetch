import re

from django.conf import settings
from django.db.backends.signals import connection_created


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def set_postgres_search_path(sender, connection, **kwargs):
    if connection.vendor != "postgresql":
        return

    search_path = getattr(settings, "DATABASE_SEARCH_PATH", "")
    schemas = [schema.strip() for schema in search_path.split(",") if schema.strip()]
    if not schemas:
        return
    if not all(IDENTIFIER_RE.match(schema) for schema in schemas):
        raise ValueError("DATABASE_SEARCH_PATH contains an invalid schema name.")

    quoted = ", ".join(connection.ops.quote_name(schema) for schema in schemas)
    with connection.cursor() as cursor:
        cursor.execute(f"set search_path to {quoted}")


connection_created.connect(set_postgres_search_path)
