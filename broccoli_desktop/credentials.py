"""Credential Manager-backed storage for the desktop API token."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

import keyring

SERVICE_NAME = "Broccoli Desktop"
ACCOUNT_NAME = "api-token"

_Result = TypeVar("_Result")


class CredentialStorageError(RuntimeError):
    """Raised when Windows Credential Manager cannot complete an operation."""


class KeyringProtocol(Protocol):
    """The limited keyring surface required by :class:`CredentialStore`."""

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class CredentialStore:
    """Store the API token only in the operating system credential manager."""

    def __init__(self, backend: KeyringProtocol = keyring) -> None:
        self._backend = backend

    def load_token(self) -> str | None:
        """Return the stored token, if a user has authenticated."""
        return self._run_backend_operation(
            lambda: self._backend.get_password(SERVICE_NAME, ACCOUNT_NAME)
        )

    def save_token(self, token: str) -> None:
        """Save a non-empty pasted token after normalizing surrounding whitespace."""
        normalized_token = token.strip()
        if not normalized_token:
            raise ValueError("A credential is required.")
        self._run_backend_operation(
            lambda: self._backend.set_password(SERVICE_NAME, ACCOUNT_NAME, normalized_token)
        )

    def delete_token(self) -> None:
        """Remove the stored API token."""
        self._run_backend_operation(
            lambda: self._backend.delete_password(SERVICE_NAME, ACCOUNT_NAME)
        )

    @staticmethod
    def _run_backend_operation(operation: Callable[[], _Result]) -> _Result:
        try:
            return operation()
        except Exception:
            raise CredentialStorageError("Credential storage is unavailable.") from None
