"""Test the config-entry key store that keeps a display's SEC2 keys."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from opendisplay.sec2.errors import Sec2StorageError
from opendisplay.sec2.keystore import MemoryKeyStore
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.opendisplay.const import (
    CONF_DEVICE_UID,
    CONF_ENCRYPTION_KEY,
    CONF_KEY_CANDIDATES,
    DOMAIN,
)
from custom_components.opendisplay.keystore import EntryKeyStore, key_rotated_issue_id

from . import TEST_ADDRESS, TEST_TITLE

OLD = bytes.fromhex("11" * 16)
NEW = bytes.fromhex("22" * 16)
OTHER = bytes.fromhex("33" * 16)
UID = bytes.fromhex("0000441bf6c96440")
NAME = "OD4064C9"


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return an entry that holds a migrated 2.x key."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_ADDRESS,
        title=TEST_TITLE,
        data={CONF_ENCRYPTION_KEY: OLD.hex()},
    )
    entry.add_to_hass(hass)
    return entry


def test_the_stored_key_is_tried(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The entry's key is the confirmed one."""
    assert EntryKeyStore(hass, entry).keys_to_try(NAME) == [OLD]


def test_an_entry_without_a_key_has_none_to_try(hass: HomeAssistant) -> None:
    """A keyless entry offers nothing and knows no uid."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=TEST_ADDRESS, data={})
    entry.add_to_hass(hass)

    store = EntryKeyStore(hass, entry)

    assert store.keys_to_try(NAME) == []
    assert store.uid(NAME) is None


def test_a_staged_key_is_stored_before_it_is_confirmed(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Staging keeps both keys, so a rotation cut short can still connect."""
    store = EntryKeyStore(hass, entry)

    store.stage(NAME, NEW, "rotation")

    assert entry.data[CONF_ENCRYPTION_KEY] == OLD.hex()
    assert entry.data[CONF_KEY_CANDIDATES] == [NEW.hex()]
    assert store.keys_to_try(NAME) == [OLD, NEW]


def test_promotion_confirms_the_key_and_drops_the_others(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The key that opened a session replaces everything else; the uid is bound."""
    store = EntryKeyStore(hass, entry)
    store.stage(NAME, NEW, "rotation")

    store.promote(NAME, NEW, uid=UID)

    assert entry.data[CONF_ENCRYPTION_KEY] == NEW.hex()
    assert entry.data[CONF_KEY_CANDIDATES] == []
    assert entry.data[CONF_DEVICE_UID] == UID.hex()
    assert store.keys_to_try(NAME) == [NEW]
    assert store.uid(NAME) == UID


def test_candidates_are_tried_newest_first_and_at_most_three(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Up to three keys: the confirmed one, then the newest candidates."""
    store = EntryKeyStore(hass, entry)
    store.add_candidate(NAME, NEW, "user")
    store.add_candidate(NAME, OTHER, "user")
    store.add_candidate(NAME, bytes.fromhex("44" * 16), "user")

    assert store.keys_to_try(NAME) == [OLD, bytes.fromhex("44" * 16), OTHER]


def test_a_known_key_is_not_added_twice(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Adding the confirmed key or a known candidate changes nothing."""
    store = EntryKeyStore(hass, entry)
    store.add_candidate(NAME, OLD, "user")
    store.add_candidate(NAME, NEW, "user")
    store.add_candidate(NAME, NEW, "user")

    assert entry.data[CONF_KEY_CANDIDATES] == [NEW.hex()]


def test_forget_removes_every_key(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """After an unclaim the entry holds no key, candidate or uid."""
    store = EntryKeyStore(hass, entry)
    store.stage(NAME, NEW, "rotation")
    store.promote(NAME, NEW, uid=UID)

    store.forget(NAME)

    assert CONF_ENCRYPTION_KEY not in entry.data
    assert CONF_KEY_CANDIDATES not in entry.data
    assert CONF_DEVICE_UID not in entry.data


def test_a_stage_that_does_not_read_back_raises(
    hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library sends ROTATE_CONFIRM only after the new key is proven stored."""
    monkeypatch.setattr(hass.config_entries, "async_update_entry", lambda *a, **k: True)

    with pytest.raises(Sec2StorageError):
        EntryKeyStore(hass, entry).stage(NAME, NEW, "rotation")


def test_a_rotation_raises_a_repair_issue(
    hass: HomeAssistant, entry: MockConfigEntry, issue_registry: ir.IssueRegistry
) -> None:
    """Other apps that held the old key need the new one: tell the user once."""
    store = EntryKeyStore(hass, entry)
    store.stage(NAME, NEW, "rotation")
    store.promote(NAME, NEW, uid=UID)

    issue = issue_registry.async_get_issue(DOMAIN, key_rotated_issue_id(entry))
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": entry.entry_id}  # never the key itself


def test_confirming_the_same_key_raises_no_issue(
    hass: HomeAssistant, entry: MockConfigEntry, issue_registry: ir.IssueRegistry
) -> None:
    """An ordinary session with the stored key is not a rotation."""
    EntryKeyStore(hass, entry).promote(NAME, OLD, uid=UID)

    assert issue_registry.async_get_issue(DOMAIN, key_rotated_issue_id(entry)) is None


def test_the_store_follows_the_library_rules(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The same sequence gives the same keys as the library's reference store."""
    reference = MemoryKeyStore()
    reference.promote(NAME, OLD)
    store = EntryKeyStore(hass, entry)
    for target in (reference, store):
        target.add_candidate(NAME, OTHER, "user")
        target.stage(NAME, NEW, "rotation")

    assert store.keys_to_try(NAME) == reference.keys_to_try(NAME)

    for target in (reference, store):
        target.promote(NAME, NEW, uid=UID)

    assert store.keys_to_try(NAME) == reference.keys_to_try(NAME)
    assert store.uid(NAME) == reference.uid(NAME)


def test_forget_on_a_keyless_entry_is_harmless(hass: HomeAssistant) -> None:
    """Forgetting what was never stored does not raise."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=TEST_ADDRESS, data={})
    entry.add_to_hass(hass)

    EntryKeyStore(hass, entry).forget(NAME)

    assert entry.data == {}
