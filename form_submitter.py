"""
Google Form submitter.

Loads a Google Form that has a single email field and a reCAPTCHA v2
("I'm not a robot") widget. Fills in the email, solves the audio captcha
using `recaptcha_solver.RecaptchaSolver`, and submits the form.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from recaptcha_solver import RecaptchaSolver


Logger = Callable[[str], None]


@dataclass
class SubmitResult:
    email: str
    success: bool
    message: str


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
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        },
    )
    return driver


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

        has_captcha = False
        try:
            WebDriverWait(driver, 3).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//iframe[contains(@title, 'reCAPTCHA')]")
                )
            )
            has_captcha = True
        except Exception:
            has_captcha = False

        if has_captcha:
            log("reCAPTCHA detected — solving via audio challenge ...")
            RecaptchaSolver(driver, logger=log).solve()
        else:
            log("No reCAPTCHA on page; proceeding to submit.")

        submit_btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//div[@role='button' and .//span[contains(., 'Submit') "
                    "or contains(., 'submit') or contains(., 'Gửi') "
                    "or contains(., 'Enviar')]]",
                )
            )
        )
        submit_btn.click()
        log("Clicked submit.")

        WebDriverWait(driver, 20).until(
            lambda d: "formResponse" in d.current_url
            or "Your response has been recorded" in d.page_source
            or "Thanks for" in d.page_source
            or "thank" in d.page_source.lower()
        )

        log(f"Submission confirmed for {email}.")
        return SubmitResult(email=email, success=True, message="Submitted successfully")

    except Exception as exc:
        log(f"Error submitting for {email}: {exc}")
        return SubmitResult(email=email, success=False, message=str(exc))
    finally:
        try:
            time.sleep(0.5)
            driver.quit()
        except Exception:
            pass
