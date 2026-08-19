from __future__ import annotations

import pytest

from broccoli_desktop.credentials import CredentialStorageError, CredentialStore


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
