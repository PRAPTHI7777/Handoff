import tempfile
import unittest
from pathlib import Path

from app.memory import MemoryStore, MemoryStoreError


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "memory.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_opaque_api_key_shaped_memory_is_rejected(self) -> None:
        with self.assertRaises(MemoryStoreError):
            MemoryStore(self.path).save("gsk_test_DUMMY_0123456789abcdef")

    def test_ordinary_preference_memory_is_saved(self) -> None:
        saved = MemoryStore(self.path).save("Prefers concise answers")
        self.assertEqual(saved["value"], "Prefers concise answers")
