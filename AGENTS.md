# AGENTS.md

## Cursor Cloud specific instructions

### Overview

This is a single-service Python Flask app (Google Form Submitter with audio reCAPTCHA bypass). See `README.md` for full project description and layout.

### Running the app

```bash
python3 app.py
```

Starts on `http://localhost:5000`. No database or external services needed — everything runs in-process.

### System dependencies

The VM already has **Google Chrome**, **ffmpeg**, and **Python 3.12** pre-installed. Selenium 4 auto-manages ChromeDriver — no manual install needed.

### Key gotchas

- Python packages install to `~/.local` (user install) since the system site-packages is read-only. This is fine — `python3` picks them up automatically.
- Selenium launches a real Chrome instance per form submission. In headless mode (`--headless=new`), this works without a display server.
- There are no automated tests, no linter config, and no build step in this project. The codebase is pure Python + vanilla HTML/CSS/JS with no transpilation.
- The app uses Server-Sent Events (SSE) for live progress. Test the API flow with: `POST /api/submit` to create a job, then `GET /api/stream/<job_id>` for the event stream.
- Actual form submission requires a real Google Form URL with an email field and reCAPTCHA. Fake URLs will start the pipeline (Selenium launch, Chrome navigation) but fail at the form-loading step — this is expected and useful for verifying the infrastructure works.
