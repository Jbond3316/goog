# FormBot — Google Form Auto-Submitter

A Flask web app that automatically submits a Google Form (email-only field) while bypassing reCAPTCHA v2 using the **audio challenge** approach from [sarperavci/GoogleRecaptchaBypass](https://github.com/sarperavci/GoogleRecaptchaBypass).

## How it works

1. User pastes a Google Form URL and their email into the web UI.
2. The Flask backend spawns a background thread that launches a headless Chromium browser via **DrissionPage**.
3. The browser navigates to the form and fills the email field.
4. **RecaptchaSolver** clicks the reCAPTCHA checkbox, switches to the **audio** challenge, downloads the MP3, converts it to WAV with **pydub/ffmpeg**, and transcribes it with **Google Speech Recognition**.
5. The transcribed answer is typed in and the form is submitted.
6. The frontend polls `/status/<job_id>` and shows live progress steps until completion.

## Requirements

- Python 3.9+
- Google Chrome / Chromium installed
- `ffmpeg` installed

```bash
# Ubuntu / Debian
sudo apt-get install -y ffmpeg chromium-browser
```

## Setup

```bash
pip install -r requirements.txt
```

## Running

```bash
python app.py
# Open http://localhost:5000
```

Set `PORT` environment variable to change the default port.

## Project structure

```
├── app.py              # Flask server + background submission logic
├── RecaptchaSolver.py  # Audio reCAPTCHA solver (DrissionPage)
├── templates/
│   └── index.html      # Single-page UI
├── requirements.txt
└── .env.example
```

## Notes

- Google may temporarily block IPs that solve many CAPTCHAs in quick succession. Use responsibly.
- The app runs Chromium in `--headless=new` mode; a real display is not required.
