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

from form_submitter import submit_form, submit_form_chain
from proxy_support import ProxyConfig, parse_proxy_lines
from capmonster_solver import CapMonsterError, CapMonsterSolver
from human_behavior import HumanBehavior
from recaptcha_solver import test_wit_token
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
        proxy_pool: "list[ProxyConfig] | None" = None,
        max_retries: int = 2,
        concurrency: int = 1,
        send_me_copy: bool = True,
        use_signed_in_profile: bool = False,
        captcha_method: str = "audio",
        capmonster_api_key: str = "",
        human: "HumanBehavior | None" = None,
        speech_engine: str = "google",
        wit_token: str = "",
        verify_proxy_at_startup: bool = False,
        reuse_browser: bool = False,
        max_per_browser: int = 0,
    ):
        self.id = uuid.uuid4().hex
        self.form_urls = form_urls
        self.emails = emails
        self.delay = delay
        self.headless = headless
        self.proxy = proxy
        self.proxy_pool = proxy_pool or []
        self.max_retries = max_retries
        self.concurrency = max(1, concurrency)
        self.send_me_copy = send_me_copy
        self.use_signed_in_profile = use_signed_in_profile
        self.captcha_method = captcha_method
        self.capmonster_api_key = capmonster_api_key
        self.human = human
        self.speech_engine = speech_engine
        self.wit_token = wit_token
        self.verify_proxy_at_startup = verify_proxy_at_startup
        self.reuse_browser = reuse_browser
        self.max_per_browser = max(0, int(max_per_browser or 0))
        self.cancelled = False
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

    if job.proxy_pool:
        proxy_start_index = (idx - 1) % len(job.proxy_pool)
        proxy_for_log = job.proxy_pool[proxy_start_index]
    else:
        proxy_start_index = 0
        proxy_for_log = job.proxy

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
            proxy_pool=job.proxy_pool or None,
            proxy_start_index=proxy_start_index,
            max_retries=job.max_retries,
            send_me_copy=job.send_me_copy,
            use_signed_in_profile=job.use_signed_in_profile,
            captcha_method=job.captcha_method,
            capmonster_api_key=job.capmonster_api_key,
            human=job.human,
            speech_engine=job.speech_engine,
            wit_token=job.wit_token,
            verify_proxy_at_startup=job.verify_proxy_at_startup,
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


def _submit_chunk(
    job: Job,
    indices: "list[int]",
    chunk_emails: "list[str]",
    form_idx: "int | None" = None,
) -> "tuple[int, int]":
    """Run a chunk of emails through ONE form. If ``form_idx`` is set,
    every email in the chunk is submitted to ``job.form_urls[form_idx]``
    and every transition between emails uses 'Submit another response'.

    The chunk is split into "browser sessions" of ``job.max_per_browser``
    emails each (0 = unlimited = one session for the whole chunk). Each
    session is its own submit_form_chain call, which means a fresh
    Chrome / fresh fingerprint / fresh proxy TCP session every N emails.

    Emits per-email progress/log/result events. Returns
    (success_count, failure_count) for this chunk."""
    total = len(job.emails)

    starting_idx = indices[0]
    if job.proxy_pool:
        proxy_start_index = (starting_idx - 1) % len(job.proxy_pool)
    else:
        proxy_start_index = 0

    # Per-email metadata so progress events still match what the
    # non-batch path emits (form label + form index per email).
    def email_to_form_index(local_i: int) -> int:
        if form_idx is not None:
            return form_idx
        global_i = indices[local_i]
        return (global_i - 1) % len(job.form_urls)

    successes = 0
    failures = 0

    def chain_log(msg: str) -> None:
        # Chain-level lines (driver build, fingerprint, proxy verify) get
        # attached to the FIRST email in the chunk so the UI shows them.
        first_email = chunk_emails[0]
        first_idx = indices[0]
        job.emit(
            "log",
            index=first_idx,
            total=total,
            email=first_email,
            form_label=f"form {email_to_form_index(0) + 1}",
            message=msg,
        )

    def per_email_log(local_idx_oneindexed: int, email: str, msg: str) -> None:
        global_idx = indices[local_idx_oneindexed - 1]
        f_idx = (global_idx - 1) % len(job.form_urls)
        job.emit(
            "log",
            index=global_idx,
            total=total,
            email=email,
            form_label=f"form {f_idx + 1}",
            message=msg,
        )

    def emit_progress_for(local_idx_oneindexed: int, email: str) -> None:
        global_idx = indices[local_idx_oneindexed - 1]
        f_idx = (global_idx - 1) % len(job.form_urls)
        job.emit(
            "progress",
            index=global_idx,
            total=total,
            email=email,
            form_index=f_idx + 1,
            form_total=len(job.form_urls),
            status="starting",
        )

    # Build sessions: when max_per_browser is 0, the whole chunk runs
    # in one browser. Otherwise we split it into back-to-back sessions
    # of max_per_browser each — each session is its own
    # submit_form_chain call (= new browser, fresh fingerprint, fresh
    # proxy TCP session, fresh user-data-dir).
    if job.max_per_browser and job.max_per_browser > 0:
        session_starts = list(range(0, len(chunk_emails), job.max_per_browser))
    else:
        session_starts = [0]

    for session_no, start in enumerate(session_starts, start=1):
        if job.cancelled:
            break

        end = (
            start + job.max_per_browser
            if job.max_per_browser and job.max_per_browser > 0
            else len(chunk_emails)
        )
        sess_indices = indices[start:end]
        sess_emails = chunk_emails[start:end]
        if not sess_emails:
            continue

        if len(session_starts) > 1:
            chain_log(
                f"Browser session {session_no}/{len(session_starts)}: "
                f"emails {sess_indices[0]}..{sess_indices[-1]}"
            )

        # Closures bound per session — the inner chain hands us
        # session-local 1-based indices; we translate to global.
        def make_per_log(s_indices):
            def per_log(local_idx_oneindexed: int, email: str, msg: str) -> None:
                global_idx = s_indices[local_idx_oneindexed - 1]
                f_idx = (
                    form_idx if form_idx is not None
                    else (global_idx - 1) % len(job.form_urls)
                )
                job.emit(
                    "log",
                    index=global_idx,
                    total=total,
                    email=email,
                    form_label=f"form {f_idx + 1}",
                    message=msg,
                )
            return per_log

        def make_progress(s_indices):
            def emit_progress(local_idx_oneindexed: int, email: str) -> None:
                global_idx = s_indices[local_idx_oneindexed - 1]
                f_idx = (
                    form_idx if form_idx is not None
                    else (global_idx - 1) % len(job.form_urls)
                )
                job.emit(
                    "progress",
                    index=global_idx,
                    total=total,
                    email=email,
                    form_index=f_idx + 1,
                    form_total=len(job.form_urls),
                    status="starting",
                )
            return emit_progress

        def make_per_email_result(s_indices):
            def on_res(local_idx_oneindexed: int, email, result) -> None:
                nonlocal successes, failures
                global_idx = s_indices[local_idx_oneindexed - 1]
                f_idx = (
                    form_idx if form_idx is not None
                    else (global_idx - 1) % len(job.form_urls)
                )
                if result.success:
                    successes += 1
                else:
                    failures += 1
                job.emit(
                    "result",
                    index=global_idx,
                    total=total,
                    email=email,
                    form_index=f_idx + 1,
                    success=result.success,
                    message=result.message,
                )
            return on_res

        def make_email_to_form(form_idx_local, s_indices):
            def f(local_i: int) -> int:
                if form_idx_local is not None:
                    return form_idx_local
                global_i = s_indices[local_i]
                return (global_i - 1) % len(job.form_urls)
            return f

        per_log_cb = make_per_log(sess_indices)
        emit_progress_cb = make_progress(sess_indices)
        on_result_cb = make_per_email_result(sess_indices)
        e2f = make_email_to_form(form_idx, sess_indices)

        progress_emitted: "set[int]" = set()

        def make_on_log_with_progress(per_log, emit_progress, seen):
            def on_log_with_progress(idx: int, email: str, msg: str) -> None:
                if idx not in seen:
                    emit_progress(idx, email)
                    seen.add(idx)
                per_log(idx, email, msg)
            return on_log_with_progress

        on_log_cb = make_on_log_with_progress(
            per_log_cb, emit_progress_cb, progress_emitted
        )

        # Each session uses the next proxy in the pool to maximize
        # IP diversity across browser restarts.
        sess_proxy_start = (
            (proxy_start_index + (session_no - 1)) % len(job.proxy_pool)
            if job.proxy_pool else 0
        )

        # Track which session-local indices the chain emitted a
        # result for so on a chain crash we can mark only the
        # truly unprocessed emails as aborted.
        completed_local: "set[int]" = set()

        original_on_result = on_result_cb

        def on_res_tracking(local_idx_oneindexed: int, email, result, _orig=original_on_result):
            completed_local.add(local_idx_oneindexed)
            _orig(local_idx_oneindexed, email, result)

        try:
            submit_form_chain(
                form_urls=job.form_urls,
                emails=sess_emails,
                logger=chain_log,
                headless=job.headless,
                proxy=job.proxy,
                proxy_pool=job.proxy_pool or None,
                proxy_start_index=sess_proxy_start,
                send_me_copy=job.send_me_copy,
                use_signed_in_profile=job.use_signed_in_profile,
                captcha_method=job.captcha_method,
                capmonster_api_key=job.capmonster_api_key,
                human=job.human,
                speech_engine=job.speech_engine,
                wit_token=job.wit_token,
                verify_proxy_at_startup=job.verify_proxy_at_startup,
                on_log=on_log_cb,
                on_result=on_res_tracking,
                email_to_form_index=e2f,
                should_stop=lambda: job.cancelled,
            )
        except Exception as exc:
            # Mark any session emails that didn't get a result as
            # aborted. The next session still runs.
            for local_i in range(len(sess_emails)):
                if (local_i + 1) in completed_local:
                    continue
                global_idx = sess_indices[local_i]
                email = sess_emails[local_i]
                f_idx = (
                    form_idx if form_idx is not None
                    else (global_idx - 1) % len(job.form_urls)
                )
                job.emit(
                    "result",
                    index=global_idx,
                    total=total,
                    email=email,
                    form_index=f_idx + 1,
                    success=False,
                    message=f"Chain aborted: {exc}",
                )
                failures += 1

    return successes, failures


def _run_job(job: Job) -> None:
    total = len(job.emails)
    job.emit(
        "start",
        total=total,
        form_urls=job.form_urls,
        form_count=len(job.form_urls),
        concurrency=job.concurrency,
        reuse_browser=job.reuse_browser,
    )

    if job.reuse_browser:
        # Form-affinity partitioning: each browser handles emails for
        # ONE form. Emails go to forms round-robin (email i -> form
        # (i-1)%N_forms), then for each form's bucket of emails we
        # split them across the workers we've been allocated for that
        # form.
        n_forms = len(job.form_urls)
        n_workers = max(1, min(job.concurrency, len(job.emails)))

        # Group emails by form.
        form_groups: "list[list[tuple[int, str]]]" = [
            [] for _ in range(n_forms)
        ]
        for i, email in enumerate(job.emails, start=1):
            form_groups[(i - 1) % n_forms].append((i, email))

        # Allocate workers across forms: each form gets at least one
        # worker if it has emails; remaining workers go to the
        # form(s) with the most emails.
        workers_per_form = [1 if g else 0 for g in form_groups]
        forms_in_use = sum(1 for g in form_groups if g)
        remaining = max(0, n_workers - forms_in_use)
        # Hand remaining workers to the largest groups.
        order = sorted(
            (i for i in range(n_forms) if form_groups[i]),
            key=lambda i: -len(form_groups[i]),
        )
        idx = 0
        while remaining > 0 and order:
            workers_per_form[order[idx % len(order)]] += 1
            remaining -= 1
            idx += 1

        # Build chunks: for each form, slice its emails into
        # ``workers_per_form[fi]`` contiguous sub-chunks (one per
        # worker assigned to that form).
        chunks_meta: "list[tuple[int, list[int], list[str]]]" = []
        for fi, group in enumerate(form_groups):
            workers = workers_per_form[fi]
            if not group or workers <= 0:
                continue
            per = (len(group) + workers - 1) // workers
            for w in range(workers):
                sub = group[w * per : (w + 1) * per]
                if sub:
                    chunks_meta.append(
                        (fi, [g for g, _ in sub], [e for _, e in sub])
                    )

        successes_total = 0
        failures_total = 0
        counts_lock = threading.Lock()

        def chunk_worker(form_idx, w_indices, w_emails):
            nonlocal successes_total, failures_total
            s, f = _submit_chunk(
                job, w_indices, w_emails, form_idx=form_idx
            )
            with counts_lock:
                successes_total += s
                failures_total += f

        n_chunks = len(chunks_meta)
        if n_chunks <= 1:
            if n_chunks == 1:
                chunk_worker(*chunks_meta[0])
        else:
            stagger = job.delay if job.delay > 0 else 0.0
            with ThreadPoolExecutor(
                max_workers=n_chunks,
                thread_name_prefix=f"chain-{job.id[:6]}",
            ) as pool:
                futures = []
                for c_idx, meta in enumerate(chunks_meta):
                    if c_idx > 0 and stagger > 0:
                        time.sleep(stagger)
                    futures.append(pool.submit(chunk_worker, *meta))
                for fut in futures:
                    fut.result()

        job.emit(
            "done",
            total=total,
            success=successes_total,
            failure=failures_total,
        )
        job.done = True
        return

    def _emit_cancelled(idx: int, email: str) -> None:
        f_idx = (idx - 1) % len(job.form_urls)
        job.emit(
            "result",
            index=idx,
            total=total,
            email=email,
            form_index=f_idx + 1,
            success=False,
            message="Cancelled by user (stop button).",
        )

    if job.concurrency <= 1:
        success = failure = 0
        for idx, email in enumerate(job.emails, start=1):
            if job.cancelled:
                _emit_cancelled(idx, email)
                failure += 1
                continue
            ok = _submit_one(job, idx, email)
            success += int(ok)
            failure += int(not ok)
            if idx < total and job.delay > 0 and not job.cancelled:
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
        if job.cancelled:
            _emit_cancelled(idx, email)
            with counts_lock:
                failure_count += 1
            return
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
            if job.cancelled:
                # Don't even queue further emails after cancel; the
                # not-yet-launched ones will be marked cancelled in
                # the worker.
                pass
            elif i > 1 and stagger > 0:
                time.sleep(stagger)
            futures.append(pool.submit(worker, i, email))
        for f in futures:
            f.result()

    job.emit(
        "done", total=total, success=success_count, failure=failure_count
    )
    job.done = True


def _parse_human(d: dict) -> "HumanBehavior | None":
    if not d or not d.get("enabled"):
        return None
    def _i(key: str, default: int) -> int:
        try:
            return int(d.get(key) or default)
        except (TypeError, ValueError):
            return default
    return HumanBehavior(
        enabled=True,
        mouse_speed_min=_i("mouse_speed_min", 15),
        mouse_speed_max=_i("mouse_speed_max", 20),
        keyboard_delay_min=_i("keyboard_delay_min", 100),
        keyboard_delay_max=_i("keyboard_delay_max", 150),
        screen_width=_i("screen_width", 1920),
        screen_height=_i("screen_height", 1080),
    )


@app.post("/api/stop/<job_id>")
def api_stop(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "unknown job"}), 404
    job.cancelled = True
    job.emit(
        "log",
        index=0,
        total=len(job.emails),
        email="",
        message="Stop requested by user.",
    )
    return jsonify({"ok": True})


@app.get("/")
def index() -> str:
    return render_template(
        "index.html",
        default_capmonster_key_set=bool(os.getenv("CAPMONSTER_API_KEY")),
        default_wit_token_set=bool(os.getenv("WIT_TOKEN")),
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


@app.post("/api/test_wit")
def api_test_wit():
    data = request.get_json(force=True, silent=True) or {}
    token = (
        data.get("token")
        or os.getenv("WIT_TOKEN")
        or ""
    ).strip()
    if not token:
        return jsonify({"ok": False, "error": "Token is empty"}), 400
    try:
        test_wit_token(token)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200
    return jsonify({"ok": True, "message": "OK — wit.ai token accepted"})


@app.post("/api/test_capmonster")
def api_test_capmonster():
    data = request.get_json(force=True, silent=True) or {}
    api_key = (
        data.get("api_key")
        or os.getenv("CAPMONSTER_API_KEY")
        or ""
    ).strip()
    if not api_key:
        return jsonify({"ok": False, "error": "API key is empty"}), 400
    try:
        cm = CapMonsterSolver(api_key)
        balance = cm.get_balance()
    except CapMonsterError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 200
    return jsonify({
        "ok": True,
        "message": f"OK — balance ${balance:.4f}",
    })


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
    proxy_pool: list[ProxyConfig] = []
    proxy_data = data.get("proxy") or {}
    if proxy_data and proxy_data.get("enabled"):
        # Multi-proxy textarea takes priority when non-empty.
        list_text = proxy_data.get("list") or ""
        proxy_pool = parse_proxy_lines(list_text)

        if proxy_pool:
            pass  # use the pool; ignore the single-proxy fields
        else:
            host = (proxy_data.get("host") or "").strip()
            port_raw = str(proxy_data.get("port") or "").strip()
            if not host or not port_raw:
                return jsonify({
                    "error": (
                        "Proxy is enabled but neither a single host:port "
                        "nor a non-empty multi-proxy list was provided."
                    )
                }), 400
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

    captcha_method = (data.get("captcha_method") or "audio").strip().lower()
    if captcha_method not in ("audio", "capmonster"):
        return jsonify({
            "error": f"Unknown captcha_method {captcha_method!r}; "
                     "expected 'audio' or 'capmonster'."
        }), 400

    capmonster_api_key = (
        data.get("capmonster_api_key")
        or os.getenv("CAPMONSTER_API_KEY")
        or ""
    ).strip()
    if captcha_method == "capmonster" and not capmonster_api_key:
        return jsonify({
            "error": (
                "CapMonster.Cloud is selected but no API key was "
                "provided. Paste it into the UI or set "
                "CAPMONSTER_API_KEY in the environment."
            )
        }), 400

    speech_engine = (data.get("speech_engine") or "google").strip().lower()
    if speech_engine not in ("google", "wit"):
        return jsonify({
            "error": f"Unknown speech_engine {speech_engine!r}; "
                     "expected 'google' or 'wit'."
        }), 400
    wit_token = (
        data.get("wit_token")
        or os.getenv("WIT_TOKEN")
        or ""
    ).strip()
    if captcha_method == "audio" and speech_engine == "wit" and not wit_token:
        return jsonify({
            "error": (
                "wit.ai is selected but no token was provided. "
                "Get a Server Access Token at https://wit.ai (Settings) "
                "or set WIT_TOKEN in the environment."
            )
        }), 400

    verify_proxy_at_startup = bool(data.get("verify_proxy_at_startup", False))
    reuse_browser = bool(data.get("reuse_browser", False))
    try:
        max_per_browser = int(data.get("max_per_browser", 0))
    except (TypeError, ValueError):
        max_per_browser = 0
    max_per_browser = max(0, min(max_per_browser, 1000))

    human_cfg = _parse_human(data.get("human") or {})

    job = Job(
        form_urls=form_urls,
        emails=emails,
        delay=delay,
        headless=headless,
        proxy=proxy_cfg,
        proxy_pool=proxy_pool,
        max_retries=max_retries,
        concurrency=concurrency,
        send_me_copy=send_me_copy,
        use_signed_in_profile=use_signed_in_profile,
        captcha_method=captcha_method,
        capmonster_api_key=capmonster_api_key,
        human=human_cfg,
        speech_engine=speech_engine,
        wit_token=wit_token,
        verify_proxy_at_startup=verify_proxy_at_startup,
        reuse_browser=reuse_browser,
        max_per_browser=max_per_browser,
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
