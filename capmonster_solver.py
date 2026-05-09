"""
CapMonster.Cloud API client for reCAPTCHA v2 (visible + invisible).

Pre-solves the captcha BEFORE the form's Submit button is clicked:
  1. Find the form's reCAPTCHA site key on the page.
  2. POST to /createTask with the site key + form URL.
  3. Poll /getTaskResult until status="ready".
  4. Inject the returned token into ``g-recaptcha-response`` so the
     form sees the captcha as solved.
  5. Click Submit — the challenge popup never appears.

API docs: https://docs.capmonster.cloud/
Pricing:   https://capmonster.cloud (~$0.6 per 1000 reCAPTCHAv2 tasks)
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import requests
from selenium.common.exceptions import WebDriverException


CAPMONSTER_BASE = "https://api.capmonster.cloud"

Logger = Callable[[str], None]


class CapMonsterError(RuntimeError):
    pass


class CapMonsterSolver:
    def __init__(
        self,
        api_key: str,
        logger: Optional[Logger] = None,
        poll_interval: float = 3.0,
        timeout: float = 180.0,
    ) -> None:
        if not api_key or not api_key.strip():
            raise CapMonsterError("CapMonster API key is empty")
        self.api_key = api_key.strip()
        self.log: Logger = logger or (lambda msg: None)
        self.poll_interval = poll_interval
        self.timeout = timeout

    def solve_recaptcha_v2(
        self,
        website_url: str,
        website_key: str,
        is_invisible: bool = True,
    ) -> str:
        """Submit a RecaptchaV2TaskProxyless and poll until solved.

        Returns the ``gRecaptchaResponse`` token. Raises CapMonsterError
        on failure or timeout.
        """
        task = {
            "type": "RecaptchaV2TaskProxyless",
            "websiteURL": website_url,
            "websiteKey": website_key,
            "isInvisible": bool(is_invisible),
        }
        try:
            resp = requests.post(
                f"{CAPMONSTER_BASE}/createTask",
                json={"clientKey": self.api_key, "task": task},
                timeout=30,
                verify=False,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise CapMonsterError(f"createTask request failed: {exc}") from exc

        if data.get("errorId"):
            raise CapMonsterError(
                f"createTask: {data.get('errorCode')} — "
                f"{data.get('errorDescription')}"
            )
        task_id = data.get("taskId")
        if not task_id:
            raise CapMonsterError(f"createTask returned no taskId: {data!r}")
        self.log(f"CapMonster task {task_id} created; polling for solution ...")

        started = time.time()
        deadline = started + self.timeout
        time.sleep(self.poll_interval)

        while time.time() < deadline:
            try:
                resp = requests.post(
                    f"{CAPMONSTER_BASE}/getTaskResult",
                    json={"clientKey": self.api_key, "taskId": task_id},
                    timeout=30,
                    verify=False,
                )
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                self.log(f"getTaskResult network error (will retry): {exc}")
                time.sleep(self.poll_interval)
                continue

            if data.get("errorId"):
                raise CapMonsterError(
                    f"getTaskResult: {data.get('errorCode')} — "
                    f"{data.get('errorDescription')}"
                )

            status = data.get("status")
            if status == "ready":
                solution = data.get("solution") or {}
                token = solution.get("gRecaptchaResponse")
                if not token:
                    raise CapMonsterError(
                        f"CapMonster returned no gRecaptchaResponse: {solution!r}"
                    )
                self.log(
                    f"CapMonster solved in {time.time() - started:.1f}s "
                    f"(token len {len(token)})"
                )
                return token
            if status == "processing":
                time.sleep(self.poll_interval)
                continue

            raise CapMonsterError(
                f"CapMonster returned unexpected status: {status!r}"
            )

        raise CapMonsterError(
            f"CapMonster timed out after {int(self.timeout)}s "
            f"(task {task_id})"
        )

    def inject_token(self, driver, token: str) -> None:
        """Inject the token into ``g-recaptcha-response`` and fire the
        registered grecaptcha callbacks (which is what Google Forms'
        invisible reCAPTCHA listens for)."""
        try:
            driver.execute_script(_INJECT_JS, token)
        except WebDriverException as exc:
            raise CapMonsterError(
                f"Failed to inject token into page: {exc}"
            ) from exc

    def get_balance(self) -> float:
        """Optional: fetch CapMonster account balance. Used by the
        UI's 'Test API key' button."""
        resp = requests.post(
            f"{CAPMONSTER_BASE}/getBalance",
            json={"clientKey": self.api_key},
            timeout=20,
            verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errorId"):
            raise CapMonsterError(
                f"{data.get('errorCode')} — {data.get('errorDescription')}"
            )
        return float(data.get("balance") or 0.0)


_FIND_SITEKEY_JS = r"""
var el = document.querySelector('[data-sitekey]');
if (el) return el.getAttribute('data-sitekey');
var iframes = document.querySelectorAll('iframe[src*="recaptcha"]');
for (var i = 0; i < iframes.length; i++) {
  var m = iframes[i].src.match(/[?&]k=([^&]+)/);
  if (m) return decodeURIComponent(m[1]);
}
var html = document.documentElement.outerHTML;
var m1 = html.match(/data-sitekey="([^"]+)"/);
if (m1) return m1[1];
var m2 = html.match(/[?&]k=([0-9A-Za-z_-]{20,})/);
if (m2) return m2[1];
var m3 = html.match(/sitekey['"]?\s*[:=]\s*['"]([^'"]+)['"]/);
if (m3) return m3[1];
return null;
"""


def find_recaptcha_sitekey(driver) -> Optional[str]:
    """Best-effort scan of the current page for a reCAPTCHA site key."""
    try:
        return driver.execute_script(_FIND_SITEKEY_JS)
    except WebDriverException:
        return None


_INJECT_JS = r"""
var token = arguments[0];

var ta = document.getElementById('g-recaptcha-response');
if (ta) {
  ta.style.display = 'block';
  ta.value = token;
  ta.innerHTML = token;
} else {
  ta = document.createElement('textarea');
  ta.id = 'g-recaptcha-response';
  ta.name = 'g-recaptcha-response';
  ta.style.display = 'block';
  ta.value = token;
  document.body.appendChild(ta);
}

// Walk grecaptcha's internal client objects and fire any registered
// callback. This is what Google Forms' invisible reCAPTCHA waits for.
try {
  if (typeof ___grecaptcha_cfg !== 'undefined' && ___grecaptcha_cfg.clients) {
    var clients = ___grecaptcha_cfg.clients;
    Object.keys(clients).forEach(function (k) {
      var c = clients[k];
      Object.keys(c).forEach(function (k1) {
        var sub = c[k1];
        if (sub && typeof sub === 'object') {
          Object.keys(sub).forEach(function (k2) {
            var leaf = sub[k2];
            if (leaf && typeof leaf.callback === 'function') {
              try { leaf.callback(token); } catch (e) {}
            }
          });
        }
      });
    });
  }
} catch (e) {}

// Also dispatch an input event on the textarea for forms that listen
// to value changes via React/Vue/etc.
try {
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.dispatchEvent(new Event('change', { bubbles: true }));
} catch (e) {}
"""
