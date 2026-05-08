// =====================================================================
//  ZennoPoster — Google Form Submitter with Audio reCAPTCHA Bypass
//  No proxy, no IMAP. Pure C# action block.
// ---------------------------------------------------------------------
//  PROJECT VARIABLES (create them in the variables tab):
//    FORM_URL    Google Form viewform URL (the …/viewform link)
//    EMAIL       single email to submit (use a list + "Take" action
//                for batch runs)
//    WIT_TOKEN   wit.ai server access token (free, 1-min signup at
//                https://wit.ai → create an app → "Server Access
//                Token" under Settings)
//
//  PROJECT SETTINGS:
//    Browser type:           Chromium
//    "Receive HTTP requests": Disabled
//    "Save profile":          No (fresh profile per run)
//
//  HOW TO USE:
//    1. New ZennoPoster project (Project Maker → File → New).
//    2. Variables tab: add FORM_URL, EMAIL, WIT_TOKEN.
//    3. Drop a single block: Add Action → Custom Action → Custom
//       code (C#)  →  paste this whole file's body (everything
//       after the "PASTE FROM HERE" line below).
//    4. Set Browser → Chromium in project settings.
//    5. Run it. The block returns 0 on success and throws on
//       failure (ZennoPoster will mark the run as Bad).
//
//  OPTIONAL FOR BATCH:
//    - File processing → Get line from file (FILE = emails.txt) →
//      stores result in EMAIL → loop the C# block once per line.
//    - Set "Threads" in ZennoPoster main UI to 10 for parallel runs.
// =====================================================================
//
// >>> PASTE FROM HERE <<<

using System;
using System.IO;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using ZennoLab.CommandCenter;
using ZennoLab.InterfacesLibrary.ProjectModel;

// --------------------------- inputs --------------------------------
string formUrl  = project.Variables["FORM_URL"].Value;
string email    = project.Variables["EMAIL"].Value;
string witToken = project.Variables["WIT_TOKEN"].Value;

if (string.IsNullOrWhiteSpace(formUrl)) throw new Exception("FORM_URL is empty");
if (string.IsNullOrWhiteSpace(email))   throw new Exception("EMAIL is empty");
if (string.IsNullOrWhiteSpace(witToken)) throw new Exception("WIT_TOKEN is empty (get one at https://wit.ai)");

var tab = instance.ActiveTab;
var rng = new Random();

void Log(string msg) { project.SendInfoToLog(msg, true); }

// --------------------------- step 1: open form ---------------------
Log("Opening form ...");
tab.Navigate(formUrl, "");
tab.WaitDownloading();
Thread.Sleep(rng.Next(1500, 2800));

// --------------------------- step 2: fill email --------------------
HtmlElement emailInput = tab.FindElementByAttribute("input", "type", "email", "regexp", 0);
if (emailInput.IsVoid)
    emailInput = tab.FindElementByAttribute("input", "type", "text", "regexp", 0);
if (emailInput.IsVoid) throw new Exception("Email input not found");

emailInput.Click();
Thread.Sleep(300);
emailInput.SetValue("");

// human-ish typing: send char by char
foreach (char c in email)
{
    string current = emailInput.GetAttribute("value") ?? "";
    emailInput.SetValue(current + c);
    Thread.Sleep(rng.Next(60, 170));
}
Log("Filled email: " + email);
Thread.Sleep(rng.Next(800, 1600));

// --------------------------- step 3: click Submit ------------------
HtmlElement submitBtn = tab.FindElementByAttribute("div", "jsname", "M2UYVd", "regexp", 0);
if (submitBtn.IsVoid)
{
    // Fallback: any role=button containing "Submit"
    submitBtn = tab.FindElementByAttribute(
        "div", "role", "button", "regexp", 0);
}
if (submitBtn.IsVoid) throw new Exception("Submit button not found");
submitBtn.Click();
Log("Clicked Submit.");

// --------------------------- step 4: wait for captcha iframe -------
HtmlElement challengeIframe = HtmlElement.GetVoid();
for (int i = 0; i < 30; i++)
{
    challengeIframe = tab.FindElementByAttribute(
        "iframe", "title", "recaptcha challenge", "regexp", 0);
    if (!challengeIframe.IsVoid) break;
    Thread.Sleep(500);
}

if (challengeIframe.IsVoid)
{
    Log("No captcha challenge appeared — checking confirmation directly.");
}
else
{
    Log("Captcha popup detected — waiting 5s before clicking audio button ...");
    Thread.Sleep(5000);

    // ----------------------- step 5: switch to audio --------------
    HtmlElement audioBtn = tab.FindElementByAttribute(
        "button", "id", "recaptcha-audio-button", "regexp", 0);
    if (audioBtn.IsVoid)
    {
        // Some Chrome versions render it as a div role=button
        audioBtn = tab.FindElementByAttribute(
            "*", "id", "recaptcha-audio-button", "regexp", 0);
    }
    if (audioBtn.IsVoid) throw new Exception("Audio button not found");
    audioBtn.Click();
    Log("Switched to audio challenge.");
    Thread.Sleep(1500);

    // Bot-detection check
    string pageHtml = tab.GetMainSourceCode();
    if (pageHtml.Contains("Try again later"))
        throw new Exception("Google flagged this session ('Try again later'). Slow down or change network.");

    // ----------------------- step 6: get audio URL ----------------
    HtmlElement audioSrc = tab.FindElementByAttribute(
        "audio", "id", "audio-source", "regexp", 0);
    if (audioSrc.IsVoid)
        audioSrc = tab.FindElementByAttribute(
            "source", "id", "audio-source", "regexp", 0);
    if (audioSrc.IsVoid) throw new Exception("Audio source element not found");

    string audioUrl = audioSrc.GetAttribute("src");
    Log("Downloading audio: " + audioUrl);

    // ----------------------- step 7: download mp3 -----------------
    string tempMp3 = Path.Combine(
        Path.GetTempPath(),
        "recap_" + Guid.NewGuid().ToString("N") + ".mp3");
    using (var wc = new WebClient())
        wc.DownloadFile(audioUrl, tempMp3);

    // ----------------------- step 8: send to wit.ai ---------------
    // wit.ai accepts MP3 directly and returns a streamed response of
    // partial JSON objects separated by \r\n. We grab the LAST "text".
    string transcript = "";
    try
    {
        var req = (HttpWebRequest)WebRequest.Create(
            "https://api.wit.ai/speech?v=20240101");
        req.Method = "POST";
        req.Headers["Authorization"] = "Bearer " + witToken;
        req.ContentType = "audio/mpeg3";
        req.Timeout = 30000;

        byte[] audioBytes = File.ReadAllBytes(tempMp3);
        req.ContentLength = audioBytes.Length;
        using (var rs = req.GetRequestStream())
            rs.Write(audioBytes, 0, audioBytes.Length);

        using (var resp = (HttpWebResponse)req.GetResponse())
        using (var sr = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
        {
            string body = sr.ReadToEnd();
            // grab every "text":"…" and keep the last non-empty one
            var matches = Regex.Matches(body, @"""text""\s*:\s*""([^""]*)""");
            foreach (Match m in matches)
            {
                if (!string.IsNullOrWhiteSpace(m.Groups[1].Value))
                    transcript = m.Groups[1].Value;
            }
        }
    }
    finally
    {
        try { File.Delete(tempMp3); } catch { /* ignore */ }
    }

    if (string.IsNullOrWhiteSpace(transcript))
        throw new Exception("wit.ai transcription returned no text");

    transcript = transcript.Trim().ToLower();
    Log("Transcribed audio: '" + transcript + "'");

    // ----------------------- step 9: submit answer ----------------
    HtmlElement responseInput = tab.FindElementByAttribute(
        "input", "id", "audio-response", "regexp", 0);
    if (responseInput.IsVoid) throw new Exception("audio-response input not found");

    responseInput.Click();
    Thread.Sleep(200);
    responseInput.SetValue(transcript);
    Thread.Sleep(rng.Next(400, 800));

    HtmlElement verifyBtn = tab.FindElementByAttribute(
        "button", "id", "recaptcha-verify-button", "regexp", 0);
    if (!verifyBtn.IsVoid) verifyBtn.Click();
    Log("Submitted audio answer. Waiting 3s before form submit ...");
    Thread.Sleep(3000);
}

// --------------------------- step 10: wait for /formResponse -------
bool delivered = false;
for (int i = 0; i < 25; i++)
{
    if ((tab.URL ?? "").Contains("formResponse"))
    {
        delivered = true;
        break;
    }
    Thread.Sleep(1000);
}

if (!delivered)
{
    // Form didn't auto-submit after captcha; click Submit one more time.
    Log("No auto-submit detected — clicking Submit once more.");
    submitBtn = tab.FindElementByAttribute("div", "jsname", "M2UYVd", "regexp", 0);
    if (!submitBtn.IsVoid) submitBtn.Click();

    for (int i = 0; i < 25; i++)
    {
        if ((tab.URL ?? "").Contains("formResponse")) { delivered = true; break; }
        Thread.Sleep(1000);
    }
}

if (!delivered)
    throw new Exception(
        "Form did not redirect to /formResponse — submission was not " +
        "accepted (captcha rejected or form requires more fields).");

Log("SUCCESS: " + email + " → " + tab.URL);
Thread.Sleep(rng.Next(2500, 4000));
return 0;
