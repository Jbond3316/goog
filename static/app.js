const form = document.getElementById("submit-form");
const goBtn = document.getElementById("go");
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
    <span class="badge pending">queued</span>
  `;
  resultsEl.appendChild(li);
  return li;
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

  const form_url = document.getElementById("form_url").value.trim();
  const rawEmails = document.getElementById("emails").value;
  const delay = parseFloat(document.getElementById("delay").value || "0");
  const headless = document.getElementById("headless").checked;
  const max_retries = parseInt(
    document.getElementById("max_retries").value || "2",
    10
  );

  const emails = rawEmails
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);

  if (!form_url || emails.length === 0) {
    alert("Please provide a Form URL and at least one email.");
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

  let resp;
  try {
    resp = await fetch("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        form_url,
        emails: rawEmails,
        delay,
        headless,
        proxy,
        max_retries,
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
      appendLog(`Started — ${payload.total} email(s)`);
    } else if (event === "progress") {
      setBadge(payload.email, "running", "running");
      appendLog(`[${payload.index}/${payload.total}] ${payload.email}: starting`);
    } else if (event === "log") {
      appendLog(`[${payload.index}/${payload.total}] ${payload.email}: ${payload.message}`);
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
      appendLog(
        `[${payload.index}/${payload.total}] ${payload.email}: ${
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
