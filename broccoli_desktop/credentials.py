"""Credential Manager-backed storage for the desktop API token."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

import keyring
from keyring.errors import PasswordDeleteError

SERVICE_NAME = "Broccoli Desktop"
ACCOUNT_NAME = "api-token"
PROXY_PASSWORD_ACCOUNT_NAME = "proxy-password"

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
        """Remove the stored API token.

        Treats an already-missing entry as success. The real Windows backend
        raises PasswordDeleteError when nothing matches -- unlike a naive
        in-memory fake -- and callers here (logout, an invalid-token cleanup,
        a background auth failure) call this unconditionally. "No token in
        the vault" is the goal of every one of them, and it is already true.
        """
        self._delete_ignoring_missing(ACCOUNT_NAME)

    def load_proxy_password(self) -> str | None:
        """Return the stored proxy password, if one was saved."""
        return self._run_backend_operation(
            lambda: self._backend.get_password(SERVICE_NAME, PROXY_PASSWORD_ACCOUNT_NAME)
        )

    def save_proxy_password(self, password: str) -> None:
        """Save a non-empty proxy password under its own vault entry.

        Kept separate from the API token entry so clearing one credential can
        never reach into the other, and so this is the only place the proxy
        password is ever written to disk.
        """
        if not password:
            raise ValueError("A credential is required.")
        self._run_backend_operation(
            lambda: self._backend.set_password(SERVICE_NAME, PROXY_PASSWORD_ACCOUNT_NAME, password)
        )

    def delete_proxy_password(self) -> None:
        """Remove the stored proxy password.

        Disabling the proxy calls this unconditionally, and disabling is the
        default for everyone who never configured one -- see delete_token for
        why an already-missing entry must not be treated as a failure here.
        """
        self._delete_ignoring_missing(PROXY_PASSWORD_ACCOUNT_NAME)

    def _delete_ignoring_missing(self, account_name: str) -> None:
        def operation() -> None:
            try:
                self._backend.delete_password(SERVICE_NAME, account_name)
            except PasswordDeleteError:
                pass

        self._run_backend_operation(operation)

    @staticmethod
    def _run_backend_operation(operation: Callable[[], _Result]) -> _Result:
        try:
            return operation()
        except Exception:
            raise CredentialStorageError("Credential storage is unavailable.") from None
