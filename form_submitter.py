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
from datetime import datetime, timezone
from typing import Callable, Optional

from selenium import webdriver
from selenium.common.exceptions import (
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
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
from inbox_verifier import InboxConfig, wait_for_receipt
from capmonster_solver import (
    CapMonsterError,
    CapMonsterSolver,
    find_recaptcha_sitekey,
)
from human_behavior import (
    HumanBehavior,
    human_move_and_click,
    human_type,
    human_warmup,
)
import google_signin


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
    use_signed_in_profile: bool = False,
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
    # 'none' = driver.get() returns IMMEDIATELY without waiting for
    # the page to load. The WebDriverWait on the email field then
    # becomes the actual gate — we type as soon as that input renders,
    # without waiting for fonts/analytics/etc. coming through the slow
    # proxy. Critical for slow / lossy proxies; otherwise driver.get()
    # blocks for the entire page_load_timeout (default 30s) on every
    # submission.
    opts.page_load_strategy = "none"
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
    if use_signed_in_profile and google_signin.has_master_profile():
        try:
            google_signin.clone_into(user_data_dir)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to clone signed-in profile: {exc}"
            ) from exc
    opts.add_argument(f"--user-data-dir={user_data_dir}")

    ext_dir: Optional[str] = None
    if proxy is not None:
        # CRITICAL: always pass --proxy-server at the command line so
        # the proxy is in effect for the FIRST network request Chrome
        # makes, before the auth extension has had a chance to apply
        # chrome.proxy.settings.set() asynchronously. Without this flag
        # there is a race window during which Chrome's startup traffic
        # (and even the very first driver.get) can egress through the
        # local network instead of the proxy — leaking the real IP.
        opts.add_argument(
            f"--proxy-server={proxy.scheme}://{proxy.host}:{proxy.port}"
        )
        # Don't bypass the proxy for any non-loopback target.
        opts.add_argument("--proxy-bypass-list=<-loopback>")
        if proxy.username or proxy.password:
            # The extension exists ONLY to answer
            # webRequest.onAuthRequired with the proxy credentials.
            # Chrome doesn't accept user:pass embedded in --proxy-server.
            ext_dir = build_proxy_auth_extension(proxy)
            opts.add_argument(f"--load-extension={ext_dir}")
            opts.add_argument(f"--disable-extensions-except={ext_dir}")

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
    human: Optional[HumanBehavior] = None,
) -> bool:
    try:
        btn = WebDriverWait(driver, 10).until(lambda d: _find_submit_button(d))
    except TimeoutException:
        log("Could not find the form's Submit button.")
        return False
    if human is not None and human.enabled:
        human_move_and_click(driver, btn, human)
        log("Clicked Submit (human-paced).")
        return True
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", btn
        )
    except WebDriverException:
        pass
    try:
        btn.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", btn)
    log("Clicked Submit.")
    return True


SEND_COPY_LABELS = (
    "Send me a copy of my responses",
    "Email me a copy of my responses",
    "Send me a copy of my response",
    "Email a copy of my responses to me",
)


def _tick_send_me_copy(driver, log: Logger) -> bool:
    """If the form has the optional 'Send me a copy of my responses'
    checkbox, click it. Some forms enable email confirmations only
    when the respondent ticks this box, in which case skipping it
    means the address never gets the receipt even though the
    submission counted.

    Returns True if the box was found and toggled on.
    """
    label_xpath = " or ".join(
        f"contains(@aria-label, {repr(label)})" for label in SEND_COPY_LABELS
    )
    candidates_xpath = (
        f"//div[@role='checkbox' and ({label_xpath})] | "
        f"//*[@role='checkbox' and ({label_xpath})]"
    )
    text_xpath = " or ".join(
        f"contains(., {repr(label)})" for label in SEND_COPY_LABELS
    )
    label_neighbour_xpath = (
        f"//label[{text_xpath}]/preceding::*[@role='checkbox'][1] | "
        f"//label[{text_xpath}]/following::*[@role='checkbox'][1]"
    )

    for xp in (candidates_xpath, label_neighbour_xpath):
        try:
            elems = driver.find_elements(By.XPATH, xp)
        except WebDriverException:
            continue
        for el in elems:
            try:
                if not el.is_displayed():
                    continue
                aria_checked = (el.get_attribute("aria-checked") or "").lower()
                if aria_checked == "true":
                    log("'Send me a copy' was already checked.")
                    return True
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", el
                )
                time.sleep(0.4)
                try:
                    el.click()
                except WebDriverException:
                    driver.execute_script("arguments[0].click();", el)
                log("Ticked 'Send me a copy of my responses'.")
                time.sleep(0.4)
                return True
            except WebDriverException:
                continue
    return False


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

    # With page_load_strategy="none", driver.get() returns immediately
    # before any HTTP response. We then poll for the body text.
    body_wait_seconds = 12.0

    for attempt in range(2):
        for url in endpoints:
            try:
                driver.get(url)
                try:
                    body = WebDriverWait(driver, body_wait_seconds).until(
                        lambda d: (
                            (d.find_element(By.TAG_NAME, "body").text or "").strip()
                            or False
                        )
                    )
                except TimeoutException:
                    last_err = f"{url}: empty body within {int(body_wait_seconds)}s"
                    continue
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
        time.sleep(0.5)

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


def _attempt_submit(
    form_url: str,
    email: str,
    log: Logger,
    headless: bool,
    proxy: Optional[ProxyConfig],
    send_me_copy: bool = True,
    inbox: Optional[InboxConfig] = None,
    inbox_timeout: float = 120.0,
    submit_started_at: Optional[datetime] = None,
    use_signed_in_profile: bool = False,
    captcha_method: str = "audio",
    capmonster_api_key: Optional[str] = None,
    human: Optional[HumanBehavior] = None,
    speech_engine: str = "google",
    wit_token: Optional[str] = None,
    verify_proxy_at_startup: bool = False,
) -> None:
    """Run one full submission attempt in a fresh browser. Raises on
    failure; returns None on success."""
    rng = random.Random()
    human = human or HumanBehavior(enabled=False)

    fp_override: Optional[Fingerprint] = None
    if human.enabled:
        fp_override = random_fingerprint()
        fp_override.screen_width = human.screen_width
        fp_override.screen_height = human.screen_height
        fp_override.window_width = human.window_width
        fp_override.window_height = human.window_height

    driver, ext_dir, user_data_dir, fp = _build_driver(
        headless=headless,
        proxy=proxy,
        use_signed_in_profile=use_signed_in_profile,
        fingerprint=fp_override,
    )
    if use_signed_in_profile:
        log("Using cloned signed-in Google profile.")
    ua_short = fp.user_agent.split(") ", 1)[0] + ")"
    log(
        f"Fingerprint: {ua_short} | {fp.platform} | {fp.timezone} | "
        f"{fp.screen_width}x{fp.screen_height} | GPU={fp.webgl_renderer[:40]}"
    )
    if human.enabled:
        log(
            f"Human mode ON: keyboard {human.keyboard_delay_min}-"
            f"{human.keyboard_delay_max}ms/char, mouse "
            f"{human.mouse_speed_min}-{human.mouse_speed_max}px/step, "
            f"screen {human.screen_width}x{human.screen_height}"
        )
    try:
        if proxy is not None and verify_proxy_at_startup:
            log("Verifying proxy connectivity ...")
            _verify_proxy(driver, proxy, log)

        # Short page-load timeout — under page_load_strategy='none'
        # driver.get() returns immediately anyway, but if a downstream
        # CDP / extension call internally promotes the load to 'normal'
        # we don't want to block forever here.
        try:
            driver.set_page_load_timeout(20)
        except WebDriverException:
            pass
        try:
            driver.get(form_url)
        except TimeoutException:
            log(
                "driver.get() reported page-load timeout; ignoring and "
                "polling for the email field anyway."
            )

        if human.enabled:
            time.sleep(rng.uniform(1.2, 2.4))
            human_warmup(driver, human, rng)

        # Poll for the email field with a generous timeout — this is
        # now the ONLY gate. We wait for it to be CLICKABLE (present
        # + displayed + enabled) rather than just present, so React
        # has had time to attach its handlers and the field is
        # actually interactable.
        email_xpath = "//input[@type='email' or @type='text']"
        email_input = WebDriverWait(driver, 45).until(
            EC.element_to_be_clickable((By.XPATH, email_xpath))
        )

        # Belt-and-suspenders: click + clear + send_keys can still
        # race React's hydration on a heavily-loaded form. Retry a
        # few times if the element reports as not interactable.
        last_err: Optional[Exception] = None
        for attempt in range(6):
            try:
                email_input.click()
                email_input.clear()
                if human.enabled:
                    human_type(email_input, email, human, rng)
                    log(f"Typed email (human-paced): {email}")
                else:
                    email_input.send_keys(email)
                    log(f"Pasted email: {email}")
                last_err = None
                break
            except (
                ElementNotInteractableException,
                StaleElementReferenceException,
                WebDriverException,
            ) as exc:
                last_err = exc
                time.sleep(0.7)
                # Re-locate in case React replaced the node.
                try:
                    email_input = WebDriverWait(driver, 10).until(
                        EC.element_to_be_clickable((By.XPATH, email_xpath))
                    )
                except TimeoutException:
                    continue
        if last_err is not None:
            raise RuntimeError(
                f"Email field never became interactable: "
                f"{type(last_err).__name__}: {str(last_err).splitlines()[0] if str(last_err) else ''}"
            )

        if human.enabled:
            time.sleep(rng.uniform(0.5, 1.2))
            human_warmup(driver, human, rng)

        if send_me_copy:
            if not _tick_send_me_copy(driver, log):
                log("'Send me a copy' option not present on this form.")

        if captcha_method == "capmonster":
            if not capmonster_api_key:
                raise RuntimeError(
                    "Captcha method is 'capmonster' but no API key was "
                    "provided. Set CAPMONSTER_API_KEY env var or paste it "
                    "into the UI."
                )
            site_key = find_recaptcha_sitekey(driver)
            if site_key:
                log(f"Solving via CapMonster (site key: {site_key[:18]}…)")
                cm = CapMonsterSolver(capmonster_api_key, logger=log)
                token = cm.solve_recaptcha_v2(
                    website_url=form_url,
                    website_key=site_key,
                    is_invisible=True,
                )
                cm.inject_token(driver, token)
                log("CapMonster token injected.")
            else:
                log(
                    "No reCAPTCHA site key found on page — "
                    "submitting without pre-solve."
                )

            if not _click_submit(driver, log, human):
                raise RuntimeError("Submit button not found on form.")

            auto_submitted = False
            end = time.time() + 6
            while time.time() < end:
                if _confirmation_reached(driver):
                    auto_submitted = True
                    break
                time.sleep(0.2)
            if not auto_submitted:
                log("Form did not auto-submit; clicking Submit once more.")
                _click_submit(driver, log, human)
        else:
            solver = RecaptchaSolver(
                driver,
                logger=log,
                proxy=proxy,
                speech_engine=speech_engine,
                wit_token=wit_token,
            )

            if _has_anchor_iframe(driver):
                log("Checkbox reCAPTCHA detected — clicking anchor first.")
                solver.click_anchor_if_visible(timeout=5)
                solver.solve_challenge_if_present(timeout=8)

            if not _click_submit(driver, log, human):
                raise RuntimeError("Submit button not found on form.")

            log("Waiting for reCAPTCHA challenge (if any) after submit ...")
            solved = solver.solve_challenge_if_present(timeout=10)

            if solved:
                log("Challenge solved.")

                auto_submitted = False
                end = time.time() + 6
                while time.time() < end:
                    if _confirmation_reached(driver):
                        auto_submitted = True
                        break
                    time.sleep(0.2)

                if not auto_submitted:
                    log("Form did not auto-submit; clicking Submit once more.")
                    _click_submit(driver, log, human)

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
            f"Form submission accepted by Google for {email} "
            f"(URL: {driver.current_url})."
        )

        if inbox is not None and inbox.is_configured:
            since = submit_started_at or datetime.now(timezone.utc)
            since = since.replace(second=0, microsecond=0)
            receipt = wait_for_receipt(
                inbox,
                recipient=email,
                since=since,
                timeout=inbox_timeout,
                logger=log,
            )
            if receipt is None:
                raise RuntimeError(
                    f"Form was accepted but no receipt arrived in inbox "
                    f"{inbox.username} within {int(inbox_timeout)}s. "
                    "Either the form's 'Send me a copy' opt-in wasn't "
                    "ticked, the form is set to 'never send receipts', "
                    "or Gmail throttled the receipt."
                )
    finally:
        try:
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
    proxy_pool: Optional[list] = None,
    proxy_start_index: int = 0,
    max_retries: int = 2,
    retry_backoff: float = 4.0,
    send_me_copy: bool = True,
    inbox: Optional[InboxConfig] = None,
    inbox_timeout: float = 120.0,
    use_signed_in_profile: bool = False,
    captcha_method: str = "audio",
    capmonster_api_key: Optional[str] = None,
    human: Optional[HumanBehavior] = None,
    speech_engine: str = "google",
    wit_token: Optional[str] = None,
    verify_proxy_at_startup: bool = False,
) -> SubmitResult:
    """Fill the email field and submit a single Google Form response.

    On ``RecaptchaBlockedError`` ("Try again later") we tear the browser
    down (releasing the proxy's TCP session so the next connection gets
    a fresh exit IP) and retry up to ``max_retries`` additional times.
    """
    log: Logger = logger or (lambda msg: None)
    log(f"Opening form for {email} ...")

    pool: list = list(proxy_pool) if proxy_pool else []
    if pool:
        log(
            f"Proxy pool: {len(pool)} proxies; first attempt will use #"
            f"{(proxy_start_index % len(pool)) + 1} ({pool[proxy_start_index % len(pool)].label()})"
        )
    elif proxy is not None:
        log(
            f"Using proxy {proxy.host}:{proxy.port} "
            f"(user={proxy.username or '-'})"
        )

    total_attempts = max_retries + 1
    for attempt in range(1, total_attempts + 1):
        if pool:
            cur_proxy = pool[(proxy_start_index + attempt - 1) % len(pool)]
            log(
                f"Attempt {attempt}/{total_attempts} via proxy "
                f"#{((proxy_start_index + attempt - 1) % len(pool)) + 1}: "
                f"{cur_proxy.label()}"
            )
        else:
            cur_proxy = proxy
            if attempt > 1:
                log(
                    f"Attempt {attempt}/{total_attempts} "
                    "(rotating browser / proxy IP) ..."
                )
        attempt_start = datetime.now(timezone.utc)
        try:
            _attempt_submit(
                form_url=form_url,
                email=email,
                log=log,
                headless=headless,
                proxy=cur_proxy,
                send_me_copy=send_me_copy,
                inbox=inbox,
                inbox_timeout=inbox_timeout,
                submit_started_at=attempt_start,
                use_signed_in_profile=use_signed_in_profile,
                captcha_method=captcha_method,
                capmonster_api_key=capmonster_api_key,
                human=human,
                speech_engine=speech_engine,
                wit_token=wit_token,
                verify_proxy_at_startup=verify_proxy_at_startup,
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
