"""
One-time Google sign-in flow + profile cloning for parallel browsers.

The user clicks "Sign in with Google" in the UI. The server opens a
real, headed Chrome window pointed at https://accounts.google.com,
backed by a persistent user-data-dir at ``MASTER_PROFILE_DIR``. The
user signs in manually inside that window (Google blocks automated
logins, so this step has to be human-driven). When they click "Mark
sign-in complete", we close the Chrome cleanly, leaving the profile
populated with all the relevant cookies / Local Storage / etc.

For every subsequent form submission, ``clone_master_profile()``
copies the master profile (minus volatile cache directories) into
a brand-new tempdir, and Chrome launches against that tempdir. Each
parallel browser gets its own copy so they can run concurrently
(Chrome's Singleton lock prevents two Chromes from sharing a single
profile).

Limits / caveats:
- Sign-in is intentionally NOT routed through the proxy by default.
  Doing it from your real IP avoids Google's "unusual sign-in
  activity" prompts that would otherwise require phone verification.
  Once the cookies are saved, sessions usually survive an IP change
  for typical Google Forms use.
- The saved profile lives under ``./.google_profile`` next to the
  app. It survives server restarts. Click "Sign out / clear" to wipe
  it.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions

from proxy_support import ProxyConfig, build_proxy_auth_extension


_HERE = os.path.dirname(os.path.abspath(__file__))
MASTER_PROFILE_DIR = os.path.join(_HERE, ".google_profile")
META_FILE = os.path.join(_HERE, ".google_profile.meta.json")


# --- volatile cache subdirs we skip when cloning to save disk + time ---
_SKIP_PROFILE_NAMES = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "GraphiteDawnCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "Service Worker",
    "blob_storage",
    "Crashpad",
    "Crash Reports",
    "component_crx_cache",
    "GrShaderCache",
    "Default Cache",
}


@dataclass
class SigninSession:
    profile_dir: str
    driver: webdriver.Chrome
    proxy_ext_dir: Optional[str]
    started_at: float


_LOCK = threading.Lock()
_CURRENT: Optional[SigninSession] = None


# ---------------------------------------------------------------------
#  metadata helpers
# ---------------------------------------------------------------------

def _read_meta() -> dict:
    if not os.path.isfile(META_FILE):
        return {}
    try:
        with open(META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_meta(d: dict) -> None:
    try:
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except OSError:
        pass


def has_master_profile() -> bool:
    return os.path.isdir(MASTER_PROFILE_DIR) and bool(
        os.listdir(MASTER_PROFILE_DIR)
    )


def saved_email() -> Optional[str]:
    return _read_meta().get("email") if has_master_profile() else None


def is_signin_active() -> bool:
    with _LOCK:
        return _CURRENT is not None


# ---------------------------------------------------------------------
#  driver for the sign-in flow
# ---------------------------------------------------------------------

def _build_signin_driver(
    profile_dir: str,
    proxy: Optional[ProxyConfig] = None,
) -> tuple[webdriver.Chrome, Optional[str]]:
    opts = ChromeOptions()
    # Don't block sign-in on slow proxies / slow Google account pages.
    opts.page_load_strategy = "eager"
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1100,820")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--ignore-ssl-errors=yes")
    opts.set_capability("acceptInsecureCerts", True)
    opts.add_experimental_option(
        "excludeSwitches", ["enable-automation", "enable-logging"]
    )
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"--user-data-dir={profile_dir}")

    ext_dir: Optional[str] = None
    if proxy is not None:
        opts.add_argument(
            f"--proxy-server={proxy.scheme}://{proxy.host}:{proxy.port}"
        )
        opts.add_argument("--proxy-bypass-list=<-loopback>")
        if proxy.username or proxy.password:
            ext_dir = build_proxy_auth_extension(proxy)
            opts.add_argument(f"--load-extension={ext_dir}")
            opts.add_argument(f"--disable-extensions-except={ext_dir}")

    driver = webdriver.Chrome(options=opts)
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
            },
        )
    except WebDriverException:
        pass
    return driver, ext_dir


# ---------------------------------------------------------------------
#  public sign-in API
# ---------------------------------------------------------------------

def start_signin(proxy: Optional[ProxyConfig] = None) -> SigninSession:
    """Open a Chrome window pointed at Google sign-in. Returns
    immediately; the browser stays open for the user to interact
    with. Call ``finish_signin`` (or ``cancel_signin``) afterwards.

    Raises RuntimeError if a sign-in is already in progress.
    """
    global _CURRENT
    with _LOCK:
        if _CURRENT is not None:
            raise RuntimeError(
                "A sign-in is already in progress. Finish or cancel it first."
            )

        # wipe any prior master profile
        if os.path.exists(MASTER_PROFILE_DIR):
            shutil.rmtree(MASTER_PROFILE_DIR, ignore_errors=True)
        os.makedirs(MASTER_PROFILE_DIR, exist_ok=True)
        if os.path.exists(META_FILE):
            try:
                os.remove(META_FILE)
            except OSError:
                pass

        try:
            driver, ext_dir = _build_signin_driver(MASTER_PROFILE_DIR, proxy)
        except Exception:
            shutil.rmtree(MASTER_PROFILE_DIR, ignore_errors=True)
            raise

        try:
            driver.get("https://accounts.google.com/signin")
        except WebDriverException:
            pass

        _CURRENT = SigninSession(
            profile_dir=MASTER_PROFILE_DIR,
            driver=driver,
            proxy_ext_dir=ext_dir,
            started_at=time.time(),
        )
        return _CURRENT


def _detect_email(driver: webdriver.Chrome) -> Optional[str]:
    """Best-effort: try to read the signed-in email from a Google
    page. Never raises; returns None if we can't find it."""
    try:
        driver.get("https://myaccount.google.com/")
        time.sleep(2.0)
    except WebDriverException:
        return None

    scripts = [
        # data-email / data-identifier are common on Google account pages
        "return document.querySelector('[data-email]')?.getAttribute('data-email');",
        "return document.querySelector('[data-identifier]')?.getAttribute('data-identifier');",
        # avatar aria-label often contains the email after the display name
        "var el=document.querySelector('a[aria-label*=\"@\"]');"
        "return el ? el.getAttribute('aria-label') : null;",
        # personal-info page has a labelled email row
        "var el=document.querySelector('a[href*=\"/u/0/\"]');"
        "return el ? el.getAttribute('aria-label') : null;",
    ]
    for src in scripts:
        try:
            value = driver.execute_script(src)
        except WebDriverException:
            continue
        if not value:
            continue
        for token in str(value).split():
            t = token.strip(" ,()<>\"'")
            if "@" in t and "." in t.split("@", 1)[1]:
                return t
    return None


def finish_signin() -> Optional[str]:
    """Close the sign-in browser cleanly. The profile directory now
    contains the cookies and is ready to be cloned for submissions.

    Returns the detected email (best effort) or None.
    """
    global _CURRENT
    with _LOCK:
        sess = _CURRENT
        if sess is None:
            raise RuntimeError("No sign-in session is active.")

        email: Optional[str] = None
        try:
            email = _detect_email(sess.driver)
        except Exception:
            email = None

        try:
            sess.driver.quit()
        except Exception:
            pass

        if sess.proxy_ext_dir:
            shutil.rmtree(sess.proxy_ext_dir, ignore_errors=True)

        time.sleep(1.0)
        _CURRENT = None

    if not has_master_profile():
        raise RuntimeError(
            "Sign-in profile is empty — did you actually sign in before "
            "clicking 'Mark sign-in complete'?"
        )

    _write_meta({"email": email or "", "saved_at": int(time.time())})
    return email


def cancel_signin() -> None:
    """Close the sign-in browser without saving."""
    global _CURRENT
    with _LOCK:
        sess = _CURRENT
        _CURRENT = None
        if sess is None:
            return
        try:
            sess.driver.quit()
        except Exception:
            pass
        if sess.proxy_ext_dir:
            shutil.rmtree(sess.proxy_ext_dir, ignore_errors=True)

    shutil.rmtree(MASTER_PROFILE_DIR, ignore_errors=True)
    if os.path.exists(META_FILE):
        try:
            os.remove(META_FILE)
        except OSError:
            pass


def clear_master_profile() -> None:
    """Wipe the saved master profile + metadata. Cancels any active
    sign-in first."""
    cancel_signin()
    shutil.rmtree(MASTER_PROFILE_DIR, ignore_errors=True)
    if os.path.exists(META_FILE):
        try:
            os.remove(META_FILE)
        except OSError:
            pass


# ---------------------------------------------------------------------
#  cloning master profile for parallel form submissions
# ---------------------------------------------------------------------

def _ignore_caches(_src, names):
    return [n for n in names if n in _SKIP_PROFILE_NAMES]


def clone_into(target_dir: str) -> None:
    """Copy the master profile contents into ``target_dir`` (which
    must already exist). Volatile cache subdirs are skipped to keep
    the clone fast (~50ms) and small (~5-10 MB)."""
    if not has_master_profile():
        raise RuntimeError("No master profile saved — sign in first.")
    shutil.copytree(
        MASTER_PROFILE_DIR,
        target_dir,
        ignore=_ignore_caches,
        dirs_exist_ok=True,
    )
