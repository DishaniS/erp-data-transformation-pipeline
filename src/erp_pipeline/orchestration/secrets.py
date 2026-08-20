"""Credentials are referenced, never stored.

THE RULE PHASE 3 SET, WHICH PHASE 13 MUST NOT BREAK
---------------------------------------------------
``ConnectionSettings`` are runtime-only. A registered source records *how to
reach* a system - host, port, database, driver - and never the password.

So a registered source carries a ``credential_ref``: a name. At execution time
the name is handed to a ``SecretProvider`` which returns the secret, it is used
to open a connection, and it is never written to the job row, the catalog, a
log line or an API response.

This is deliberately not a secrets platform. It is the smallest abstraction
that keeps passwords out of the database, with a seam where a real vault would
be plugged in.
"""

from __future__ import annotations

import os
from typing import Mapping, Protocol, runtime_checkable

from erp_pipeline.orchestration.errors import SecretUnavailableError


@runtime_checkable
class SecretProvider(Protocol):
    """Resolves a credential reference to its value."""

    def resolve(self, reference: str) -> str: ...

    def has(self, reference: str) -> bool: ...


class EnvironmentSecretProvider:
    """Reads secrets from environment variables.

    A reference like ``erp_source_pw`` reads ``ERP_SECRET_ERP_SOURCE_PW``. The
    prefix keeps the lookup explicit, so a caller cannot use a reference to
    read arbitrary environment (``PATH``, cloud tokens) by naming it.
    """

    def __init__(self, prefix: str = "ERP_SECRET_") -> None:
        self.prefix = prefix

    def _key(self, reference: str) -> str:
        return f"{self.prefix}{reference}".upper().replace("-", "_")

    def has(self, reference: str) -> bool:
        return bool(os.environ.get(self._key(reference)))

    def resolve(self, reference: str) -> str:
        value = os.environ.get(self._key(reference))

        if not value:
            # The variable NAME is safe to disclose; the value never is.
            raise SecretUnavailableError(
                f"no secret is configured for reference {reference!r}",
                reference=reference,
                expected_environment_variable=self._key(reference),
            )

        return value

    def __repr__(self) -> str:
        return f"EnvironmentSecretProvider(prefix={self.prefix!r})"


class InMemorySecretProvider:
    """For tests and local demos. Redacts itself on repr."""

    def __init__(self, secrets: Mapping[str, str] | None = None) -> None:
        self._secrets = dict(secrets or {})

    def put(self, reference: str, value: str) -> None:
        self._secrets[reference] = value

    def has(self, reference: str) -> bool:
        return reference in self._secrets

    def resolve(self, reference: str) -> str:
        if reference not in self._secrets:
            raise SecretUnavailableError(
                f"no secret is configured for reference {reference!r}",
                reference=reference,
            )

        return self._secrets[reference]

    def __repr__(self) -> str:
        # References are operational names and safe to show. Values are not,
        # and a repr is exactly where they would otherwise leak into a log.
        return (
            f"InMemorySecretProvider(references={sorted(self._secrets)!r}, "
            "values=<redacted>)"
        )


class NullSecretProvider:
    """Resolves nothing. The right default when no credentials are configured."""

    def has(self, reference: str) -> bool:
        return False

    def resolve(self, reference: str) -> str:
        raise SecretUnavailableError(
            "no secret provider is configured for this deployment",
            reference=reference,
        )

    def __repr__(self) -> str:
        return "NullSecretProvider()"


__all__ = [
    "SecretProvider",
    "EnvironmentSecretProvider",
    "InMemorySecretProvider",
    "NullSecretProvider",
]
