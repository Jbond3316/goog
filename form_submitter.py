"""
Google Form submitter.

Fills a single email field on a Google Form's /viewform page, solves the
reCAPTCHA (audio challenge) if one is present, and submits. Supports both
the "I'm not a robot" checkbox variant and the invisible reCAPTCHA that
fires from Submit click (common on Google Forms today).
"""

from __future__ import annotations

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
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from recaptcha_solver import RecaptchaSolver, ANCHOR_IFRAME_XPATH


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


def _build_driver(headless: bool = True) -> webdriver.Chrome:
    opts = ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=en-US")
    opts.add_experimental_option(
        "excludeSwitches", ["enable-automation", "enable-logging"]
    )
    opts.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=opts)
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            },
        )
    except WebDriverException:
        pass
    return driver


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


def _click_submit(driver, log: Logger) -> bool:
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


def _confirmation_reached(driver) -> bool:
    url = driver.current_url or ""
    if "formResponse" in url:
        return True
    try:
        src = driver.page_source
    except WebDriverException:
        return False
    markers = [
        "Your response has been recorded",
        "Your response was recorded",
        "Thanks for submitting",
        "Thank you for submitting",
        "We've recorded",
    ]
    return any(m.lower() in src.lower() for m in markers)


def submit_form(
    form_url: str,
    email: str,
    logger: Optional[Logger] = None,
    headless: bool = True,
) -> SubmitResult:
    """Fill the email field and submit a single Google Form response."""
    log: Logger = logger or (lambda msg: None)
    log(f"Opening form for {email} ...")

    driver = _build_driver(headless=headless)
    try:
        driver.get(form_url)

        email_input = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (By.XPATH, "//input[@type='email' or @type='text']")
            )
        )
        email_input.clear()
        email_input.send_keys(email)
        log(f"Filled email: {email}")

        solver = RecaptchaSolver(driver, logger=log)

        if _has_anchor_iframe(driver):
            log("Checkbox reCAPTCHA detected — clicking anchor first.")
            solver.click_anchor_if_visible(timeout=5)
            solver.solve_challenge_if_present(timeout=8)

        if not _click_submit(driver, log):
            raise RuntimeError("Submit button not found on form.")

        log("Waiting for reCAPTCHA challenge (if any) after submit ...")
        solved = solver.solve_challenge_if_present(timeout=10)

        if solved:
            log("Challenge solved. Waiting to see if form auto-submits ...")
            auto_submitted = False
            end = time.time() + 6
            while time.time() < end:
                if _confirmation_reached(driver):
                    auto_submitted = True
                    break
                time.sleep(0.5)

            if not auto_submitted:
                log("Form did not auto-submit; clicking Submit once more.")
                _click_submit(driver, log)

        try:
            WebDriverWait(driver, 25).until(lambda d: _confirmation_reached(d))
        except TimeoutException:
            if not _confirmation_reached(driver):
                raise RuntimeError(
                    "Form did not reach a confirmation page. "
                    "The captcha may have been rejected or the form requires "
                    "additional fields."
                )

        log(f"Submission confirmed for {email}.")
        return SubmitResult(email=email, success=True, message="Submitted successfully")

    except Exception as exc:
        log(f"Error submitting for {email}: {exc}")
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        return SubmitResult(email=email, success=False, message=msg or exc.__class__.__name__)
    finally:
        try:
            time.sleep(0.5)
            driver.quit()
        except Exception:
            pass
