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
//  IMPORTANT: do NOT add `using` directives at the top of the C#
//  code block — ZennoPoster wraps your code as a method body and
//  the C# compiler will treat `using System;` as a using-statement,
//  giving CS1003/CS1026 "(' expected". The required namespaces are
//  already imported by ZennoPoster.
// =====================================================================
//
// >>> PASTE FROM HERE <<<

// ----------------- inline fallbacks -------------------------------
// If you don't want to set Default values in Project Maker (or mark
// the variables as "Input"), just paste your real values into the
// three constants below. They are used ONLY when the matching
// project variable is empty, so the same script works either way.
//
// Why this matters: ZennoPoster runtime starts every task from each
// variable's "Default value" (saved in the .zp file). What you type
// into the live "Value" column in Project Maker is NOT saved — so
// the script sees an empty string when ZennoPoster runs it.
const string DEFAULT_FORM_URL  = "";
const string DEFAULT_EMAIL     = "";
const string DEFAULT_WIT_TOKEN = "";
// ------------------------------------------------------------------

string formUrl  = project.Variables["FORM_URL"].Value;
string email    = project.Variables["EMAIL"].Value;
string witToken = project.Variables["WIT_TOKEN"].Value;

if (string.IsNullOrWhiteSpace(formUrl))  formUrl  = DEFAULT_FORM_URL;
if (string.IsNullOrWhiteSpace(email))    email    = DEFAULT_EMAIL;
if (string.IsNullOrWhiteSpace(witToken)) witToken = DEFAULT_WIT_TOKEN;

if (string.IsNullOrWhiteSpace(formUrl))
    throw new Exception(
        "FORM_URL is empty. Set its 'Default value' in Project Maker's " +
        "Variables tab (NOT the live 'Value' column — that doesn't save), " +
        "OR paste it into DEFAULT_FORM_URL at the top of this code block.");
if (string.IsNullOrWhiteSpace(email))
    throw new Exception(
        "EMAIL is empty. Either set its 'Default value' in the Variables " +
        "tab, set it to be filled by a 'Get line from file' action before " +
        "this block, or paste a test address into DEFAULT_EMAIL.");
if (string.IsNullOrWhiteSpace(witToken))
    throw new Exception(
        "WIT_TOKEN is empty. Get a free Server Access Token at " +
        "https://wit.ai and set it as the 'Default value' of WIT_TOKEN " +
        "in the Variables tab, or paste it into DEFAULT_WIT_TOKEN.");

var tab = instance.ActiveTab;
var rng = new Random();

// --------------------------- step 1: open form ---------------------
project.SendInfoToLog("Opening form ...", true);
tab.Navigate(formUrl, "");
tab.WaitDownloading();
System.Threading.Thread.Sleep(rng.Next(1500, 2800));

// --------------------------- step 2: fill email --------------------
HtmlElement emailInput = tab.FindElementByAttribute("input", "type", "email", "regexp", 0);
if (emailInput.IsVoid)
    emailInput = tab.FindElementByAttribute("input", "type", "text", "regexp", 0);
if (emailInput.IsVoid) throw new Exception("Email input not found");

emailInput.Click();
System.Threading.Thread.Sleep(300);
emailInput.SetValue("", "Full", false, false);

for (int i = 0; i < email.Length; i++)
{
    string current = emailInput.GetAttribute("value");
    if (current == null) current = "";
    emailInput.SetValue(current + email[i], "Full", false, false);
    System.Threading.Thread.Sleep(rng.Next(60, 170));
}
project.SendInfoToLog("Filled email: " + email, true);
System.Threading.Thread.Sleep(rng.Next(800, 1600));

// --------------------------- step 3: click Submit ------------------
HtmlElement submitBtn = tab.FindElementByAttribute("div", "jsname", "M2UYVd", "regexp", 0);
if (submitBtn.IsVoid)
    submitBtn = tab.FindElementByAttribute("div", "role", "button", "regexp", 0);
if (submitBtn.IsVoid) throw new Exception("Submit button not found");
submitBtn.Click();
project.SendInfoToLog("Clicked Submit.", true);

// --------------------------- step 4: wait for captcha iframe -------
HtmlElement challengeIframe = tab.FindElementByAttribute("iframe", "title", "recaptcha challenge", "regexp", 0);
for (int i = 0; i < 30 && challengeIframe.IsVoid; i++)
{
    System.Threading.Thread.Sleep(500);
    challengeIframe = tab.FindElementByAttribute("iframe", "title", "recaptcha challenge", "regexp", 0);
}

if (challengeIframe.IsVoid)
{
    project.SendInfoToLog("No captcha challenge appeared — checking confirmation directly.", true);
}
else
{
    project.SendInfoToLog("Captcha popup detected — waiting 5s before clicking audio button ...", true);
    System.Threading.Thread.Sleep(5000);

    // ----------------------- step 5: switch to audio --------------
    HtmlElement audioBtn = tab.FindElementByAttribute("button", "id", "recaptcha-audio-button", "regexp", 0);
    if (audioBtn.IsVoid)
        audioBtn = tab.FindElementByAttribute("*", "id", "recaptcha-audio-button", "regexp", 0);
    if (audioBtn.IsVoid) throw new Exception("Audio button not found");
    audioBtn.Click();
    project.SendInfoToLog("Switched to audio challenge.", true);
    System.Threading.Thread.Sleep(1500);

    // ----------------------- step 6: get audio URL ----------------
    // If Google blocked the session ("Try again later"), the
    // audio-source element won't be present and we'll throw below
    // with a clear message — no need to scrape page HTML for it.
    HtmlElement tryAgainEl = tab.FindElementByAttribute(
        "div", "class", "rc-doscaptcha-header-text", "regexp", 0);
    if (!tryAgainEl.IsVoid)
        throw new Exception("Google flagged this session ('Try again later'). Slow down or change network.");

    HtmlElement audioSrc = tab.FindElementByAttribute("audio", "id", "audio-source", "regexp", 0);
    if (audioSrc.IsVoid)
        audioSrc = tab.FindElementByAttribute("source", "id", "audio-source", "regexp", 0);
    if (audioSrc.IsVoid) throw new Exception("Audio source element not found");

    string audioUrl = audioSrc.GetAttribute("src");
    project.SendInfoToLog("Downloading audio: " + audioUrl, true);

    // ----------------------- step 7: download mp3 -----------------
    string tempMp3 = System.IO.Path.Combine(
        System.IO.Path.GetTempPath(),
        "recap_" + Guid.NewGuid().ToString("N") + ".mp3");
    using (var wc = new System.Net.WebClient())
        wc.DownloadFile(audioUrl, tempMp3);

    // ----------------------- step 8: send to wit.ai ---------------
    // wit.ai accepts MP3 directly. Response is a stream of partial
    // JSON objects (\r\n-separated); keep the LAST "text" value.
    string transcript = "";
    try
    {
        var req = (System.Net.HttpWebRequest)System.Net.WebRequest.Create("https://api.wit.ai/speech?v=20240101");
        req.Method = "POST";
        req.Headers["Authorization"] = "Bearer " + witToken;
        req.ContentType = "audio/mpeg3";
        req.Timeout = 30000;

        byte[] audioBytes = System.IO.File.ReadAllBytes(tempMp3);
        req.ContentLength = audioBytes.Length;
        using (var rs = req.GetRequestStream())
            rs.Write(audioBytes, 0, audioBytes.Length);

        using (var resp = (System.Net.HttpWebResponse)req.GetResponse())
        using (var sr = new System.IO.StreamReader(resp.GetResponseStream(), System.Text.Encoding.UTF8))
        {
            string body = sr.ReadToEnd();
            var matches = System.Text.RegularExpressions.Regex.Matches(body, "\"text\"\\s*:\\s*\"([^\"]*)\"");
            for (int i = 0; i < matches.Count; i++)
            {
                string t = matches[i].Groups[1].Value;
                if (!string.IsNullOrWhiteSpace(t)) transcript = t;
            }
        }
    }
    finally
    {
        try { System.IO.File.Delete(tempMp3); } catch { }
    }

    if (string.IsNullOrWhiteSpace(transcript))
        throw new Exception("wit.ai transcription returned no text");

    transcript = transcript.Trim().ToLower();
    project.SendInfoToLog("Transcribed audio: '" + transcript + "'", true);

    // ----------------------- step 9: submit answer ----------------
    HtmlElement responseInput = tab.FindElementByAttribute("input", "id", "audio-response", "regexp", 0);
    if (responseInput.IsVoid) throw new Exception("audio-response input not found");

    responseInput.Click();
    System.Threading.Thread.Sleep(200);
    responseInput.SetValue(transcript, "Full", false, false);
    System.Threading.Thread.Sleep(rng.Next(400, 800));

    HtmlElement verifyBtn = tab.FindElementByAttribute("button", "id", "recaptcha-verify-button", "regexp", 0);
    if (!verifyBtn.IsVoid) verifyBtn.Click();
    project.SendInfoToLog("Submitted audio answer. Waiting 3s before form submit ...", true);
    System.Threading.Thread.Sleep(3000);
}

// --------------------------- step 10: wait for /formResponse -------
bool delivered = false;
for (int i = 0; i < 25; i++)
{
    string currentUrl = tab.URL;
    if (currentUrl != null && currentUrl.Contains("formResponse"))
    {
        delivered = true;
        break;
    }
    System.Threading.Thread.Sleep(1000);
}

if (!delivered)
{
    project.SendInfoToLog("No auto-submit detected — clicking Submit once more.", true);
    submitBtn = tab.FindElementByAttribute("div", "jsname", "M2UYVd", "regexp", 0);
    if (!submitBtn.IsVoid) submitBtn.Click();

    for (int i = 0; i < 25; i++)
    {
        string currentUrl = tab.URL;
        if (currentUrl != null && currentUrl.Contains("formResponse"))
        {
            delivered = true;
            break;
        }
        System.Threading.Thread.Sleep(1000);
    }
}

if (!delivered)
    throw new Exception(
        "Form did not redirect to /formResponse — submission was not " +
        "accepted (captcha rejected or form requires more fields).");

project.SendInfoToLog("SUCCESS: " + email + " -> " + tab.URL, true);
System.Threading.Thread.Sleep(rng.Next(2500, 4000));
return 0;
