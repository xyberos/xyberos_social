"""Does the database hold the schema this code expects?

Deploying code before its migrations is the failure this application cannot absorb quietly: a
column added by `0020` is missing, so the first request that touches it raises a 500 several
minutes after the deploy looks healthy. The migration files name their own order, so the head the
code was written for is knowable without starting Alembic — and comparing it to the
`alembic_version` row turns a confusing runtime error into a refusal to boot.

No connection here is fatal on its own: a database that is briefly unreachable must not turn a
restart into an outage. `check_schema` reports what it found and leaves the decision to the
caller.
"""

import logging
import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

VERSIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

_REVISION = re.compile(r'^revision\s*=\s*["\']([^"\']+)["\']', re.M)
_DOWN_REVISION = re.compile(r'^down_revision\s*=\s*(.+)$', re.M)
_QUOTED = re.compile(r'["\']([^"\']+)["\']')

CURRENT = "current"
BEHIND = "behind"
ABSENT = "absent"
UNREACHABLE = "unreachable"


def head_revision(versions_dir: Path = VERSIONS_DIR) -> str | None:
    """The single revision nothing else revises, read from the migration files themselves."""
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in sorted(versions_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        revision = _REVISION.search(source)
        if revision is None:
            continue
        revisions.add(revision.group(1))
        down = _DOWN_REVISION.search(source)
        if down is None:
            continue
        # `None` and a tuple of parents both appear here; only the named ones are ancestors.
        parents.update(_QUOTED.findall(down.group(1)))
    heads = revisions - parents
    if len(heads) != 1:
        # More than one head means the history branched, which is an operator problem to merge
        # rather than something this check can judge.
        return None
    return heads.pop()


def check_schema(database: Session) -> tuple[str, str | None]:
    """`(status, applied revision)` for the connection, in this code's terms."""
    try:
        applied = database.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:
        # A failed statement leaves a PostgreSQL transaction aborted, so the session has to be
        # rolled back before it can answer anything else — otherwise the reachability probe
        # below fails for the same reason and every never-migrated database looks unreachable.
        database.rollback()
        try:
            database.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return UNREACHABLE, None
        # Reachable, but no revision could be read: the database was never migrated.
        return ABSENT, None
    expected = head_revision()
    if expected is None or applied == expected:
        return CURRENT, applied
    return BEHIND, applied


class SchemaNotCurrent(RuntimeError):
    """The database does not hold the schema this code was written for."""


def verify_schema(database: Session, *, strict: bool) -> str:
    """Report the schema state, and refuse to continue when `strict` and it is behind.

    A transient database outage is never fatal here: it is a warning, so a restart during an
    incident does not extend the incident. Being behind is fatal in production, because every
    request that touches a new column would otherwise fail long after the deploy looked healthy.
    """
    status, applied = check_schema(database)
    expected = head_revision()
    if status == CURRENT:
        return status
    if status == UNREACHABLE:
        logger.warning(
            "could not read the schema revision; the database was unreachable at startup",
            extra={"event": "schema.unreachable", "expected_revision": expected},
        )
        return status

    detail = (
        "the database has no migration history"
        if status == ABSENT
        else f"the database is at {applied!r}"
    )
    message = f"{detail} but this code expects {expected!r}; run `alembic upgrade head`"
    if strict:
        raise SchemaNotCurrent(message)
    logger.warning(message, extra={"event": f"schema.{status}", "expected_revision": expected})
    return status
