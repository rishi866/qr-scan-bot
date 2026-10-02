"""Database hygiene for a hosted, shared PostgreSQL (Supabase): our own schema, Row Level Security, a self-check.

Supabase serves the ``public`` schema - and any schema listed in its Data API settings - over HTTP to the roles ``anon``,
``authenticated`` and ``service_role``. This application only ever talks to its database directly, so the safe setup is a
dedicated schema that is *not* exposed (``DB_SCHEMA``), with Row Level Security switched on for every table as a second lock.

On an ordinary PostgreSQL (no such roles) nothing in here changes anything.
"""

from __future__ import annotations

import re

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

API_ROLES = ("anon", "authenticated", "service_role")
SUPABASE_HOST_SUFFIXES = (".supabase.co", ".supabase.com")

# (status, label, detail) as printed by ``python -m app.cli check``; status: True = fine, None = warning, False = problem
Advice = tuple[bool | None, str, str]


class SchemaAccessError(RuntimeError):
    """The database role may not create / use the configured schema. The message says how to fix it."""


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sql_name(name: str) -> str:
    """A name as one would type it in SQL: quoted only when it has to be."""
    return name if re.fullmatch(r"[a-z_][a-z0-9_]*", name) else quote_ident(name)


async def effective_schema(db: AsyncSession) -> str:
    """The schema our tables live in: ``DB_SCHEMA``, otherwise whatever the connection's default is."""
    configured = get_settings().db_schema
    if configured:
        return configured
    return (await db.execute(text("SELECT current_schema()"))).scalar() or "public"


def ensure_schema(connection: Connection, schema: str) -> None:
    """Create ``schema`` unless it exists. Raises ``SchemaAccessError`` when this database role cannot work in it.

    ``pg_namespace`` is consulted, not ``information_schema.schemata``: the latter hides schemas the role has no privilege on,
    which would turn "belongs to another role" into a baffling 'schema already exists'.
    """
    owner = connection.execute(text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = :name"), {"name": schema}).scalar()
    me = connection.execute(text("SELECT current_user")).scalar()
    if owner is None:
        if not connection.execute(text("SELECT has_database_privilege(current_database(), 'CREATE')")).scalar():
            database = connection.execute(text("SELECT current_database()")).scalar()
            raise SchemaAccessError(
                f"the database role '{me}' may not create the schema '{schema}'. Ask the database owner to run:  "
                f"GRANT CREATE ON DATABASE {_sql_name(database)} TO {_sql_name(me)};  "
                f"(or create it for the role:  CREATE SCHEMA {schema} AUTHORIZATION {_sql_name(me)};)  - see docs/SUPABASE.md"
            )
        connection.execute(text(f"CREATE SCHEMA {quote_ident(schema)}"))
    elif not connection.execute(text("SELECT has_schema_privilege(:name, 'CREATE')"), {"name": schema}).scalar():
        raise SchemaAccessError(
            f"the schema '{schema}' exists but belongs to '{owner}', and the database role '{me}' may not create tables in it. "
            f"Connect as '{owner}' instead, or hand the schema over:  ALTER SCHEMA {schema} OWNER TO {_sql_name(me)};  "
            f"(and every table in it - see docs/SUPABASE.md, Troubleshooting)"
        )
    connection.commit()


async def api_roles_present(db: AsyncSession) -> list[str]:
    stmt = text("SELECT rolname FROM pg_roles WHERE rolname IN :names ORDER BY rolname").bindparams(bindparam("names", expanding=True))
    return [row[0] for row in await db.execute(stmt, {"names": list(API_ROLES)})]


async def tables_without_rls(db: AsyncSession, schema: str) -> list[str]:
    rows = await db.execute(
        text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relkind IN ('r', 'p') AND NOT c.relrowsecurity ORDER BY c.relname"
        ),
        {"schema": schema},
    )
    return [row[0] for row in rows]


async def enable_row_level_security(db: AsyncSession, schema: str) -> list[str]:
    """Switch Row Level Security on for every table of ``schema`` that lacks it. Idempotent.

    Only done where Supabase's API roles exist. With RLS on and no policies those roles can read nothing, while this
    application - which owns the tables - is unaffected (owners bypass RLS). Returns the tables that were changed.
    """
    if not await api_roles_present(db):
        return []
    changed = []
    for table in await tables_without_rls(db, schema):
        await db.execute(text(f"ALTER TABLE {quote_ident(schema)}.{quote_ident(table)} ENABLE ROW LEVEL SECURITY"))
        changed.append(table)
    return changed


def exposed_schemas_from(role_settings: list[str]) -> list[str]:
    """``['pgrst.db_schemas=public, graphql_public']`` -> ``['public', 'graphql_public']`` (the ``authenticator`` role's settings)."""
    for setting in role_settings:
        key, _, value = setting.partition("=")
        if key == "pgrst.db_schemas":
            return [part.strip() for part in value.split(",") if part.strip()]
    return []


async def _data_api_schemas(db: AsyncSession) -> list[str]:
    """Schemas PostgREST is configured to expose (``pgrst.db_schemas`` on the ``authenticator`` role)."""
    rows = await db.execute(text("SELECT unnest(rolconfig) FROM pg_roles WHERE rolname = 'authenticator' AND rolconfig IS NOT NULL"))
    return exposed_schemas_from([row[0] for row in rows])


async def exposure_problems(db: AsyncSession, schema: str) -> list[str]:
    """Ways our data could be reachable through Supabase's HTTP API. Empty on a database without Supabase's roles."""
    roles = await api_roles_present(db)
    if not roles:
        return []
    problems = []
    if schema == "public":
        problems.append("the tables are in the 'public' schema, which Supabase exposes over its HTTP API - set DB_SCHEMA (e.g. qrbot) and migrate again")
    elif schema in await _data_api_schemas(db):
        problems.append(f"schema '{schema}' is listed under Exposed schemas in the Data API settings - remove it there")
    if schema != "public":
        for role in roles:
            usable = (await db.execute(text("SELECT has_schema_privilege(:role, :schema, 'USAGE')"), {"role": role, "schema": schema})).scalar()
            if usable:
                problems.append(f"role '{role}' can use schema '{schema}' - run: REVOKE ALL ON SCHEMA {quote_ident(schema)} FROM {role}")
    missing = await tables_without_rls(db, schema)
    if missing:
        listed = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        problems.append(f"Row Level Security is off for {len(missing)} table(s) ({listed}) - `python -m app.cli migrate` switches it on")
    return problems


def libpq_url(database_url: str, root_cert: str | None = None) -> str:
    """``postgresql+asyncpg://u:p@h:5432/db?ssl=require`` -> ``postgresql://u:p@h:5432/db?sslmode=require`` (for pg_dump / psql).

    ``root_cert`` (the path asyncpg reads from ``PGSSLROOTCERT``) is passed on as ``sslrootcert`` for ``verify-ca`` / ``verify-full``.
    """
    url = make_url(database_url)
    query = dict(url.query)
    ssl = query.pop("ssl", None)
    if ssl is not None and "sslmode" not in query:
        query["sslmode"] = ssl if isinstance(ssl, str) else ssl[0]
    if root_cert and "sslrootcert" not in query:
        query["sslrootcert"] = root_cert
    return url.set(drivername="postgresql", query=query).render_as_string(hide_password=False)


def hosted_db_advice(database_url: str, schema: str, pool_total: int) -> list[Advice]:
    """Configuration advice that only needs the URL: reads nothing from the database."""
    url = make_url(database_url)
    host = (url.host or "").lower()
    if not host.endswith(SUPABASE_HOST_SUFFIXES):
        return []
    advice: list[Advice] = []
    if url.port == 6543:
        advice.append((False, "database port", "6543 is Supabase's transaction pooler, which does not support the prepared statements this app uses - use the session pooler (port 5432)"))
    ssl_mode = str(url.query.get("ssl", url.query.get("sslmode", "require")))
    if ssl_mode in ("disable", "allow", "prefer", "false", "0"):
        advice.append((None, "database SSL", f"ssl={ssl_mode} may send the password and all data unencrypted - use ssl=require (or verify-full)"))
    if host.startswith("db."):
        advice.append((None, "database host", "the direct connection is IPv6-only unless the project has the IPv4 add-on; from an IPv4-only server use the session pooler host (aws-...pooler.supabase.com:5432)"))
    elif "pooler" in host:
        advice.append((None, "connection pool", f"in session mode each open connection takes one pooler slot: the API and the bot may open up to {pool_total} each (DB_POOL_SIZE + DB_MAX_OVERFLOW) - keep 2 x {pool_total} below the pooler's pool size (Database settings)"))
    if not schema:
        advice.append((None, "DB_SCHEMA", "not set: the tables would go to the 'public' schema, which Supabase exposes over its HTTP API - set DB_SCHEMA=qrbot"))
    return advice
