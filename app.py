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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Dict

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from form_submitter import submit_form
from proxy_support import ProxyConfig


app = Flask(__name__)


class Job:
    def __init__(
        self,
        form_url: str,
        emails: list[str],
        delay: float,
        headless: bool,
        proxy: "ProxyConfig | None" = None,
        max_retries: int = 2,
        concurrency: int = 1,
    ):
        self.id = uuid.uuid4().hex
        self.form_url = form_url
        self.emails = emails
        self.delay = delay
        self.headless = headless
        self.proxy = proxy
        self.max_retries = max_retries
        self.concurrency = max(1, concurrency)
        self.queue: "queue.Queue[dict]" = queue.Queue()
        self.done = False

    def emit(self, event: str, **data) -> None:
        payload = {"event": event, **data}
        self.queue.put(payload)


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _submit_one(job: Job, idx: int, email: str) -> bool:
    """Worker that runs one email through submit_form and emits progress.
    Returns True on success."""
    total = len(job.emails)
    job.emit("progress", index=idx, total=total, email=email, status="starting")

    def log(msg: str, _email=email, _idx=idx) -> None:
        job.emit("log", index=_idx, total=total, email=_email, message=msg)

    try:
        result = submit_form(
            form_url=job.form_url,
            email=email,
            logger=log,
            headless=job.headless,
            proxy=job.proxy,
            max_retries=job.max_retries,
        )
    except Exception as exc:
        log(f"Unhandled error: {exc}")
        job.emit(
            "result",
            index=idx,
            total=total,
            email=email,
            success=False,
            message=str(exc) or exc.__class__.__name__,
        )
        return False

    job.emit(
        "result",
        index=idx,
        total=total,
        email=email,
        success=result.success,
        message=result.message,
    )
    return result.success


def _run_job(job: Job) -> None:
    total = len(job.emails)
    job.emit(
        "start",
        total=total,
        form_url=job.form_url,
        concurrency=job.concurrency,
    )

    if job.concurrency <= 1:
        success = failure = 0
        for idx, email in enumerate(job.emails, start=1):
            ok = _submit_one(job, idx, email)
            success += int(ok)
            failure += int(not ok)
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
        return

    stagger = job.delay if job.delay > 0 else 0.0
    success_count = 0
    failure_count = 0
    counts_lock = threading.Lock()

    def worker(idx: int, email: str) -> None:
        nonlocal success_count, failure_count
        ok = _submit_one(job, idx, email)
        with counts_lock:
            if ok:
                success_count += 1
            else:
                failure_count += 1

    with ThreadPoolExecutor(
        max_workers=job.concurrency,
        thread_name_prefix=f"submit-{job.id[:6]}",
    ) as pool:
        futures = []
        for i, email in enumerate(job.emails, start=1):
            if i > 1 and stagger > 0:
                time.sleep(stagger)
            futures.append(pool.submit(worker, i, email))
        for f in futures:
            f.result()

    job.emit(
        "done", total=total, success=success_count, failure=failure_count
    )
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
    headless = bool(data.get("headless", False))

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

    proxy_cfg: ProxyConfig | None = None
    proxy_data = data.get("proxy") or {}
    if proxy_data and proxy_data.get("enabled"):
        host = (proxy_data.get("host") or "").strip()
        port_raw = str(proxy_data.get("port") or "").strip()
        if not host or not port_raw:
            return jsonify({"error": "Proxy host and port are required when enabled"}), 400
        try:
            port = int(port_raw)
        except ValueError:
            return jsonify({"error": "Proxy port must be an integer"}), 400
        proxy_cfg = ProxyConfig(
            host=host,
            port=port,
            username=(proxy_data.get("username") or "").strip(),
            password=(proxy_data.get("password") or ""),
            scheme=(proxy_data.get("scheme") or "http").strip() or "http",
        )

    try:
        max_retries = int(data.get("max_retries", 2))
    except (TypeError, ValueError):
        max_retries = 2
    max_retries = max(0, min(max_retries, 10))

    try:
        concurrency = int(data.get("concurrency", 1))
    except (TypeError, ValueError):
        concurrency = 1
    concurrency = max(1, min(concurrency, 20))

    job = Job(
        form_url=form_url,
        emails=emails,
        delay=delay,
        headless=headless,
        proxy=proxy_cfg,
        max_retries=max_retries,
        concurrency=concurrency,
    )
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
