"""Scope: multi-tenancy without a permission model.

DESIGN.md §14. Every Psych entry point takes a Scope, every Record is stamped
with one, and every store query is filtered by one. Psych does not interpret a
Scope beyond identity and isolation: it does not know what a tenant is, whether
a principal may act, or how anyone authenticated. That is the consumer's
``Policy`` port.

This is what gives Psych tenant-correct data and per-tenant metering without
owning orgs, teams or roles.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["Scope"]

_MAX_LABELS: Final = 32
"""Labels ride on every Record. An unbounded dict here is an unbounded write
amplification across the whole log, so it is capped."""

_MAX_LABEL_LENGTH: Final = 256


class Scope(BaseModel):
    """The tenancy and identity context threaded through every call.

    Frozen, because a Scope that can be mutated after a Record is stamped with
    it is a Scope that can be mutated between the authorization check and the
    call it authorised.

    Attributes:
        tenant: the isolation boundary. Two Runs with different tenants must
            never see each other's data, and this is the field that decides it.
        principal: who is acting, when the consumer knows. Psych records it and
            passes it to Policy; it never interprets it.
        labels: free-form consumer metadata, stamped on Records and available
            for their own filtering and metering.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str = Field(min_length=1, max_length=256)
    principal: str | None = Field(default=None, max_length=256)
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("labels")
    @classmethod
    def _labels_are_bounded(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > _MAX_LABELS:
            raise ValueError(
                f"a Scope carries at most {_MAX_LABELS} labels, got {len(value)}; "
                "labels are stamped on every Record, so they are capped"
            )
        for key, label in value.items():
            if not key:
                raise ValueError("a Scope label key cannot be empty")
            if len(key) > _MAX_LABEL_LENGTH or len(label) > _MAX_LABEL_LENGTH:
                raise ValueError(f"Scope label {key!r} exceeds {_MAX_LABEL_LENGTH} characters")
        return value

    @property
    def pool_key(self) -> tuple[str, str | None]:
        """The tenancy half of an MCP client pool key.

        DESIGN.md §10.4 is blunt about this: never pool MCP clients by URL alone,
        pool by ``(scope, server, credential)``. Pooling by URL will eventually
        send tenant A's OAuth token on tenant B's call, and it is a one-line
        mistake. This property exists so that the tenancy half of that key has
        one definition rather than being re-derived at each call site.

        Labels are deliberately excluded: they are consumer metadata and two
        Runs differing only by label are the same tenant and principal, so they
        may share a pooled connection.
        """
        return (self.tenant, self.principal)

    def __str__(self) -> str:
        if self.principal is None:
            return f"tenant={self.tenant}"
        return f"tenant={self.tenant} principal={self.principal}"
