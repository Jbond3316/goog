const form = document.getElementById("submit-form");
const goBtn = document.getElementById("go");
const imapTestBtn = document.getElementById("imap_test");
const imapTestStatus = document.getElementById("imap_test_status");
const signinStatusEl = document.getElementById("signin-status");
const signinStartBtn = document.getElementById("signin_start");
const signinFinishBtn = document.getElementById("signin_finish");
const signinCancelBtn = document.getElementById("signin_cancel");
const signinClearBtn = document.getElementById("signin_clear");
const useSignedInBox = document.getElementById("use_signed_in");
const statusCard = document.getElementById("status-card");
const resultsEl = document.getElementById("results");
const logEl = document.getElementById("log");
const progressFill = document.querySelector(".progress-fill");
const pillTotal = document.querySelector(".pill-total");
const pillOk = document.querySelector(".pill-ok");
const pillErr = document.querySelector(".pill-err");

let evtSource = null;

function appendLog(msg) {
  const t = new Date().toLocaleTimeString();
  logEl.textContent += `[${t}] ${msg}\n`;
  logEl.scrollTop = logEl.scrollHeight;
}

function renderEmailRow(email) {
  const li = document.createElement("li");
  li.dataset.email = email;
  li.innerHTML = `
    <span class="email">${email}</span>
    <span class="row-meta muted small"></span>
    <span class="badge pending">queued</span>
  `;
  resultsEl.appendChild(li);
  return li;
}

function setRowMeta(email, text) {
  const li = resultsEl.querySelector(`li[data-email="${CSS.escape(email)}"]`);
  if (!li) return;
  const meta = li.querySelector(".row-meta");
  if (meta) meta.textContent = text;
}

function setBadge(email, cls, text) {
  const li = resultsEl.querySelector(`li[data-email="${CSS.escape(email)}"]`);
  if (!li) return;
  const badge = li.querySelector(".badge");
  badge.className = `badge ${cls}`;
  badge.textContent = text;
}

function resetUi(emails) {
  statusCard.hidden = false;
  resultsEl.innerHTML = "";
  logEl.textContent = "";
  progressFill.style.width = "0%";
  pillTotal.textContent = `${emails.length} total`;
  pillOk.textContent = `0 ok`;
  pillErr.textContent = `0 failed`;
  emails.forEach(renderEmailRow);
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (evtSource) {
    evtSource.close();
    evtSource = null;
  }

  const rawFormUrls = document.getElementById("form_urls").value;
  const form_urls = rawFormUrls
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const rawEmails = document.getElementById("emails").value;
  const delay = parseFloat(document.getElementById("delay").value || "0");
  const headless = document.getElementById("headless").checked;
  const max_retries = parseInt(
    document.getElementById("max_retries").value || "2",
    10
  );
  const concurrency = parseInt(
    document.getElementById("concurrency").value || "1",
    10
  );
  const send_me_copy = document.getElementById("send_me_copy").checked;
  const use_signed_in_profile = useSignedInBox && useSignedInBox.checked;

  const emails = rawEmails
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);

  if (form_urls.length === 0 || emails.length === 0) {
    alert("Please provide at least one Form URL and at least one email.");
    return;
  }

  goBtn.disabled = true;
  goBtn.textContent = "Running ...";
  resetUi(emails);

  const proxy = {
    enabled: document.getElementById("proxy_enabled").checked,
    host: document.getElementById("proxy_host").value.trim(),
    port: document.getElementById("proxy_port").value.trim(),
    username: document.getElementById("proxy_username").value.trim(),
    password: document.getElementById("proxy_password").value,
    scheme: "http",
  };

  const inbox = {
    enabled: document.getElementById("inbox_enabled").checked,
    host: document.getElementById("imap_host").value.trim(),
    port: parseInt(document.getElementById("imap_port").value || "993", 10),
    username: document.getElementById("imap_username").value.trim(),
    password: document.getElementById("imap_password").value,
    timeout: parseInt(
      document.getElementById("imap_timeout").value || "120",
      10
    ),
    use_ssl: true,
    mailbox: "INBOX",
  };

  let resp;
  try {
    resp = await fetch("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        form_urls,
        emails: rawEmails,
        delay,
        headless,
        proxy,
        max_retries,
        concurrency,
        send_me_copy,
        inbox,
        use_signed_in_profile,
      }),
    });
  } catch (err) {
    alert("Network error: " + err);
    goBtn.disabled = false;
    goBtn.textContent = "Start submissions";
    return;
  }

  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    alert("Error: " + (data.error || resp.statusText));
    goBtn.disabled = false;
    goBtn.textContent = "Start submissions";
    return;
  }

  const { job_id } = await resp.json();
  appendLog(`Job started: ${job_id}`);

  let done = 0;
  let ok = 0;
  let err = 0;
  const total = emails.length;

  evtSource = new EventSource(`/api/stream/${job_id}`);
  evtSource.onmessage = (e) => {
    let payload;
    try {
      payload = JSON.parse(e.data);
    } catch {
      return;
    }
    const { event } = payload;

    if (event === "start") {
      const c = payload.concurrency || 1;
      const fc = payload.form_count || 1;
      const formStr = fc > 1 ? `, ${fc} forms (round-robin)` : "";
      appendLog(
        `Started — ${payload.total} email(s)` +
          (c > 1 ? `, ${c} parallel browser(s)` : "") +
          formStr
      );
    } else if (event === "progress") {
      setBadge(payload.email, "running", "running");
      const fIdx = payload.form_index;
      if (fIdx) setRowMeta(payload.email, `form ${fIdx}`);
      const formTag = fIdx ? ` form ${fIdx}` : "";
      appendLog(
        `[${payload.index}/${payload.total}]${formTag} ${payload.email}: starting`
      );
    } else if (event === "log") {
      const tag = payload.form_label ? ` ${payload.form_label}` : "";
      appendLog(
        `[${payload.index}/${payload.total}]${tag} ${payload.email}: ${payload.message}`
      );
    } else if (event === "result") {
      done += 1;
      if (payload.success) {
        ok += 1;
        setBadge(payload.email, "ok", "submitted");
      } else {
        err += 1;
        setBadge(payload.email, "err", "failed");
      }
      pillOk.textContent = `${ok} ok`;
      pillErr.textContent = `${err} failed`;
      progressFill.style.width = `${(done / total) * 100}%`;
      const fTag = payload.form_index ? ` form ${payload.form_index}` : "";
      appendLog(
        `[${payload.index}/${payload.total}]${fTag} ${payload.email}: ${
          payload.success ? "SUCCESS" : "FAILED"
        } — ${payload.message}`
      );
    } else if (event === "done") {
      appendLog(`Done. ${payload.success} success, ${payload.failure} failed.`);
      goBtn.disabled = false;
      goBtn.textContent = "Start submissions";
      evtSource.close();
      evtSource = null;
    }
  };

  evtSource.onerror = () => {
    appendLog("Lost connection to server.");
    goBtn.disabled = false;
    goBtn.textContent = "Start submissions";
    if (evtSource) {
      evtSource.close();
      evtSource = null;
    }
  };
});

// ----- Google sign-in lifecycle -----
async function refreshSigninStatus() {
  if (!signinStatusEl) return;
  let s;
  try {
    const r = await fetch("/api/signin/status");
    s = await r.json();
  } catch (e) {
    signinStatusEl.textContent = "Status check failed: " + e;
    return;
  }
  const showHide = (el, on) => el && (el.hidden = !on);
  if (s.active) {
    signinStatusEl.innerHTML =
      'Browser open on the server &mdash; sign in there, then click ' +
      '<strong>Mark sign-in complete</strong>.';
    showHide(signinStartBtn, false);
    showHide(signinFinishBtn, true);
    showHide(signinCancelBtn, true);
    showHide(signinClearBtn, false);
    if (useSignedInBox) useSignedInBox.disabled = true;
  } else if (s.has_profile) {
    const who = s.email ? s.email : "(account)";
    signinStatusEl.innerHTML = `Signed in as <strong>${who}</strong>. Submissions can use this profile.`;
    showHide(signinStartBtn, false);
    showHide(signinFinishBtn, false);
    showHide(signinCancelBtn, false);
    showHide(signinClearBtn, true);
    if (useSignedInBox) {
      useSignedInBox.disabled = false;
      if (!useSignedInBox.dataset.userTouched) useSignedInBox.checked = true;
    }
  } else {
    signinStatusEl.textContent =
      "Not signed in. Click 'Sign in with Google' to open a Chrome window on the server.";
    showHide(signinStartBtn, true);
    showHide(signinFinishBtn, false);
    showHide(signinCancelBtn, false);
    showHide(signinClearBtn, false);
    if (useSignedInBox) {
      useSignedInBox.disabled = true;
      useSignedInBox.checked = false;
    }
  }
}

if (useSignedInBox) {
  useSignedInBox.addEventListener("change", () => {
    useSignedInBox.dataset.userTouched = "1";
  });
}

if (signinStartBtn) {
  signinStartBtn.addEventListener("click", async () => {
    signinStartBtn.disabled = true;
    signinStatusEl.textContent = "Opening Chrome on the server ...";
    try {
      const r = await fetch("/api/signin/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const j = await r.json();
      if (!j.ok) signinStatusEl.textContent = "Error: " + (j.error || "");
    } catch (e) {
      signinStatusEl.textContent = "Network error: " + e;
    } finally {
      signinStartBtn.disabled = false;
      refreshSigninStatus();
    }
  });
}

if (signinFinishBtn) {
  signinFinishBtn.addEventListener("click", async () => {
    signinFinishBtn.disabled = true;
    signinStatusEl.textContent = "Closing browser and saving profile ...";
    try {
      const r = await fetch("/api/signin/finish", { method: "POST" });
      const j = await r.json();
      if (!j.ok) signinStatusEl.textContent = "Error: " + (j.error || "");
    } catch (e) {
      signinStatusEl.textContent = "Network error: " + e;
    } finally {
      signinFinishBtn.disabled = false;
      refreshSigninStatus();
    }
  });
}

if (signinCancelBtn) {
  signinCancelBtn.addEventListener("click", async () => {
    await fetch("/api/signin/cancel", { method: "POST" });
    refreshSigninStatus();
  });
}

if (signinClearBtn) {
  signinClearBtn.addEventListener("click", async () => {
    if (!confirm("Wipe the saved Google profile?")) return;
    await fetch("/api/signin/clear", { method: "POST" });
    refreshSigninStatus();
  });
}

refreshSigninStatus();
setInterval(refreshSigninStatus, 8000);

if (imapTestBtn) {
  imapTestBtn.addEventListener("click", async () => {
    const payload = {
      host: document.getElementById("imap_host").value.trim(),
      port: parseInt(document.getElementById("imap_port").value || "993", 10),
      username: document.getElementById("imap_username").value.trim(),
      password: document.getElementById("imap_password").value,
      use_ssl: true,
    };
    imapTestStatus.textContent = "Testing ...";
    imapTestStatus.style.color = "";
    imapTestBtn.disabled = true;
    try {
      const r = await fetch("/api/test_inbox", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json();
      if (j.ok) {
        imapTestStatus.textContent = j.message || "OK";
        imapTestStatus.style.color = "var(--ok)";
      } else {
        imapTestStatus.textContent = j.error || "Login failed";
        imapTestStatus.style.color = "var(--err)";
      }
    } catch (e) {
      imapTestStatus.textContent = "Network error: " + e;
      imapTestStatus.style.color = "var(--err)";
    } finally {
      imapTestBtn.disabled = false;
    }
  });
}
