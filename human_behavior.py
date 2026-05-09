"""
Human-behaviour helpers for form-fill flows.

Re-introduces realistic typing cadence and mouse movement, configurable
via :class:`HumanBehavior`. Defaults match the values requested by the
user:

  * mouse_speed   = 15-20 (pixels per step in the move chain)
  * keyboard_delay = 100-150 ms per character
  * screen        = 1920 x 1080
  * browser_type  = Chrome (always; we don't drive other browsers)

When ``HumanBehavior.enabled`` is False, the helpers fall back to the
fast "paste in one shot" mode.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Optional

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains


@dataclass
class HumanBehavior:
    enabled: bool = True
    # Per-step mouse movement size in pixels. Smaller -> more steps,
    # smoother motion, slightly slower.
    mouse_speed_min: int = 15
    mouse_speed_max: int = 20
    # Inter-character typing delay in milliseconds.
    keyboard_delay_min: int = 100
    keyboard_delay_max: int = 150
    # Forced screen + window dimensions when human mode is on. Overrides
    # the random resolution from ``fingerprint.random_fingerprint``.
    screen_width: int = 1920
    screen_height: int = 1080
    browser_type: str = "Chrome"  # informational; we always use Chromium

    def __post_init__(self) -> None:
        # Sanity: ensure min <= max
        if self.mouse_speed_min > self.mouse_speed_max:
            self.mouse_speed_min, self.mouse_speed_max = (
                self.mouse_speed_max,
                self.mouse_speed_min,
            )
        if self.keyboard_delay_min > self.keyboard_delay_max:
            self.keyboard_delay_min, self.keyboard_delay_max = (
                self.keyboard_delay_max,
                self.keyboard_delay_min,
            )
        # Clamp mouse step to reasonable bounds
        self.mouse_speed_min = max(1, min(self.mouse_speed_min, 200))
        self.mouse_speed_max = max(self.mouse_speed_min, min(self.mouse_speed_max, 400))
        # Clamp typing delay to reasonable bounds
        self.keyboard_delay_min = max(0, min(self.keyboard_delay_min, 2000))
        self.keyboard_delay_max = max(
            self.keyboard_delay_min, min(self.keyboard_delay_max, 5000)
        )

    @property
    def window_width(self) -> int:
        return min(self.screen_width - 40, 1480)

    @property
    def window_height(self) -> int:
        return min(self.screen_height - 80, 940)


def human_type(
    element,
    text: str,
    cfg: HumanBehavior,
    rng: Optional[random.Random] = None,
) -> None:
    """Type ``text`` into ``element`` one character at a time with a
    randomised inter-character delay. Falls back to a single send_keys
    if ``cfg.enabled`` is False."""
    if not cfg.enabled:
        element.send_keys(text)
        return
    rng = rng or random.Random()
    for ch in text:
        element.send_keys(ch)
        ms = rng.randint(cfg.keyboard_delay_min, cfg.keyboard_delay_max)
        time.sleep(ms / 1000.0)
        # Tiny chance of a small "thinking" pause
        if rng.random() < 0.04:
            time.sleep(rng.uniform(0.25, 0.55))


def human_warmup(
    driver,
    cfg: HumanBehavior,
    rng: Optional[random.Random] = None,
) -> None:
    """A few small mouse moves around the viewport before any real
    interaction. reCAPTCHA v3 looks for ANY pre-click motion."""
    if not cfg.enabled:
        return
    rng = rng or random.Random()
    try:
        actions = ActionChains(driver)
        for _ in range(rng.randint(2, 4)):
            dx = rng.randint(-cfg.mouse_speed_max * 5, cfg.mouse_speed_max * 5)
            dy = rng.randint(-cfg.mouse_speed_max * 4, cfg.mouse_speed_max * 4)
            actions.move_by_offset(dx, dy)
            actions.pause(rng.uniform(0.05, 0.15))
        actions.perform()
    except WebDriverException:
        pass


def human_move_and_click(
    driver,
    element,
    cfg: HumanBehavior,
    rng: Optional[random.Random] = None,
) -> None:
    """Scroll ``element`` into view, move the mouse to it via a multi-step
    ActionChains motion (pixels-per-step taken from
    ``cfg.mouse_speed_min..mouse_speed_max``), pause briefly, and click."""
    rng = rng or random.Random()

    if not cfg.enabled:
        try:
            element.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", element)
        return

    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", element
        )
        time.sleep(rng.uniform(0.15, 0.35))
    except WebDriverException:
        pass

    try:
        size = element.size or {}
        w = max(int(size.get("width") or 1), 1)
        h = max(int(size.get("height") or 1), 1)
        # Click at a slightly randomized offset inside the element so
        # repeated clicks don't all land at the exact same pixel.
        ox = rng.randint(-w // 4, w // 4)
        oy = rng.randint(-h // 4, h // 4)

        actions = ActionChains(driver)

        # A handful of small approach jitters first, with motion step
        # size drawn from the configured range.
        for _ in range(rng.randint(2, 4)):
            step_x = rng.randint(cfg.mouse_speed_min, cfg.mouse_speed_max)
            step_y = rng.randint(cfg.mouse_speed_min, cfg.mouse_speed_max)
            actions.move_by_offset(
                step_x * rng.choice([-1, 1]),
                step_y * rng.choice([-1, 1]),
            )
            actions.pause(rng.uniform(0.02, 0.08))

        actions.move_to_element_with_offset(element, ox, oy)
        actions.pause(rng.uniform(0.15, 0.35))
        actions.click()
        actions.perform()
    except WebDriverException:
        try:
            element.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", element)
