# Google Form Submitter (audio reCAPTCHA)

A small Flask web app that submits a Google Form containing a single email
field plus an **"I'm not a robot"** reCAPTCHA v2 widget. The captcha is
solved using the audio challenge, ported from
[sarperavci/GoogleRecaptchaBypass](https://github.com/sarperavci/GoogleRecaptchaBypass)
(selenium branch) and adapted for use inside a Flask app.

> ⚠️ This project is provided for educational / research purposes. Only
> use it against forms and accounts you own or are authorised to submit
> to. Google may rate-limit or block your IP if you solve many captchas
> in a short period.

## Features

- Clean, modern single-page UI
- Multiple form URLs per batch with **round-robin pairing** between
  emails and forms (email 1 → form 1, email 2 → form 2, email 3 →
  form 1, …). Single-form runs still work — just paste one URL.
- Submit any number of emails in a batch (one per line or comma-separated)
- Optional delay between submissions to avoid rate-limits
- Optional headless toggle (useful for debugging)
- Live progress streamed from the server via Server-Sent Events
- **Two captcha-solving methods** — pick per run:
  1. **Audio bypass (free)** — downloads the reCAPTCHA audio challenge
     and transcribes it via Google's own free speech-recognition
     endpoint. No account needed.
  2. **[CapMonster.Cloud](https://capmonster.cloud) API** — paid, ~$0.6
     per 1000 captchas. Pre-solves the reCAPTCHA token via API and
     injects it before the form Submit click, so the challenge popup
     never appears. More reliable on rate-limited IPs. Reads
     `CAPMONSTER_API_KEY` from the environment as a default.
- **Authenticated HTTP proxy support** — wires credentials into Chrome
  via a generated `webRequest.onAuthRequired` extension (works out of
  the box with providers like DataImpulse, Bright Data, etc.). Both
  the form browser and the captcha-audio download go through the proxy.
- **One-time Google sign-in** — open Chrome on the server pointed at
  Google's sign-in page, log in manually once, and every subsequent
  parallel browser clones that signed-in profile. Useful when the
  form requires the respondent to be logged in. Sign-in is not
  proxied (so your real IP avoids "unusual sign-in activity"
  prompts), but submissions are.
- **IMAP delivery verification** — after Google accepts each form, log
  in to a Gmail / IMAP inbox via SSL and wait for the receipt email
  from `forms-receipts-noreply@google.com`. If the receipt never
  lands within the timeout, the submission is correctly marked
  **failed**. Catches silent drops where the response counter goes
  up but no email actually arrives. For Gmail, requires 2-Step
  Verification + a 16-character App Password
  (https://myaccount.google.com/apppasswords).
  Defaults can be set via `IMAP_USERNAME` / `IMAP_PASSWORD` env vars.

## How it works

1. The browser submits the Form URL + email list to `/api/submit`.
2. The Flask backend spawns a worker thread that, for each email:
   1. Boots a Chrome + Selenium session.
   2. Opens the form and fills in the email input.
   3. If a reCAPTCHA is present:
      - Clicks the "I'm not a robot" checkbox.
      - If the click alone doesn't pass, switches to the audio challenge.
      - Downloads the `.mp3`, converts it to `.wav` with `ffmpeg`/`pydub`,
        and transcribes it with `SpeechRecognition`'s Google recognizer.
      - Types the transcription and submits the audio response.
   3. Clicks **Submit** on the form.
3. The browser subscribes to `/api/stream/<job_id>` and shows live
   per-email progress.

## Requirements

- Python 3.10+
- Google Chrome / Chromium (recent version)
- `chromedriver` matching your Chrome version on `$PATH`
  (Selenium 4 will auto-manage it in most setups, but installing it
  manually is the most reliable)
- `ffmpeg` on `$PATH` (required by `pydub`)

### Installing system dependencies

On Debian / Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg chromium-browser chromium-driver
```

On macOS (Homebrew):

```bash
brew install --cask google-chrome
brew install ffmpeg chromedriver
```

## Install Python dependencies

```bash
python -m venv .venv
source .venv/bin/activate        # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

Then open <http://localhost:5000> in a browser.

Paste:

- **Google Form URL** – the `…/viewform` link.
- **Emails** – one per line, or comma-separated.
- **Delay** – seconds to wait between submissions.
- **Headless** – off by default. reCAPTCHA often blocks headless Chrome
  regardless of proxy; leave this off unless you're running on a server
  without a display and know your traffic handles reCAPTCHA headless.
- **Proxy** – tick *Route through HTTP proxy* and fill host / port /
  username / password. The inputs are pre-filled with DataImpulse
  gateway defaults; change as needed. When enabled, both Chrome and
  the captcha-audio download are routed through the proxy.

Click **Start submissions**.

## Project layout

```
.
├── app.py                 # Flask app + SSE job streaming
├── form_submitter.py      # Selenium: opens form, fills email, submits
├── recaptcha_solver.py    # Audio reCAPTCHA solver (ported from sarperavci)
├── requirements.txt
├── static/
│   ├── app.js
│   └── styles.css
└── templates/
    └── index.html
```

## Credits

- Audio captcha solver approach and XPaths adapted from
  [sarperavci/GoogleRecaptchaBypass](https://github.com/sarperavci/GoogleRecaptchaBypass).

## Disclaimer

This code is released as-is for educational purposes. Automating form
submissions or captcha solving may violate the Terms of Service of
Google and/or other platforms. You are responsible for how you use it.
