# ZennoPoster — Google Form Submitter (audio reCAPTCHA)

Single-action ZennoPoster project that submits a Google Form (single
email field + reCAPTCHA v2) by solving the audio challenge.

No proxy. No IMAP. Just core form-fill + audio-bypass.

## Files

- `SubmitForm.cs` — paste the body of this file into a single
  **Custom code (C#)** action block in your ZennoPoster project.

## Project setup

1. **Project Maker** → File → New → Project type **HTTP/Browser**.
2. **Project settings** → Browser type: **Chromium**.
3. **Variables tab**, add three variables:

   | Name        | Value                                          |
   |-------------|------------------------------------------------|
   | `FORM_URL`  | `https://docs.google.com/forms/d/e/.../viewform` |
   | `EMAIL`     | the email to submit (or bind to a list)        |
   | `WIT_TOKEN` | your wit.ai *Server Access Token*              |

4. Add one action: **Add Action → Custom Action → Custom code (C#)**,
   paste the code from `SubmitForm.cs` (everything below the
   `>>> PASTE FROM HERE <<<` line).
5. Run.

## wit.ai token (free)

We use [wit.ai](https://wit.ai) for speech-to-text because it accepts
MP3 directly — no `ffmpeg`, no audio conversion needed.

1. Sign in with your Facebook / Meta account at <https://wit.ai>.
2. New App → name it anything → English → Open.
3. Settings (gear icon, top-left) → copy **Server Access Token**.
4. Paste into ZennoPoster's `WIT_TOKEN` variable.

The free tier handles way more requests than you'll hit submitting
forms.

## Batch submission

To loop over a list of emails:

1. Add **File processing → Get line from file** action.
   - File: `emails.txt` (one email per line)
   - Result: store in variable `EMAIL`
   - Mode: *Take and delete* (so retries don't re-submit)
2. Drag the `Custom code (C#)` action **after** the file action.
3. Right-click the file action → **Set this as restart point**.
4. In the main ZennoPoster window, set **Threads = 10** for parallel.

Each thread runs a fresh Chromium profile (ZennoPoster's "Save profile"
should be off), so cookies / fingerprints don't carry over.

## Important: variables disappear when run in ZennoPoster

**Symptom:** the project runs fine in Project Maker but ZennoPoster
shows `FORM_URL is empty` (or any other variable empty).

**Reason:** every variable in ZennoPoster has two columns — **Value**
(live runtime) and **Default value** (saved in the .zp file).
Project Maker shows both. ZennoPoster runtime only starts from the
**Default value**. Whatever you type into the live "Value" column
during development is *not* persisted to the .zp file.

**Fix — pick one:**

### A. Set Default value (best for fixed configuration)

1. Project Maker → **Variables** tab.
2. Click in the **Default value** column for each variable
   (`FORM_URL`, `WIT_TOKEN`).
3. Type the value there. **File → Save**.

For `EMAIL`, leave the default empty and feed it from a file:

1. Drop a **File processing → Get line from file** action *before*
   the C# block.
2. File: `emails.txt`, Mode: *Take and delete*, Result variable:
   `EMAIL`.
3. ZennoPoster threads = N → each thread takes one line and
   submits in parallel.

### B. Mark them as "Input" variables (per-task overrides)

1. Project Maker → Variables tab → right-click each variable →
   **Edit** → tick **"Display in input"** → OK → Save.
2. In ZennoPoster, when you add a task for this project, the
   right-hand panel exposes those three fields per task.

### C. Hardcode in the C# block (quickest)

The script has three `DEFAULT_*` constants at the top:

```csharp
const string DEFAULT_FORM_URL  = "";
const string DEFAULT_EMAIL     = "";
const string DEFAULT_WIT_TOKEN = "";
```

Paste your real values into them. They are used as fallbacks only
when the matching project variable is empty, so the same code works
in both Project Maker (with variables set) and ZennoPoster runtime
(picking up the constants).

## Important: do NOT add `using` directives

ZennoPoster's "Custom code (C#)" action wraps your code as the body of
a method. If you add `using System;` etc. at the top, the compiler
treats them as using-statements (resource blocks) and fails with:

```
CS1003: Syntax error, '(' expected
CS1026: ) expected
```

The required namespaces (`System`, `System.IO`, `System.Net`,
`System.Text`, `System.Text.RegularExpressions`, `System.Threading`,
`ZennoLab.CommandCenter`, `ZennoLab.InterfacesLibrary.ProjectModel`)
are imported by ZennoPoster automatically. The `SubmitForm.cs` body
uses fully-qualified names where needed.

## Troubleshooting

| Symptom                                            | Fix                                                                                                                  |
|----------------------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| `Email input not found`                            | The form has more than one field — the script finds the first `<input type=email\|text>`. Adjust the XPath if needed. |
| `Submit button not found`                          | Form is in a non-English language. Add a fallback `FindElementByAttribute("div", "role", "button", "regexp", 0)`.   |
| `Google flagged this session ('Try again later')` | Slow down (raise the inter-submit delay) or use a proxy. Audio bypass is rate-limited per IP.                        |
| `wit.ai transcription returned no text`            | Token wrong or expired. Test with `curl -H "Authorization: Bearer $WIT_TOKEN" https://api.wit.ai/apps`.              |
| `Form did not redirect to /formResponse`           | reCAPTCHA score too low. Add real human gestures (move mouse, scroll) or use a paid solver service.                  |

## Why wit.ai instead of Google's free Speech API?

The Python project in this repo uses Google's free speech endpoint via
the `SpeechRecognition` library, which requires the audio to be in
**FLAC** format — that needs `ffmpeg` or a NAudio FLAC encoder. wit.ai
takes MP3 directly with one HTTP POST, which is the cleanest approach
inside a single ZennoPoster C# block with no external binaries.

If you'd rather not use wit.ai, swap step 8 in `SubmitForm.cs` for:

- **2captcha / Anti-Captcha** — paid, ~$1 per 1000 captchas, solves
  reCAPTCHA directly without audio. ZennoPoster has built-in modules.
- **ffmpeg + Google speech-api/v2** — match the Python project's
  approach. Requires shipping `ffmpeg.exe` next to the project and
  shelling out via `Process.Start`.
