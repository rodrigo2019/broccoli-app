from __future__ import annotations

import pytest

from broccoli_desktop.credentials import CredentialStorageError, CredentialStore
from tests.fakes import RealisticFakeKeyring


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.deleted: list[tuple[str, str]] = []

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.deleted.append((service_name, username))
        self.values.pop((service_name, username), None)


class FailingKeyring:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def get_password(self, service_name: str, username: str) -> str | None:
        raise RuntimeError(f"backend error: {self.secret}")

    def set_password(self, service_name: str, username: str, password: str) -> None:
        raise RuntimeError(f"backend error: {password}")

    def delete_password(self, service_name: str, username: str) -> None:
        raise RuntimeError(f"backend error: {self.secret}")


@pytest.fixture
def fake_keyring() -> FakeKeyring:
    return FakeKeyring()


def test_deleting_a_token_uses_the_fixed_service_and_account(fake_keyring: FakeKeyring) -> None:
    store = CredentialStore(fake_keyring)

    store.delete_token()

    assert fake_keyring.deleted == [("Broccoli Desktop", "api-token")]


def test_saving_a_token_strips_pasted_whitespace(fake_keyring: FakeKeyring) -> None:
    store = CredentialStore(fake_keyring)

    store.save_token("  candidate-token\n")

    assert fake_keyring.values == {("Broccoli Desktop", "api-token"): "candidate-token"}


def test_empty_token_is_rejected_without_exposing_its_value(fake_keyring: FakeKeyring) -> None:
    store = CredentialStore(fake_keyring)

    with pytest.raises(ValueError) as error:
        store.save_token("   ")

    assert "token" not in str(error.value).lower()
    assert fake_keyring.values == {}


def test_loading_a_token_uses_the_fixed_service_and_account(fake_keyring: FakeKeyring) -> None:
    fake_keyring.values[("Broccoli Desktop", "api-token")] = "candidate-token"

    assert CredentialStore(fake_keyring).load_token() == "candidate-token"


@pytest.mark.parametrize("operation", ["load", "save", "delete"])
def test_backend_failures_do_not_propagate_credential_material(operation: str) -> None:
    secret = "secret-that-must-not-escape"
    store = CredentialStore(FailingKeyring(secret))

    with pytest.raises(CredentialStorageError) as error:
        if operation == "load":
            store.load_token()
        elif operation == "save":
            store.save_token(secret)
        else:
            store.delete_token()

    assert str(error.value) == "Credential storage is unavailable."
    assert secret not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__


def test_the_proxy_password_is_stored_under_its_own_account_separate_from_the_token(
    fake_keyring: FakeKeyring,
) -> None:
    """A second credential in a product whose whole pitch is the Windows vault --
    it must not share an account with the API token, or clearing one could ever
    reach the other."""
    store = CredentialStore(fake_keyring)

    store.save_proxy_password("proxy-secret")

    assert fake_keyring.values == {("Broccoli Desktop", "proxy-password"): "proxy-secret"}
    assert ("Broccoli Desktop", "api-token") not in fake_keyring.values


def test_loading_a_proxy_password_uses_the_fixed_service_and_account(
    fake_keyring: FakeKeyring,
) -> None:
    fake_keyring.values[("Broccoli Desktop", "proxy-password")] = "proxy-secret"

    assert CredentialStore(fake_keyring).load_proxy_password() == "proxy-secret"


def test_loading_a_proxy_password_that_was_never_saved_returns_none(
    fake_keyring: FakeKeyring,
) -> None:
    assert CredentialStore(fake_keyring).load_proxy_password() is None


def test_deleting_a_proxy_password_uses_the_fixed_service_and_account(
    fake_keyring: FakeKeyring,
) -> None:
    store = CredentialStore(fake_keyring)
    store.save_proxy_password("proxy-secret")

    store.delete_proxy_password()

    assert fake_keyring.deleted == [("Broccoli Desktop", "proxy-password")]
    assert CredentialStore(fake_keyring).load_proxy_password() is None


def test_empty_proxy_password_is_rejected_without_exposing_its_value(
    fake_keyring: FakeKeyring,
) -> None:
    store = CredentialStore(fake_keyring)

    with pytest.raises(ValueError) as error:
        store.save_proxy_password("")

    assert "password" not in str(error.value).lower()
    assert fake_keyring.values == {}


@pytest.mark.parametrize("operation", ["load", "save", "delete"])
def test_proxy_password_backend_failures_do_not_propagate_credential_material(
    operation: str,
) -> None:
    secret = "proxy-secret-that-must-not-escape"
    store = CredentialStore(FailingKeyring(secret))

    with pytest.raises(CredentialStorageError) as error:
        if operation == "load":
            store.load_proxy_password()
        elif operation == "save":
            store.save_proxy_password(secret)
        else:
            store.delete_proxy_password()

    assert str(error.value) == "Credential storage is unavailable."
    assert secret not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__


def test_deleting_a_token_that_was_never_saved_does_not_raise() -> None:
    """The real Windows backend raises PasswordDeleteError here, unlike every
    lenient in-memory fake -- and logout, the invalid-token cleanup, and the
    background auth-failure handler all call delete_token() unconditionally,
    whether or not a token was ever actually stored."""
    keyring = RealisticFakeKeyring()
    store = CredentialStore(keyring)

    store.delete_token()  # must not raise

    assert keyring.values == {}


def test_deleting_a_proxy_password_that_was_never_saved_does_not_raise() -> None:
    """Disabling the proxy calls delete_proxy_password() unconditionally, and
    disabling is the default for everyone who never configured one -- this is
    the ordinary case, not an edge case."""
    keyring = RealisticFakeKeyring()
    store = CredentialStore(keyring)

    store.delete_proxy_password()  # must not raise

    assert keyring.values == {}


def test_deleting_a_token_that_does_exist_still_deletes_it() -> None:
    """The not-found tolerance must not become a silent no-op for the ordinary
    case where an entry genuinely needs removing."""
    keyring = RealisticFakeKeyring()
    keyring.set_password("Broccoli Desktop", "api-token", "candidate-token")
    store = CredentialStore(keyring)

    store.delete_token()

    assert keyring.values == {}
    assert keyring.deleted == [("Broccoli Desktop", "api-token")]


def test_a_genuine_backend_failure_on_delete_still_propagates() -> None:
    """Only "not found" is tolerated -- a real backend failure during delete
    (vault locked, service unavailable) must still surface as
    CredentialStorageError, not be swallowed alongside the not-found case."""
    store = CredentialStore(FailingKeyring("irrelevant"))

    with pytest.raises(CredentialStorageError):
        store.delete_token()
