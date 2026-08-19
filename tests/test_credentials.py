from __future__ import annotations

import pytest

from broccoli_desktop.credentials import CredentialStore


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
