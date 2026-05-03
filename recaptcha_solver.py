"""
Audio-based Google reCAPTCHA solver.

Ported from https://github.com/sarperavci/GoogleRecaptchaBypass (selenium branch)
and adapted for use inside a Flask web app. The solver drives a Selenium
WebDriver, clicks through to the audio challenge, downloads the MP3, converts
to WAV with pydub/ffmpeg, and transcribes it with Google's free
speech-recognition endpoint.
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
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


class RecaptchaSolver:
    """Solve a Google reCAPTCHA v2 challenge using the audio challenge path."""

    def __init__(self, driver: WebDriver, logger=print) -> None:
        self.driver = driver
        self.log = logger

    def solve(self) -> bool:
        """Attempt to solve the reCAPTCHA currently displayed on the page.

        Returns True on success, raises on failure.
        """
        try:
            iframe_outer = WebDriverWait(self.driver, 15).until(
                EC.frame_to_be_available_and_switch_to_it(
                    (By.XPATH, "//iframe[contains(@title, 'reCAPTCHA')]")
                )
            )
            _ = iframe_outer

            WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable((By.ID, "recaptcha-anchor"))
            ).click()

            time.sleep(1.5)
            if self._is_solved():
                self.log("reCAPTCHA solved by simple click.")
                self.driver.switch_to.default_content()
                return True

            return self._solve_audio()

        except Exception as exc:
            self.log(f"reCAPTCHA solve error: {exc}")
            self.driver.switch_to.default_content()
            raise

    def _solve_audio(self) -> bool:
        try:
            self.driver.switch_to.default_content()

            WebDriverWait(self.driver, 15).until(
                EC.frame_to_be_available_and_switch_to_it(
                    (
                        By.XPATH,
                        "//iframe[contains(@title, 'recaptcha challenge')]",
                    )
                )
            )

            audio_btn = WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable((By.ID, "recaptcha-audio-button"))
            )
            audio_btn.click()

            if self._is_detected():
                raise RuntimeError(
                    "Google flagged this session as bot traffic "
                    "('Try again later'). Try a different network or slow down."
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
            response_input.send_keys(text)
            response_input.send_keys(Keys.ENTER)

            time.sleep(1.5)

            if self._is_solved():
                self.log("Audio reCAPTCHA solved.")
                self.driver.switch_to.default_content()
                return True

            raise RuntimeError("Audio reCAPTCHA was not accepted.")
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

    def _is_solved(self) -> bool:
        try:
            self.driver.switch_to.default_content()
            iframe = self.driver.find_element(
                By.XPATH, "//iframe[contains(@title, 'reCAPTCHA')]"
            )
            self.driver.switch_to.frame(iframe)
            checkbox = WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located((By.ID, "recaptcha-anchor"))
            )
            aria = checkbox.get_attribute("aria-checked") or ""
            classes = checkbox.get_attribute("class") or ""
            return aria == "true" or "recaptcha-checkbox-checked" in classes
        except Exception:
            return False
        finally:
            self.driver.switch_to.default_content()

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
        except Exception:
            return None
