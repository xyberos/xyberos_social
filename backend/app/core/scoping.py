"""Scope predicates that fail closed.

Every commercial row is owned by a business, and every social row is owned by an
account, a community, or a conversation. Records that carry a scope store it as a
`(scope_type, scope_id)` pair so one predicate covers every tenant-capable table.

A mismatch is a `ScopeViolation`, never a silent empty result and never a leaked row:
`app.api.middleware` maps it to a non-revealing 404, so crossing scopes fails closed the
same way a missing row does.
"""

from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

PLATFORM = "platform"
ACCOUNT = "account"
COMMUNITY = "community"
CONVERSATION = "conversation"
BUSINESS = "business"

SCOPE_TYPES = (ACCOUNT, COMMUNITY, CONVERSATION, BUSINESS, PLATFORM)


class ScopeViolation(Exception):
    """A row was read or written outside the scope the caller is entitled to."""

    def __init__(self, expected: "Scope", actual: "Scope") -> None:
        super().__init__(f"scope {actual} is outside {expected}")
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class Scope:
    kind: str
    id: UUID | None = None

    def __post_init__(self) -> None:
        if self.kind not in SCOPE_TYPES:
            raise ValueError(f"{self.kind!r} is not a known scope type")
        if self.kind == PLATFORM and self.id is not None:
            raise ValueError("the platform scope is not tenant-scoped")
        if self.kind != PLATFORM and self.id is None:
            raise ValueError(f"the {self.kind} scope requires an id")

    def __str__(self) -> str:
        return self.kind if self.id is None else f"{self.kind}:{self.id}"

    def matches(self, other: "Scope") -> bool:
        return self.kind == other.kind and self.id == other.id

    def columns(self) -> dict[str, Any]:
        """The scope pair as stored on a scope-carrying row."""
        return {"scope_type": self.kind, "scope_id": self.id}


def platform_scope() -> Scope:
    return Scope(PLATFORM)


def account_scope(account_id: UUID) -> Scope:
    return Scope(ACCOUNT, account_id)


def community_scope(community_id: UUID) -> Scope:
    return Scope(COMMUNITY, community_id)


def conversation_scope(conversation_id: UUID) -> Scope:
    return Scope(CONVERSATION, conversation_id)


def business_scope(business_id: UUID) -> Scope:
    """Reserved for Phase 11; declared here so commercial call sites have one name."""
    return Scope(BUSINESS, business_id)


def scope_type_check_sql(column: str = "scope_type") -> str:
    """One source of truth for the scope-type check constraint on scope-carrying tables."""
    allowed = ", ".join(f"'{kind}'" for kind in SCOPE_TYPES)
    return f"{column} IN ({allowed})"


def require_scope(expected: Scope, actual: Scope) -> None:
    if not expected.matches(actual):
        raise ScopeViolation(expected, actual)


def scope_criteria(model: type, scope: Scope) -> list[Any]:
    return [model.scope_type == scope.kind, model.scope_id == scope.id]


def scoped_select(model: type, scope: Scope):
    """`SELECT` restricted to one scope for a model that carries `scope_type`/`scope_id`."""
    return select(model).where(*scope_criteria(model, scope))


def scoped_get(database: Session, model: type, identity: Hashable, scope: Scope):
    """Load one row and assert its scope before returning it to the caller."""
    entity = database.get(model, identity)
    if entity is None:
        return None
    require_scope(scope, Scope(entity.scope_type, entity.scope_id))
    return entity
