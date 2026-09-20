import csv
import tempfile
import unittest
from pathlib import Path

from app.library import LibraryStore


class LibraryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = LibraryStore(self.root / "library.sqlite3")
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def create_project(self, name="DJI Osmo Action 5 Pro"):
        return self.store.create_project(name, "Action 5; OA5", str(self.transcripts), "Ficha del fabricante")

    def test_database_schema_is_versioned(self):
        self.assertEqual(self.store.schema_version(), 1)

    def test_scan_uses_existing_index_without_changing_txt_files(self):
        transcript = self.transcripts / "Review Action 5.txt"
        original_text = "The battery performed better than expected during a long hike."
        transcript.write_text(original_text, encoding="utf-8")
        generated = self.transcripts / "consolidated-summary-test.txt"
        generated.write_text("No debe aparecer", encoding="utf-8")

        with (self.transcripts / "transcription-index.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["video_id", "title", "url", "source", "transcript_path"])
            writer.writeheader()
            writer.writerow(
                {
                    "video_id": "dQw4w9WgXcQ",
                    "title": "DJI Action 5 Review",
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "source": "Whisper local (small)",
                    "transcript_path": str(transcript),
                }
            )

        candidates = self.store.scan_directory(str(self.transcripts), "Action 5")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["title"], "DJI Action 5 Review")
        self.assertEqual(candidates[0]["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(transcript.read_text(encoding="utf-8"), original_text)

    def test_import_is_idempotent_and_scoped_to_a_project(self):
        one = self.transcripts / "Action 5 review.txt"
        two = self.transcripts / "Pocket 3 review.txt"
        one.write_text("An extensive Action 5 review with real-world testing.", encoding="utf-8")
        two.write_text("An extensive Pocket 3 review with real-world testing.", encoding="utf-8")
        action = self.create_project()
        pocket = self.create_project("DJI Osmo Pocket 3")

        first = self.store.import_paths(action["id"], str(self.transcripts), [str(one.resolve())], "Action 5")
        repeated = self.store.import_paths(action["id"], str(self.transcripts), [str(one.resolve())], "Action 5")
        second = self.store.import_paths(pocket["id"], str(self.transcripts), [str(two.resolve())], "Pocket 3")

        self.assertEqual(first["added"], 1)
        self.assertEqual(repeated["added"], 0)
        self.assertEqual(repeated["already_in_project"], 1)
        self.assertEqual(second["added"], 1)
        self.assertEqual([path.name for path in self.store.project_paths(action["id"])], [one.name])
        self.assertEqual([path.name for path in self.store.project_paths(pocket["id"])], [two.name])

    def test_import_rejects_paths_outside_the_last_scan(self):
        inside = self.transcripts / "inside.txt"
        inside.write_text("Inside content", encoding="utf-8")
        outside = self.root / "outside.txt"
        outside.write_text("Outside content", encoding="utf-8")
        project = self.create_project()

        with self.assertRaisesRegex(ValueError, "no longer in the scanned folder"):
            self.store.import_paths(project["id"], str(self.transcripts), [str(outside.resolve())])


if __name__ == "__main__":
    unittest.main()
