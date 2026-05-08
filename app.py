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
import os
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
from inbox_verifier import InboxConfig, test_login as imap_test_login
import google_signin


app = Flask(__name__)


class Job:
    def __init__(
        self,
        form_urls: list[str],
        emails: list[str],
        delay: float,
        headless: bool,
        proxy: "ProxyConfig | None" = None,
        max_retries: int = 2,
        concurrency: int = 1,
        send_me_copy: bool = True,
        inbox: "InboxConfig | None" = None,
        inbox_timeout: float = 120.0,
        use_signed_in_profile: bool = False,
    ):
        self.id = uuid.uuid4().hex
        self.form_urls = form_urls
        self.emails = emails
        self.delay = delay
        self.headless = headless
        self.proxy = proxy
        self.max_retries = max_retries
        self.concurrency = max(1, concurrency)
        self.send_me_copy = send_me_copy
        self.inbox = inbox
        self.inbox_timeout = inbox_timeout
        self.use_signed_in_profile = use_signed_in_profile
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
    form_index = (idx - 1) % len(job.form_urls)
    form_url = job.form_urls[form_index]
    form_label = f"form {form_index + 1}"

    job.emit(
        "progress",
        index=idx,
        total=total,
        email=email,
        form_index=form_index + 1,
        form_total=len(job.form_urls),
        status="starting",
    )

    def log(msg: str, _email=email, _idx=idx, _label=form_label) -> None:
        job.emit(
            "log",
            index=_idx,
            total=total,
            email=_email,
            form_label=_label,
            message=msg,
        )

    log(f"Routed to {form_label}: {form_url}")

    try:
        result = submit_form(
            form_url=form_url,
            email=email,
            logger=log,
            headless=job.headless,
            proxy=job.proxy,
            max_retries=job.max_retries,
            send_me_copy=job.send_me_copy,
            inbox=job.inbox,
            inbox_timeout=job.inbox_timeout,
            use_signed_in_profile=job.use_signed_in_profile,
        )
    except Exception as exc:
        log(f"Unhandled error: {exc}")
        job.emit(
            "result",
            index=idx,
            total=total,
            email=email,
            form_index=form_index + 1,
            success=False,
            message=str(exc) or exc.__class__.__name__,
        )
        return False

    job.emit(
        "result",
        index=idx,
        total=total,
        email=email,
        form_index=form_index + 1,
        success=result.success,
        message=result.message,
    )
    return result.success


def _run_job(job: Job) -> None:
    total = len(job.emails)
    job.emit(
        "start",
        total=total,
        form_urls=job.form_urls,
        form_count=len(job.form_urls),
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


def _parse_inbox(d: dict) -> tuple["InboxConfig | None", float]:
    if not d or not d.get("enabled"):
        return None, 120.0
    username = (d.get("username") or os.getenv("IMAP_USERNAME") or "").strip()
    password = d.get("password") or os.getenv("IMAP_PASSWORD") or ""
    cfg = InboxConfig(
        host=(d.get("host") or "imap.gmail.com").strip(),
        port=int(d.get("port") or 993),
        username=username,
        password=password,
        use_ssl=bool(d.get("use_ssl", True)),
        mailbox=(d.get("mailbox") or "INBOX").strip(),
    )
    timeout = float(d.get("timeout") or 120)
    return cfg, max(15.0, min(timeout, 600.0))


@app.get("/")
def index() -> str:
    return render_template(
        "index.html",
        default_imap_username=os.getenv("IMAP_USERNAME", ""),
        default_imap_password_set=bool(os.getenv("IMAP_PASSWORD")),
    )


@app.get("/api/signin/status")
def api_signin_status():
    return jsonify({
        "active": google_signin.is_signin_active(),
        "has_profile": google_signin.has_master_profile(),
        "email": google_signin.saved_email(),
    })


@app.post("/api/signin/start")
def api_signin_start():
    data = request.get_json(force=True, silent=True) or {}
    proxy_data = data.get("proxy") or {}
    proxy_cfg: ProxyConfig | None = None
    if proxy_data.get("enabled"):
        host = (proxy_data.get("host") or "").strip()
        port_raw = str(proxy_data.get("port") or "").strip()
        if host and port_raw.isdigit():
            proxy_cfg = ProxyConfig(
                host=host,
                port=int(port_raw),
                username=(proxy_data.get("username") or "").strip(),
                password=(proxy_data.get("password") or ""),
                scheme=(proxy_data.get("scheme") or "http").strip() or "http",
            )
    try:
        google_signin.start_signin(proxy=proxy_cfg)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({
        "ok": True,
        "message": (
            "Chrome opened on the server. Sign in with your Google "
            "account in that window, then click 'Mark sign-in complete'."
        ),
    })


@app.post("/api/signin/finish")
def api_signin_finish():
    try:
        email = google_signin.finish_signin()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "email": email or ""})


@app.post("/api/signin/cancel")
def api_signin_cancel():
    google_signin.cancel_signin()
    return jsonify({"ok": True})


@app.post("/api/signin/clear")
def api_signin_clear():
    google_signin.clear_master_profile()
    return jsonify({"ok": True})


@app.post("/api/test_inbox")
def api_test_inbox():
    data = request.get_json(force=True, silent=True) or {}
    cfg, _ = _parse_inbox({**data, "enabled": True})
    if cfg is None or not cfg.is_configured:
        return jsonify({"ok": False, "error": "Username and password required"}), 400
    try:
        imap_test_login(cfg)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200
    return jsonify({"ok": True, "message": f"Logged in to {cfg.username} OK"})


@app.post("/api/submit")
def api_submit():
    data = request.get_json(force=True, silent=True) or {}
    raw_form_urls = data.get("form_urls")
    if not raw_form_urls:
        # backwards-compat: single form_url field
        single = (data.get("form_url") or "").strip()
        raw_form_urls = single
    if isinstance(raw_form_urls, list):
        form_urls = [str(u).strip() for u in raw_form_urls if str(u).strip()]
    else:
        form_urls = [
            u.strip()
            for u in str(raw_form_urls).replace(",", "\n").splitlines()
            if u.strip()
        ]
    raw_emails = data.get("emails") or ""
    delay = float(data.get("delay") or 0)
    headless = bool(data.get("headless", False))

    if not form_urls:
        return jsonify({"error": "Provide at least one Google Form URL"}), 400
    bad = [u for u in form_urls if "docs.google.com/forms" not in u]
    if bad:
        return jsonify({
            "error": f"Not a Google Forms URL: {bad[0]}"
        }), 400

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

    send_me_copy = bool(data.get("send_me_copy", True))
    use_signed_in_profile = bool(data.get("use_signed_in_profile", False))
    if use_signed_in_profile and not google_signin.has_master_profile():
        return jsonify({
            "error": (
                "Use signed-in profile is on but no Google account is "
                "signed in yet. Click 'Sign in with Google' first."
            )
        }), 400

    inbox_cfg, inbox_timeout = _parse_inbox(data.get("inbox") or {})

    job = Job(
        form_urls=form_urls,
        emails=emails,
        delay=delay,
        headless=headless,
        proxy=proxy_cfg,
        max_retries=max_retries,
        concurrency=concurrency,
        send_me_copy=send_me_copy,
        inbox=inbox_cfg,
        inbox_timeout=inbox_timeout,
        use_signed_in_profile=use_signed_in_profile,
    )
    with JOBS_LOCK:
        JOBS[job.id] = job

    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return jsonify({
        "job_id": job.id,
        "count": len(emails),
        "form_count": len(form_urls),
    })


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
