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
- Submit any number of emails in a batch (one per line or comma-separated)
- Optional delay between submissions to avoid rate-limits
- Optional headless toggle (useful for debugging)
- Live progress streamed from the server via Server-Sent Events
- Audio-captcha bypass using Google's own free speech recognition API
- **Authenticated HTTP proxy support** — wires credentials into Chrome
  via a generated `webRequest.onAuthRequired` extension (works out of
  the box with providers like DataImpulse, Bright Data, etc.). Both
  the form browser and the captcha-audio download go through the proxy.

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
- **Headless** – untick to watch the browser actually solve the captcha.
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
