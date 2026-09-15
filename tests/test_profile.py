import json
import tempfile
import unittest
from pathlib import Path

from app.profile import ProfileStore, ProfileStoreError
from app.schemas import UserProfile


class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "profile.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_explicit_profile_round_trip(self) -> None:
        profile = UserProfile(
            name="Ada",
            email="ada@example.com",
            phone="555-0100",
            address="1 Example Street",
            preferences="Evening appointments",
        )
        self.assertEqual(ProfileStore(self.path).save(profile), profile)
        self.assertEqual(ProfileStore(self.path).load(), profile)

    def test_sensitive_profile_data_is_rejected(self) -> None:
        with self.assertRaises(ProfileStoreError):
            ProfileStore(self.path).save(
                UserProfile(preferences="my password is secret")
            )

    def test_missing_profile_is_empty(self) -> None:
        self.assertEqual(ProfileStore(self.path).load(), UserProfile())

    def test_invalid_profile_is_rejected(self) -> None:
        self.path.write_text(json.dumps({"profile": "invalid"}), encoding="utf-8")
        with self.assertRaises(ProfileStoreError):
            ProfileStore(self.path).load()
