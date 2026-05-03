"""
Flask web app that submits a Google Form (single email field) with a
reCAPTCHA v2 challenge. The captcha is solved via the audio challenge
using the approach from https://github.com/sarperavci/GoogleRecaptchaBypass.

A single-page UI lets the user provide:
  * Google Form URL (viewform link)
  * One or more emails (one per line, or comma-separated)
  * Optional delay between submissions

The backend runs each submission in a worker thread and streams progress
to the browser via Server-Sent Events.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import asdict
from typing import Dict

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from form_submitter import submit_form


app = Flask(__name__)


class Job:
    def __init__(self, form_url: str, emails: list[str], delay: float, headless: bool):
        self.id = uuid.uuid4().hex
        self.form_url = form_url
        self.emails = emails
        self.delay = delay
        self.headless = headless
        self.queue: "queue.Queue[dict]" = queue.Queue()
        self.done = False

    def emit(self, event: str, **data) -> None:
        payload = {"event": event, **data}
        self.queue.put(payload)


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _run_job(job: Job) -> None:
    total = len(job.emails)
    job.emit("start", total=total, form_url=job.form_url)

    success = 0
    failure = 0

    for idx, email in enumerate(job.emails, start=1):
        job.emit("progress", index=idx, total=total, email=email, status="starting")

        def log(msg: str, _email=email, _idx=idx) -> None:
            job.emit("log", index=_idx, total=total, email=_email, message=msg)

        result = submit_form(
            form_url=job.form_url,
            email=email,
            logger=log,
            headless=job.headless,
        )

        if result.success:
            success += 1
        else:
            failure += 1

        job.emit(
            "result",
            index=idx,
            total=total,
            email=email,
            success=result.success,
            message=result.message,
        )

        if idx < total and job.delay > 0:
            job.emit(
                "log",
                index=idx,
                total=total,
                email=email,
                message=f"Waiting {job.delay}s before next submission ...",
            )
            time.sleep(job.delay)

    job.emit("done", total=total, success=success, failure=failure)
    job.done = True


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.post("/api/submit")
def api_submit():
    data = request.get_json(force=True, silent=True) or {}
    form_url = (data.get("form_url") or "").strip()
    raw_emails = data.get("emails") or ""
    delay = float(data.get("delay") or 0)
    headless = bool(data.get("headless", True))

    if not form_url:
        return jsonify({"error": "form_url is required"}), 400
    if "docs.google.com/forms" not in form_url:
        return jsonify({"error": "form_url must be a Google Forms URL"}), 400

    emails = [
        e.strip()
        for e in raw_emails.replace(",", "\n").splitlines()
        if e.strip()
    ]
    if not emails:
        return jsonify({"error": "Provide at least one email"}), 400

    job = Job(form_url=form_url, emails=emails, delay=delay, headless=headless)
    with JOBS_LOCK:
        JOBS[job.id] = job

    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return jsonify({"job_id": job.id, "count": len(emails)})


@app.get("/api/stream/<job_id>")
def api_stream(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "unknown job"}), 404

    @stream_with_context
    def generate():
        yield f": connected\n\n"
        while True:
            try:
                payload = job.queue.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                if job.done and job.queue.empty():
                    break
                continue
            yield f"data: {json.dumps(payload)}\n\n"
            if payload.get("event") == "done":
                break

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
