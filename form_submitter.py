"""
Google Form submitter.

Fills a single email field on a Google Form's /viewform page, solves the
reCAPTCHA (audio challenge) if one is present, and submits. Supports both
the "I'm not a robot" checkbox variant and the invisible reCAPTCHA that
fires from Submit click (common on Google Forms today).
"""

from __future__ import annotations

import random
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from recaptcha_solver import (
    ANCHOR_IFRAME_XPATH,
    RecaptchaBlockedError,
    RecaptchaSolver,
)
from proxy_support import ProxyConfig, build_proxy_auth_extension, cleanup_extension
from fingerprint import Fingerprint, build_stealth_js, random_fingerprint


Logger = Callable[[str], None]


@dataclass
class SubmitResult:
    email: str
    success: bool
    message: str


SUBMIT_XPATHS = [
    "//div[@role='button' and @jsname='M2UYVd']",
    "//div[@role='button'][.//span[normalize-space()='Submit']]",
    "//div[@role='button'][.//span[normalize-space()='submit']]",
    "//div[@role='button'][.//span[contains(., 'Submit')]]",
    "//button[normalize-space()='Submit']",
    "//div[@role='button'][.//span[contains(., 'Gửi') or contains(., 'Enviar') or contains(., 'Отправить') or contains(., 'Senden') or contains(., '送信')]]",
]


def _build_driver(
    headless: bool = True,
    proxy: Optional[ProxyConfig] = None,
    fingerprint: Optional[Fingerprint] = None,
) -> tuple[webdriver.Chrome, Optional[str], str, Fingerprint]:
    """Return (driver, proxy_extension_dir, user_data_dir, fingerprint).

    A fresh random fingerprint is generated for each browser unless
    one is supplied. The fingerprint drives the user-agent, window
    size, and a CDP-injected stealth script that overrides navigator
    / screen / WebGL / canvas so repeated attempts don't look identical
    to reCAPTCHA.

    The caller is responsible for cleaning up both directories
    (``cleanup_extension`` + rmtree of the user-data-dir). A unique
    user-data-dir per browser is required when running many Chromes
    in parallel — otherwise Chrome's Singleton lock makes the second
    launch hang or fail.
    """
    fp = fingerprint or random_fingerprint()

    opts = ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument(f"--window-size={fp.window_width},{fp.window_height}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(f"--lang={fp.languages[0]}")
    opts.add_argument(f"--user-agent={fp.user_agent}")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--ignore-ssl-errors=yes")
    opts.add_argument("--allow-insecure-localhost")
    opts.set_capability("acceptInsecureCerts", True)
    opts.add_experimental_option(
        "excludeSwitches", ["enable-automation", "enable-logging"]
    )
    opts.add_experimental_option("useAutomationExtension", False)

    user_data_dir = tempfile.mkdtemp(prefix="chrome_udd_")
    opts.add_argument(f"--user-data-dir={user_data_dir}")

    ext_dir: Optional[str] = None
    if proxy is not None:
        if proxy.username or proxy.password:
            ext_dir = build_proxy_auth_extension(proxy)
            opts.add_argument(f"--load-extension={ext_dir}")
            opts.add_argument(f"--disable-extensions-except={ext_dir}")
        else:
            opts.add_argument(
                f"--proxy-server={proxy.scheme}://{proxy.host}:{proxy.port}"
            )

    driver = webdriver.Chrome(options=opts)

    try:
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {
                "userAgent": fp.user_agent,
                "acceptLanguage": fp.accept_language,
                "platform": fp.ua_platform,
            },
        )
    except WebDriverException:
        pass
    try:
        driver.execute_cdp_cmd(
            "Emulation.setTimezoneOverride",
            {"timezoneId": fp.timezone},
        )
    except WebDriverException:
        pass
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": build_stealth_js(fp)},
        )
    except WebDriverException:
        pass
    return driver, ext_dir, user_data_dir, fp


def _find_submit_button(driver) -> Optional[object]:
    for xp in SUBMIT_XPATHS:
        elems = driver.find_elements(By.XPATH, xp)
        for el in elems:
            try:
                if el.is_displayed():
                    return el
            except WebDriverException:
                continue
    return None


def _click_submit(
    driver,
    log: Logger,
    rng: Optional[random.Random] = None,
) -> bool:
    rng = rng or random.Random()
    try:
        btn = WebDriverWait(driver, 10).until(lambda d: _find_submit_button(d))
    except TimeoutException:
        log("Could not find the form's Submit button.")
        return False
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", btn
        )
    except WebDriverException:
        pass
    time.sleep(rng.uniform(0.4, 1.1))
    try:
        ActionChains(driver).move_to_element(btn).pause(
            rng.uniform(0.2, 0.5)
        ).click(btn).perform()
    except WebDriverException:
        try:
            btn.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", btn)
    log("Clicked Submit.")
    return True


def _has_anchor_iframe(driver) -> bool:
    try:
        driver.switch_to.default_content()
        for frame in driver.find_elements(By.XPATH, ANCHOR_IFRAME_XPATH):
            size = frame.size or {}
            if (size.get("width") or 0) > 20 and (size.get("height") or 0) > 20:
                return True
        return False
    except WebDriverException:
        return False


def _verify_proxy(driver, proxy: ProxyConfig, log: Logger) -> None:
    """Navigate to an IP echo endpoint through the proxy and log the exit IP.

    Raises RuntimeError if the proxy is clearly not being used (e.g. the
    browser errors out loading the page, or the response body doesn't
    look like an IP). The extra page load also gives the proxy-auth
    extension a moment to register its listeners before any real
    traffic happens.
    """
    endpoints = [
        "https://api.ipify.org?format=text",
        "https://icanhazip.com",
        "https://ifconfig.me/ip",
    ]
    last_err: Optional[str] = None
    driver.set_page_load_timeout(30)

    for attempt in range(3):
        for url in endpoints:
            try:
                driver.get(url)
                body = ""
                try:
                    body = driver.find_element(By.TAG_NAME, "body").text.strip()
                except WebDriverException:
                    body = ""
                if body and len(body) < 64 and any(c.isdigit() for c in body):
                    log(
                        f"Proxy verified. Exit IP: {body.splitlines()[0].strip()}"
                    )
                    return
                last_err = f"{url}: unexpected response {body[:80]!r}"
            except WebDriverException as exc:
                last_err = (
                    f"{url}: {exc.__class__.__name__}: "
                    f"{str(exc).splitlines()[0] if str(exc) else ''}"
                )
                continue
        time.sleep(1.5)

    raise RuntimeError(
        f"Proxy pre-flight failed (proxy={proxy.host}:{proxy.port}): {last_err}"
    )


def _confirmation_reached(driver) -> bool:
    """Strict confirmation: the only reliable signal is the URL
    transition from `/viewform` to `/formResponse`.

    We intentionally DO NOT match 'Your response has been recorded' in
    the page source anymore — Google Forms puts similar text on the
    viewform page (e.g. 'A copy of your responses will be emailed to
    the address you provided'), which used to produce false-positive
    successes where the form never actually submitted.
    """
    try:
        url = driver.current_url or ""
    except WebDriverException:
        return False
    return "formResponse" in url


def _human_type(element, text: str, rng: random.Random) -> None:
    """Type each character with a small randomized delay to mimic
    real typing. Google Forms + reCAPTCHA v3 weight cadence heavily."""
    for ch in text:
        element.send_keys(ch)
        time.sleep(rng.uniform(0.06, 0.18))
        if rng.random() < 0.04:
            time.sleep(rng.uniform(0.25, 0.6))


def _human_mouse_warmup(driver, rng: random.Random) -> None:
    """Move the cursor around the page a little before interacting.
    reCAPTCHA v3 observes mouse movement and scoring improves noticeably
    when there's any non-trivial motion before the critical click."""
    try:
        actions = ActionChains(driver)
        for _ in range(rng.randint(2, 4)):
            actions.move_by_offset(
                rng.randint(-120, 120), rng.randint(-80, 80)
            )
            actions.pause(rng.uniform(0.1, 0.3))
        actions.perform()
    except WebDriverException:
        pass


def _scroll_a_bit(driver, rng: random.Random) -> None:
    try:
        driver.execute_script(
            "window.scrollBy(0, arguments[0]);", rng.randint(80, 260)
        )
        time.sleep(rng.uniform(0.2, 0.6))
        driver.execute_script(
            "window.scrollBy(0, arguments[0]);", -rng.randint(40, 180)
        )
    except WebDriverException:
        pass


def _attempt_submit(
    form_url: str,
    email: str,
    log: Logger,
    headless: bool,
    proxy: Optional[ProxyConfig],
) -> None:
    """Run one full submission attempt in a fresh browser. Raises on
    failure; returns None on success."""
    driver, ext_dir, user_data_dir, fp = _build_driver(
        headless=headless, proxy=proxy
    )
    ua_short = fp.user_agent.split(") ", 1)[0] + ")"
    log(
        f"Fingerprint: {ua_short} | {fp.platform} | {fp.timezone} | "
        f"{fp.screen_width}x{fp.screen_height} | GPU={fp.webgl_renderer[:40]}"
    )
    rng = random.Random()
    try:
        if proxy is not None:
            log("Verifying proxy connectivity ...")
            _verify_proxy(driver, proxy, log)

        driver.get(form_url)

        time.sleep(rng.uniform(1.5, 3.0))
        _human_mouse_warmup(driver, rng)
        _scroll_a_bit(driver, rng)

        email_input = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (By.XPATH, "//input[@type='email' or @type='text']")
            )
        )
        try:
            ActionChains(driver).move_to_element(email_input).pause(
                rng.uniform(0.2, 0.5)
            ).click(email_input).perform()
        except WebDriverException:
            email_input.click()
        email_input.clear()
        _human_type(email_input, email, rng)
        log(f"Filled email: {email}")

        time.sleep(rng.uniform(0.8, 1.8))
        _human_mouse_warmup(driver, rng)

        solver = RecaptchaSolver(driver, logger=log, proxy=proxy)

        if _has_anchor_iframe(driver):
            log("Checkbox reCAPTCHA detected — clicking anchor first.")
            solver.click_anchor_if_visible(timeout=5)
            solver.solve_challenge_if_present(timeout=8)

        if not _click_submit(driver, log, rng):
            raise RuntimeError("Submit button not found on form.")

        log("Waiting for reCAPTCHA challenge (if any) after submit ...")
        solved = solver.solve_challenge_if_present(timeout=10)

        if solved:
            log("Challenge solved. Pausing 3s before submitting form ...")
            time.sleep(3)
            _human_mouse_warmup(driver, rng)

            log("Waiting to see if form auto-submits ...")
            auto_submitted = False
            end = time.time() + 8
            while time.time() < end:
                if _confirmation_reached(driver):
                    auto_submitted = True
                    break
                time.sleep(0.5)

            if not auto_submitted:
                log("Form did not auto-submit; clicking Submit once more.")
                _click_submit(driver, log, rng)

        try:
            WebDriverWait(driver, 30).until(lambda d: _confirmation_reached(d))
        except TimeoutException:
            pass

        if not _confirmation_reached(driver):
            raise RuntimeError(
                "Form did not redirect to /formResponse — submission was "
                "not accepted by Google (captcha score too low or form "
                "requires additional fields)."
            )

        log(
            f"Submission confirmed for {email} "
            f"(URL: {driver.current_url}). Holding browser open briefly ..."
        )
        time.sleep(rng.uniform(2.5, 4.5))
    finally:
        try:
            time.sleep(0.5)
            driver.quit()
        except Exception:
            pass
        cleanup_extension(ext_dir)
        cleanup_extension(user_data_dir)


def submit_form(
    form_url: str,
    email: str,
    logger: Optional[Logger] = None,
    headless: bool = True,
    proxy: Optional[ProxyConfig] = None,
    max_retries: int = 2,
    retry_backoff: float = 4.0,
) -> SubmitResult:
    """Fill the email field and submit a single Google Form response.

    On ``RecaptchaBlockedError`` ("Try again later") we tear the browser
    down (releasing the proxy's TCP session so the next connection gets
    a fresh exit IP) and retry up to ``max_retries`` additional times.
    """
    log: Logger = logger or (lambda msg: None)
    log(f"Opening form for {email} ...")
    if proxy is not None:
        log(
            f"Using proxy {proxy.host}:{proxy.port} "
            f"(user={proxy.username or '-'})"
        )

    total_attempts = max_retries + 1
    for attempt in range(1, total_attempts + 1):
        if attempt > 1:
            log(f"Attempt {attempt}/{total_attempts} (rotating browser / proxy IP) ...")
        try:
            _attempt_submit(
                form_url=form_url,
                email=email,
                log=log,
                headless=headless,
                proxy=proxy,
            )
            return SubmitResult(
                email=email, success=True, message="Submitted successfully"
            )
        except RecaptchaBlockedError as exc:
            log(f"Attempt {attempt}/{total_attempts} blocked: {exc}")
            if attempt < total_attempts:
                wait = retry_backoff * attempt
                log(f"Waiting {wait:.1f}s before retry with a fresh IP ...")
                time.sleep(wait)
                continue
            return SubmitResult(
                email=email,
                success=False,
                message=(
                    f"Blocked by Google after {total_attempts} attempt(s). "
                    "Try a different proxy pool or wait before retrying."
                ),
            )
        except Exception as exc:
            log(f"Error submitting for {email}: {exc}")
            msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
            return SubmitResult(
                email=email, success=False, message=msg or exc.__class__.__name__
            )

    return SubmitResult(
        email=email, success=False, message="No attempts were made."
    )
