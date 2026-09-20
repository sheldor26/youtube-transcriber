import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.library import LibraryStore


class ProjectsApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()
        self.store = LibraryStore(self.root / "library.sqlite3")
        self.library_patch = patch.object(main, "library", self.store)
        self.library_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.library_patch.stop()
        main.jobs.clear()
        main.batches.clear()
        self.temporary.cleanup()

    def test_project_import_and_editorial_export_use_only_selected_transcripts(self):
        transcript = self.transcripts / "Action 5 review.txt"
        transcript.write_text(
            "The camera stays cool during long sessions. The battery lasts two hours at 1080p.",
            encoding="utf-8",
        )
        create = self.client.post(
            "/api/projects",
            data={"name": "DJI Osmo Action 5 Pro", "aliases": "Action 5", "output_dir": str(self.transcripts)},
        )
        self.assertEqual(create.status_code, 200)
        project = create.json()["project"]
        project_id = project["id"]
        self.assertEqual(Path(project["output_dir"]), (self.transcripts / "DJI Osmo Action 5 Pro").resolve())
        self.assertTrue(Path(project["output_dir"]).is_dir())

        scan = self.client.post(f"/api/projects/{project_id}/scan", data={"transcriptions_dir": str(self.transcripts)})
        self.assertEqual(scan.status_code, 200)
        candidate = scan.json()["candidates"][0]

        imported = self.client.post(
            f"/api/projects/{project_id}/import",
            data={"transcriptions_dir": str(self.transcripts), "paths_json": json.dumps([candidate["path"]])},
        )
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()["added"], 1)

        editorial = self.client.post(
            "/api/editorial-material",
            data={"project_id": project_id, "max_words": "500"},
        )
        self.assertEqual(editorial.status_code, 200)
        material_path = Path(editorial.json()["path"])
        self.assertTrue(material_path.exists())
        self.assertIn("DJI Osmo Action 5 Pro", material_path.read_text(encoding="utf-8"))

    def test_batch_rejects_an_unknown_project_before_starting_a_worker(self):
        response = self.client.post(
            "/api/batches",
            data={
                "links_text": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "project_id": "missing-project",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not found", response.json()["detail"])

    def test_topic_and_project_batches_use_separate_subfolders(self):
        created = self.client.post(
            "/api/projects",
            data={"name": "DJI Osmo Pocket 3", "output_dir": str(self.transcripts)},
        )
        self.assertEqual(created.status_code, 200)
        project = created.json()["project"]

        with patch.object(main, "start_batch_worker", return_value=True):
            topic_batch = self.client.post(
                "/api/batches",
                data={
                    "links_text": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "output_dir": str(self.transcripts),
                    "topic_name": "Windows 11 Gaming",
                },
            )
            project_batch = self.client.post(
                "/api/batches",
                data={
                    "links_text": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "output_dir": str(self.root / "ignorada"),
                    "project_id": project["id"],
                },
            )

        self.assertEqual(topic_batch.status_code, 200)
        self.assertEqual(Path(topic_batch.json()["output_dir"]), (self.transcripts / "Windows 11 Gaming").resolve())
        self.assertTrue((self.transcripts / "Windows 11 Gaming").is_dir())
        self.assertEqual(project_batch.status_code, 200)
        self.assertEqual(project_batch.json()["output_dir"], project["output_dir"])

    def test_single_video_topic_uses_a_subfolder(self):
        with patch.object(main.threading, "Thread") as thread:
            response = self.client.post(
                "/api/jobs",
                data={
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "output_dir": str(self.transcripts),
                    "topic_name": "Windows 11 Gaming",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Path(response.json()["output_dir"]), (self.transcripts / "Windows 11 Gaming").resolve())
        self.assertTrue((self.transcripts / "Windows 11 Gaming").is_dir())
        thread.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
