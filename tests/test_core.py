import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app import claude_academy, discovery, models, pipeline, youtube


VIDEO_ID = "dQw4w9WgXcQ"
GOLDCAST_URL = "https://anthropic.ondemand.goldcast.io/on-demand/94d88402-155c-4f2c-9621-8d6ef0cb754f"


class CoreBehaviourTests(unittest.TestCase):
    def test_job_and_batch_default_message_is_english(self):
        job = models.Job(
            id="x", url="u", language="auto", model_size="tiny",
            prefer_captions=True, output_dir="/tmp", save_srt=False,
        )
        batch = models.Batch(
            id="y", urls=[], language="auto", model_size="tiny",
            prefer_captions=True, output_dir="/tmp", save_srt=False,
        )
        self.assertEqual(job.message, "Waiting to start")
        self.assertEqual(batch.message, "Waiting to start")

    def test_prune_stale_jobs_evicts_only_old_finished_jobs(self):
        long_ago = time.time() - models.JOB_RETENTION_SECONDS - 60
        recent = time.time()
        stale_done = models.Job(
            id="stale-done", url="u", language="auto", model_size="tiny",
            prefer_captions=True, output_dir="/tmp", save_srt=False,
            status="done", created_at=long_ago,
        )
        stale_running = models.Job(
            id="stale-running", url="u", language="auto", model_size="tiny",
            prefer_captions=True, output_dir="/tmp", save_srt=False,
            status="running", created_at=long_ago,
        )
        recent_done = models.Job(
            id="recent-done", url="u", language="auto", model_size="tiny",
            prefer_captions=True, output_dir="/tmp", save_srt=False,
            status="done", created_at=recent,
        )
        models.jobs.update({job.id: job for job in (stale_done, stale_running, recent_done)})
        self.addCleanup(models.jobs.clear)

        models.prune_stale_jobs()

        self.assertNotIn("stale-done", models.jobs)
        self.assertIn("stale-running", models.jobs)
        self.assertIn("recent-done", models.jobs)


    def test_youtube_url_validation_requires_a_real_youtube_host(self):
        self.assertEqual(youtube.youtube_video_id(f"https://www.youtube.com/watch?v={VIDEO_ID}"), VIDEO_ID)
        self.assertEqual(youtube.youtube_video_id(f"https://youtu.be/{VIDEO_ID}"), VIDEO_ID)
        self.assertEqual(youtube.youtube_video_id(f"https://www.youtube.com/shorts/{VIDEO_ID}"), VIDEO_ID)
        self.assertIsNone(youtube.youtube_video_id(f"https://notyoutube.com/watch?v={VIDEO_ID}"))
        self.assertIsNone(youtube.youtube_video_id("https://www.youtube.com/watch?v=short"))

    def test_supported_media_urls_include_only_claude_academy_goldcast_recordings(self):
        self.assertEqual(youtube.media_id(GOLDCAST_URL), "goldcast:94d88402-155c-4f2c-9621-8d6ef0cb754f")
        self.assertIsNone(youtube.media_id("https://example.com/on-demand/94d88402-155c-4f2c-9621-8d6ef0cb754f"))
        self.assertEqual(
            youtube.parse_urls(f"{GOLDCAST_URL}\nhttps://www.youtube.com/watch?v={VIDEO_ID}"),
            [GOLDCAST_URL, f"https://www.youtube.com/watch?v={VIDEO_ID}"],
        )

    def test_claude_academy_catalog_keeps_supported_recordings_only(self):
        payload = {
            "webinars": [
                {"title": "Goldcast webinar", "recording": {"kind": "goldcast", "url": GOLDCAST_URL}},
                {"title": "YouTube webinar", "recording": {"kind": "youtube", "url": f"https://www.youtube.com/watch?v={VIDEO_ID}"}},
                {"title": "Unsupported provider", "recording": {"kind": "vimeo", "url": "https://vimeo.com/12345"}},
                {"title": "Upcoming webinar", "recording": None},
            ]
        }
        webinars = claude_academy.parse_claude_academy_webinars(payload)
        self.assertEqual([webinar["platform"] for webinar in webinars], ["Goldcast", "YouTube"])
        self.assertEqual([webinar["title"] for webinar in webinars], ["Goldcast webinar", "YouTube webinar"])

    def test_checkbox_only_enables_when_submitted(self):
        from app.utils import form_checkbox_enabled

        self.assertTrue(form_checkbox_enabled("on"))
        self.assertTrue(form_checkbox_enabled("true"))
        self.assertFalse(form_checkbox_enabled(""))
        self.assertFalse(form_checkbox_enabled(None))

    def test_youtube_operation_retries_a_broken_pipe(self):
        attempts = []
        retries = []

        def flaky_operation():
            attempts.append(True)
            if len(attempts) == 1:
                raise BrokenPipeError(32, "Broken pipe")
            return "ok"

        with patch.object(youtube.time, "sleep") as sleep:
            result = youtube.run_youtube_operation(
                flaky_operation,
                on_retry=lambda attempt, total, _exc: retries.append((attempt, total)),
            )

        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(retries, [(1, youtube.YOUTUBE_OPERATION_ATTEMPTS)])
        sleep.assert_called_once_with(1)

    def test_year_filter_means_calendar_year(self):
        now = datetime(2026, 1, 2, 12, 0)
        base = {"duration": 600, "is_short": False}
        self.assertTrue(discovery.search_video_matches({**base, "upload_date": "20260101"}, "all", "any", "year", "all", now))
        self.assertFalse(discovery.search_video_matches({**base, "upload_date": "20251231"}, "all", "any", "year", "all", now))

    def test_short_reviews_are_not_classified_as_shorts_by_duration_or_title(self):
        review = {"duration": 120, "title": "Review completa, no shorts", "is_short": False}
        self.assertTrue(discovery.search_video_matches(review, "videos", "any", "any", "all"))
        self.assertFalse(discovery.search_video_matches(review, "shorts", "any", "any", "all"))

    def test_conflicting_negations_and_numbers_are_not_deduplicated(self):
        from app.content import sentences_are_duplicate

        self.assertFalse(
            sentences_are_duplicate(
                "La bateria dura dos horas grabando en alta resolucion.",
                "La bateria no dura dos horas grabando en alta resolucion.",
            )
        )
        self.assertFalse(
            sentences_are_duplicate(
                "Esta camara permite grabar durante dos horas sin calentarse.",
                "Esta camara permite grabar durante tres horas sin calentarse.",
            )
        )

    def test_enrichment_keeps_provider_relevance_order(self):
        videos = [
            {"id": "first", "url": "https://example.test/slow", "title": "Primero", "is_short": False},
            {"id": "second", "url": "https://example.test/fast", "title": "Segundo", "is_short": False},
        ]

        def fake_metadata(url):
            if url.endswith("slow"):
                time.sleep(0.02)
                return {"title": "Primero enriquecido", "duration": 600}
            return {"title": "Segundo enriquecido", "duration": 600}

        with patch.object(discovery, "fetch_video_metadata_with_timeout", side_effect=fake_metadata):
            enriched = discovery.enrich_search_videos(videos)

        self.assertEqual([video["id"] for video in enriched], ["first", "second"])
        self.assertEqual([video["title"] for video in enriched], ["Primero enriquecido", "Segundo enriquecido"])

    def test_search_metadata_enrichment_limits_its_candidate_sample(self):
        videos = [
            {"id": str(index), "url": f"https://example.test/{index}", "title": str(index), "is_short": False}
            for index in range(5)
        ]
        calls = []

        def fake_metadata(url):
            calls.append(url)
            return {"title": url.rsplit("/", 1)[-1], "duration": 600}

        with patch.object(discovery, "fetch_video_metadata_with_timeout", side_effect=fake_metadata):
            enriched, stats = discovery.enrich_search_videos(
                videos,
                candidate_limit=2,
                time_budget_seconds=1,
                return_stats=True,
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(stats["attempted"], 2)
        self.assertEqual(stats["skipped"], 3)
        self.assertTrue(enriched[0]["metadata_available"])
        self.assertFalse(enriched[2].get("metadata_available", False))

    def test_search_metadata_enrichment_returns_when_its_deadline_expires(self):
        videos = [
            {"id": str(index), "url": f"https://example.test/{index}", "title": str(index), "is_short": False}
            for index in range(4)
        ]

        def slow_metadata(_url):
            time.sleep(0.08)
            return {"title": "Completo", "duration": 600}

        started = time.monotonic()
        with patch.object(discovery, "fetch_video_metadata_with_timeout", side_effect=slow_metadata):
            _, stats = discovery.enrich_search_videos(
                videos,
                candidate_limit=4,
                time_budget_seconds=0.01,
                return_stats=True,
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.06)
        self.assertEqual(stats["pending"], 4)
        time.sleep(0.1)

    def test_known_video_needs_an_index_before_it_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            expected = path / "Mismo titulo.txt"
            expected.write_text("Another transcript", encoding="utf-8")
            job = models.Job(
                id="job-test",
                url=f"https://www.youtube.com/watch?v={VIDEO_ID}",
                language="auto",
                model_size="small",
                prefer_captions=True,
                output_dir=directory,
                save_srt=False,
                skip_existing=True,
            )
            self.assertIsNone(pipeline.find_existing_transcript(job, "Mismo titulo", {"id": VIDEO_ID}))

            job.title = "Mismo titulo"
            job.transcript_path = str(expected)
            job.status = "done"
            models.append_single_job_index(job)
            self.assertEqual(pipeline.find_existing_transcript(job, "Mismo titulo", {"id": VIDEO_ID}), expected)

    def test_batch_state_is_valid_json_after_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "batch-state-test.json"
            batch = models.Batch(
                id="batch-test",
                urls=[f"https://www.youtube.com/watch?v={VIDEO_ID}"],
                language="auto",
                model_size="small",
                prefer_captions=True,
                output_dir=directory,
                save_srt=False,
                state_path=str(state_path),
            )
            models.persist_batch_state(batch)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["id"], "batch-test")

    def test_batch_worker_cannot_start_twice(self):
        started = threading.Event()
        release = threading.Event()

        def fake_process(_batch_id):
            started.set()
            release.wait(timeout=1)

        with patch.object(pipeline, "process_batch", side_effect=fake_process):
            self.assertTrue(pipeline.start_batch_worker("worker-test"))
            self.assertTrue(started.wait(timeout=1))
            self.assertFalse(pipeline.start_batch_worker("worker-test"))
            release.set()
            for _ in range(20):
                with models.batch_workers_lock:
                    still_running = "worker-test" in models.batch_workers
                if not still_running:
                    break
                time.sleep(0.01)
            self.assertFalse(still_running)


if __name__ == "__main__":
    unittest.main()
