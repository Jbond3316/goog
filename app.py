import os
import threading
import time
import uuid
from flask import Flask, render_template, request, jsonify
from DrissionPage import ChromiumPage, ChromiumOptions
from RecaptchaSolver import RecaptchaSolver

app = Flask(__name__)

# Stores job status: job_id -> {"status": ..., "message": ...}
jobs: dict = {}


def _find_chrome() -> str:
    """Return the path to the Chrome/Chromium binary available on this system."""
    candidates = [
        "/usr/local/bin/google-chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return "google-chrome"  # fall back and let the OS resolve it


def run_form_submission(job_id: str, form_url: str, email: str) -> None:
    """Background thread: open the Google Form, fill email, solve reCAPTCHA, submit."""
    jobs[job_id] = {"status": "running", "message": "Starting browser…"}

    try:
        options = ChromiumOptions()
        options.set_browser_path(_find_chrome())
        # Each job gets its own free debugging port — avoids the 404 handshake error
        options.auto_port()
        options.set_argument("--no-sandbox")
        options.set_argument("--disable-dev-shm-usage")
        options.set_argument("--disable-gpu")
        options.set_argument("--headless=new")
        options.set_argument("--window-size=1280,800")
        options.set_argument("--autoplay-policy=no-user-gesture-required")
        # Suppress "DevTools listening on..." noise
        options.set_argument("--log-level=3")

        driver = ChromiumPage(addr_or_opts=options)

        try:
            jobs[job_id]["message"] = "Opening Google Form…"
            driver.get(form_url)
            time.sleep(2)

            # Fill the email field — try several selector strategies Google Forms uses
            jobs[job_id]["message"] = "Filling email field…"
            email_input = (
                driver.ele("xpath://input[@type='email']", timeout=5)
                or driver.ele("xpath://input[contains(@aria-label,'mail')]", timeout=3)
                or driver.ele("xpath://input[contains(@aria-label,'Email')]", timeout=3)
                or driver.ele("xpath://input[@jsname]", timeout=3)
            )
            if not email_input:
                raise Exception("Could not locate the email input field.")
            email_input.clear()
            email_input.input(email)
            time.sleep(0.5)

            # Solve reCAPTCHA
            jobs[job_id]["message"] = "Solving reCAPTCHA via audio challenge…"
            solver = RecaptchaSolver(driver)
            solver.solveCaptcha()

            # Click the Submit button — Google Forms uses several markup patterns
            jobs[job_id]["message"] = "Submitting form…"
            submit_btn = (
                driver.ele("xpath://div[@role='button' and .//span[text()='Submit']]", timeout=5)
                or driver.ele("xpath://div[@role='button' and .//span[contains(text(),'Submit')]]", timeout=3)
                or driver.ele("xpath://span[text()='Submit']", timeout=3)
            )
            if not submit_btn:
                raise Exception("Could not locate the Submit button.")
            submit_btn.click()
            time.sleep(2)

            # Confirm submission
            page_text = driver.ele("tag:body").text
            if "Your response has been recorded" in page_text or "Thanks" in page_text:
                jobs[job_id] = {"status": "success", "message": "Form submitted successfully!"}
            else:
                jobs[job_id] = {
                    "status": "success",
                    "message": "Form submitted (confirmation text not detected — please verify manually).",
                }

        finally:
            driver.quit()

    except Exception as exc:
        jobs[job_id] = {"status": "error", "message": str(exc)}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(force=True)
    form_url = (data.get("form_url") or "").strip()
    email = (data.get("email") or "").strip()

    if not form_url or not email:
        return jsonify({"error": "Both form_url and email are required."}), 400

    if "docs.google.com/forms" not in form_url:
        return jsonify({"error": "Please provide a valid Google Forms URL."}), 400

    job_id = str(uuid.uuid4())
    thread = threading.Thread(
        target=run_form_submission, args=(job_id, form_url, email), daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job ID."}), 404
    return jsonify(job)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
