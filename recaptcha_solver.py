"""
Audio-based Google reCAPTCHA solver.

Ported from https://github.com/sarperavci/GoogleRecaptchaBypass (selenium branch)
and adapted for use inside a Flask web app.

Supports two reCAPTCHA v2 flows:

1. Checkbox ("I'm not a robot") — click the anchor, which may or may not
   open an audio/image challenge iframe.
2. Invisible reCAPTCHA — the challenge iframe appears only after the
   page's real Submit button is clicked (common on Google Forms).

The public entry points are:

* ``click_anchor_if_visible()`` — best-effort checkbox click. Never raises.
* ``solve_challenge_if_present(timeout)`` — waits for a challenge iframe
  and solves it via audio. Returns True if a challenge was solved,
  False if none appeared within the timeout. Raises only if a challenge
  appeared and could not be solved.
"""

from __future__ import annotations

import os
import random
import tempfile
import time
from typing import Optional

import requests
from pydub import AudioSegment
import speech_recognition as sr
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


ANCHOR_IFRAME_XPATH = "//iframe[contains(@title, 'reCAPTCHA')]"
CHALLENGE_IFRAME_XPATH = "//iframe[contains(@title, 'recaptcha challenge')]"


class RecaptchaSolver:
    """Solve a Google reCAPTCHA v2 challenge using the audio challenge path."""

    def __init__(self, driver: WebDriver, logger=print) -> None:
        self.driver = driver
        self.log = logger

    def click_anchor_if_visible(self, timeout: float = 3.0) -> bool:
        """Click the "I'm not a robot" checkbox if the anchor iframe exists
        and is displayed. Returns True if we clicked it.

        Never raises; an invisible reCAPTCHA form will just return False.
        """
        try:
            self.driver.switch_to.default_content()
            iframes = self.driver.find_elements(By.XPATH, ANCHOR_IFRAME_XPATH)
            if not iframes:
                return False
            iframe = iframes[0]
            size = iframe.size or {}
            if (size.get("width") or 0) < 20 or (size.get("height") or 0) < 20:
                return False

            self.driver.switch_to.frame(iframe)
            try:
                anchor = WebDriverWait(self.driver, timeout).until(
                    EC.element_to_be_clickable((By.ID, "recaptcha-anchor"))
                )
                anchor.click()
                self.log("Clicked 'I'm not a robot' checkbox.")
                time.sleep(1.5)
                return True
            except TimeoutException:
                return False
            finally:
                self.driver.switch_to.default_content()
        except WebDriverException as exc:
            self.log(f"Anchor click skipped: {exc}")
            self.driver.switch_to.default_content()
            return False

    def solve_challenge_if_present(self, timeout: float = 10.0) -> bool:
        """Wait up to ``timeout`` seconds for an audio/image challenge iframe.

        If one appears, solve it via the audio track. Returns True if a
        challenge was solved, False if none appeared. Raises RuntimeError
        if a challenge appeared but could not be solved, or if Google
        blocks the session.
        """
        end = time.time() + timeout
        while time.time() < end:
            try:
                self.driver.switch_to.default_content()
                iframes = self.driver.find_elements(By.XPATH, CHALLENGE_IFRAME_XPATH)
                visible = [f for f in iframes if self._iframe_visible(f)]
                if visible:
                    self.log("reCAPTCHA challenge iframe detected.")
                    return self._solve_audio(visible[0])
            except WebDriverException:
                pass
            time.sleep(0.3)
        return False

    def _solve_audio(self, challenge_iframe) -> bool:
        try:
            self.driver.switch_to.frame(challenge_iframe)

            try:
                audio_btn = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable((By.ID, "recaptcha-audio-button"))
                )
                audio_btn.click()
                self.log("Switched to audio challenge.")
                time.sleep(0.5)
            except TimeoutException:
                pass

            if self._is_detected():
                raise RuntimeError(
                    "Google flagged this session as bot traffic "
                    "('Try again later'). Try a different IP or slow down."
                )

            audio_src = (
                WebDriverWait(self.driver, 15)
                .until(EC.presence_of_element_located((By.ID, "audio-source")))
                .get_attribute("src")
            )
            self.log(f"Downloading audio challenge: {audio_src}")

            text = self._transcribe_audio(audio_src)
            self.log(f"Transcribed audio: {text!r}")

            response_input = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.ID, "audio-response"))
            )
            response_input.clear()
            response_input.send_keys(text)
            response_input.send_keys(Keys.ENTER)

            time.sleep(2.0)

            if self._is_detected():
                raise RuntimeError(
                    "Google rejected the audio response ('Try again later')."
                )

            self.log("Audio reCAPTCHA submitted.")
            return True
        finally:
            self.driver.switch_to.default_content()

    def _transcribe_audio(self, url: str) -> str:
        tmp_dir = tempfile.gettempdir()
        suffix = random.randrange(1, 1_000_000)
        mp3_path = os.path.join(tmp_dir, f"recap_{suffix}.mp3")
        wav_path = os.path.join(tmp_dir, f"recap_{suffix}.wav")

        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            with open(mp3_path, "wb") as f:
                f.write(resp.content)

            sound = AudioSegment.from_mp3(mp3_path)
            sound.export(wav_path, format="wav")

            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio = recognizer.record(source)
            return recognizer.recognize_google(audio).lower()
        finally:
            for p in (mp3_path, wav_path):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass

    def _iframe_visible(self, iframe) -> bool:
        try:
            if not iframe.is_displayed():
                return False
            size = iframe.size or {}
            return (size.get("width") or 0) > 20 and (size.get("height") or 0) > 20
        except WebDriverException:
            return False

    def _is_detected(self) -> bool:
        try:
            return "Try again later" in self.driver.page_source
        except Exception:
            return False

    def get_token(self) -> Optional[str]:
        try:
            self.driver.switch_to.default_content()
            elem = self.driver.find_element(By.ID, "g-recaptcha-response")
            return elem.get_attribute("value") or None
        except NoSuchElementException:
            return None
