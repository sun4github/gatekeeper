---
name: psycopg-db-pattern
description: 'Create and refactor Python SQL data access using psycopg and psycopg_pool only. Use for connection-info builders, schema-safe SQL setup, simple async helper functions, JSONB updates, and no-ORM patterns in gatekeeper services.'
argument-hint: 'Describe the DB operation, tables, and expected input/output shape.'
---

# Psycopg DB Pattern

## Outcome
Produce small, explicit, async database functions that:
- Use `psycopg` + `psycopg_pool` directly (no ORM).
- Build conninfo from required environment variables.
- Validate dynamic SQL identifiers before interpolation.
- Keep SQL statements readable and parameterized.
- Keep data transformation logic simple and local.

## When to Use
Use this skill when implementing or modifying DB access in this repository for:
- Connection pool initialization.
- Schema/table creation and bootstrap.
- Insert/update/select helpers.
- JSONB reads and writes.
- Session/event append-close workflows.

Do not use this skill when:
- An ORM model abstraction is requested.
- Query generation is highly dynamic and cannot be validated safely.

## Procedure
1. Confirm required configuration inputs.
Check all required env vars for DB auth and target DB.
Fail fast with clear `RuntimeError` if any required var is missing.

2. Build a deterministic conninfo string.
Compose conninfo from explicit env keys (`host`, `dbname`, `user`, `password`).
Avoid hidden defaults for critical connection fields.

3. Validate any interpolated SQL identifiers.
Only allow safe schema/table identifier patterns (letters, digits, underscore, leading alpha/underscore).
Reject invalid identifiers before building SQL.

4. Initialize and open `AsyncConnectionPool`.
Use a module-level pool and explicit startup/shutdown hooks.
Create required schema/table idempotently with `CREATE ... IF NOT EXISTS`.

5. Keep each DB helper focused.
Each function should do one clear DB task (fetch, append, close, aggregate).
Use `async with pool.connection()` and `async with conn.transaction()` for write paths.

6. Parameterize values, not identifiers.
Use `%s` bind params for data values.
Only interpolate pre-validated identifiers (schema/table names).

7. Keep JSONB logic explicit.
Read JSONB into simple dict/list structures.
Guard shape assumptions (`dict`, `list`, keys present).
Mutate minimal fields and write back once.

8. Handle time semantics consistently.
Use timezone-aware timestamps.
Pick one clock basis for comparisons (local-time or UTC) and apply consistently.

9. Return deterministic output shapes.
For aggregations, return stable, typed mappings (for example `dict[str, int]`).
Return empty objects instead of `None` where callers expect iterables/maps.

## Decision Points
- No active pool available:
Return safe default (`None` side-effect for writes, `{}` for map-return reads) unless caller contract requires hard failure.

- Record exists vs does not exist:
For append-style events, insert on first event; otherwise update latest row under transaction.

- Missing nested JSON path:
Create missing nested containers (`devices`, per-device services) before append.

- Incomplete/invalid historical timestamps:
Skip duration calculation instead of failing the whole operation.

## Quality Checks
- All SQL value inputs are parameterized.
- All interpolated identifiers were regex-validated first.
- No ORM imports/usages were introduced.
- Function names reflect one responsibility each.
- Writes occur inside transactions.
- Time computations use timezone-aware `datetime` values.
- Public helpers return predictable shapes.

## Implementation Pattern
Use this structural pattern for new functions:
1. Guard pool state.
2. Prepare normalized inputs (time label, payload object, filters).
3. Acquire connection and optional transaction.
4. Fetch existing row state if needed.
5. Apply small in-memory transformation.
6. Persist via one insert/update statement.
7. Return deterministic result.

## Suggested Prompts
- "Use `psycopg-db-pattern` to add a helper that reads latest `viewrecords` for one user and one service."
- "Use `psycopg-db-pattern` to refactor this ORM query into async psycopg with parameterized SQL."
- "Use `psycopg-db-pattern` to add schema bootstrap for a new JSONB events table."
