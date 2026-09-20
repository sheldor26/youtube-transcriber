# YouTube Transcriber

A local web app for saving YouTube video transcripts as plain text. It uses existing YouTube captions first and falls back to local Whisper transcription when captions are unavailable.

## Features

- Transcribe one YouTube video or a batch of links.
- Extract every video link from a channel, including oldest, newest, most-viewed, and most-liked selections.
- Search YouTube with filters for type, duration, upload date, captions, and ordering.
- Keep each topic in its own output subfolder.
- Save TXT by default, with optional SRT output.
- Skip videos that have already been transcribed.
- Resume interrupted batches from their state file.
- Create a local, extractive consolidated summary for a completed batch.
- Retry short-lived YouTube connection failures automatically.

## Requirements

- Python 3.10 or newer.
- `ffmpeg` available on your system for Whisper audio fallback.
- An internet connection to read public YouTube metadata and media.

Age-restricted, private, removed, or sign-in-only videos may not be available to the application.

## Install

```bash
git clone https://github.com/YOUR-USERNAME/youtube-transcriber.git
cd youtube-transcriber
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Run

```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

## How transcription works

1. The app checks for manual YouTube captions.
2. It then checks for automatic YouTube captions.
3. If no usable captions are available, it downloads the audio and runs Whisper locally.

The first Whisper job for a model downloads that model. `small` is a good accuracy-oriented default; `tiny` is best for quick drafts.

## Batch output

Each batch writes the following files to the selected output folder:

- `batch-index-<id>.csv`: a batch-specific record of every result.
- `transcription-index.csv`: a reusable index used to identify existing transcripts.
- `batch-state-<id>.json`: state needed to resume a batch after an interruption.
- `consolidated-summary-<id>.txt`: optional extractive summary, when enabled.

The summary is generated locally from transcript text. It is a starting point for review, not a statement of fact or semantic consensus.

## Test

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

Tests do not download videos or run Whisper.

## License

Released under the [MIT License](LICENSE).
