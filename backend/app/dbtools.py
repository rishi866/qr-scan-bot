"""Database hygiene for a hosted, shared PostgreSQL (Supabase): our own schema, Row Level Security, a self-check.

Supabase serves the ``public`` schema - and any schema listed in its Data API settings - over HTTP to the roles ``anon``,
``authenticated`` and ``service_role``. This application only ever talks to its database directly, so the safe setup is a
dedicated schema that is *not* exposed (``DB_SCHEMA``), with Row Level Security switched on for every table as a second lock.

On an ordinary PostgreSQL (no such roles) nothing in here changes anything.
"""

from __future__ import annotations

from sqlalchemy import bindparam, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

API_ROLES = ("anon", "authenticated", "service_role")
SUPABASE_HOST_SUFFIXES = (".supabase.co", ".supabase.com")

# (status, label, detail) as printed by ``python -m app.cli check``; status: True = fine, None = warning, False = problem
Advice = tuple[bool | None, str, str]


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def effective_schema(db: AsyncSession) -> str:
    """The schema our tables live in: ``DB_SCHEMA``, otherwise whatever the connection's default is."""
    configured = get_settings().db_schema
    if configured:
        return configured
    return (await db.execute(text("SELECT current_schema()"))).scalar() or "public"


async def schema_exists(db: AsyncSession, name: str) -> bool:
    row = await db.execute(text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :name"), {"name": name})
    return row.first() is not None


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


def hosted_db_advice(database_url: str, schema: str, pool_total: int) -> list[Advice]:
    """Configuration advice that only needs the URL: reads nothing from the database."""
    url = make_url(database_url)
    host = (url.host or "").lower()
    if not host.endswith(SUPABASE_HOST_SUFFIXES):
        return []
    advice: list[Advice] = []
    if url.port == 6543:
        advice.append((False, "database port", "6543 is Supabase's transaction pooler, which does not support the prepared statements this app uses - use the session pooler (port 5432)"))
    if "ssl" not in url.query and "sslmode" not in url.query:
        advice.append((None, "database SSL", "add ?ssl=require to DATABASE_URL (Supabase accepts unencrypted connections unless SSL is enforced)"))
    if host.startswith("db."):
        advice.append((None, "database host", "the direct connection is IPv6-only unless the project has the IPv4 add-on; from an IPv4-only server use the session pooler host (aws-...pooler.supabase.com:5432)"))
    elif "pooler" in host:
        advice.append((None, "connection pool", f"in session mode each open connection takes one pooler slot: the API and the bot may open up to {pool_total} each (DB_POOL_SIZE + DB_MAX_OVERFLOW) - keep 2 x {pool_total} below the pooler's pool size (Database settings)"))
    if not schema:
        advice.append((None, "DB_SCHEMA", "not set: the tables would go to the 'public' schema, which Supabase exposes over its HTTP API - set DB_SCHEMA=qrbot"))
    return advice
