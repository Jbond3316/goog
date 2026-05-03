"""
Per-browser fingerprint randomization.

Google's reCAPTCHA blocks sessions that share the same fingerprint
across repeated failures, even when the proxy IP changes. To avoid
this, every new browser session generates its own coherent
fingerprint: user agent + navigator.platform + WebGL vendor/renderer
+ screen dimensions + timezone + languages that all match one
plausible real device.

The fingerprint is applied in two places:

* Chrome launch flags — ``--user-agent`` and ``--window-size``.
* CDP-injected script (``Page.addScriptToEvaluateOnNewDocument``) that
  overrides ``navigator.*``, ``screen.*``, WebGL parameters and
  friends before any page JS runs.

Timezone and Accept-Language are applied per-driver via ``Emulation.*``
CDP commands.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class Fingerprint:
    user_agent: str
    platform: str                    # navigator.platform
    ua_platform: str                 # navigator.userAgentData platform label
    languages: List[str]
    accept_language: str
    timezone: str
    webgl_vendor: str
    webgl_renderer: str
    hardware_concurrency: int
    device_memory: int
    screen_width: int
    screen_height: int
    window_width: int = field(init=False)
    window_height: int = field(init=False)
    color_depth: int = 24

    def __post_init__(self) -> None:
        self.window_width = min(self.screen_width - 40, 1440)
        self.window_height = min(self.screen_height - 80, 900)


_WINDOWS_PROFILES = [
    # (chrome_major, webgl_vendor, webgl_renderer)
    ("Intel Inc.", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Intel Inc.", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]

_MAC_PROFILES = [
    ("Apple Inc.", "Apple M1"),
    ("Apple Inc.", "Apple M2"),
    ("Apple Inc.", "Apple M1 Pro"),
    ("Intel Inc.", "Intel Iris OpenGL Engine"),
]

_LINUX_PROFILES = [
    ("Intel Inc.", "Mesa Intel(R) UHD Graphics (CML GT2)"),
    ("AMD", "AMD Radeon Graphics (RADV RENOIR)"),
]

_CHROME_MAJORS = [122, 123, 124, 125, 126]

_COMMON_RESOLUTIONS = [
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1680, 1050),
    (1920, 1080),
    (2560, 1440),
]

_LANG_SETS = [
    (["en-US", "en"], "en-US,en;q=0.9"),
    (["en-GB", "en"], "en-GB,en;q=0.9"),
    (["en-US", "en", "es"], "en-US,en;q=0.9,es;q=0.8"),
    (["en-IN", "en"], "en-IN,en;q=0.9"),
    (["en-CA", "en", "fr"], "en-CA,en;q=0.9,fr;q=0.8"),
]

_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "America/Denver",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Amsterdam",
    "Asia/Kolkata",
    "Australia/Sydney",
]


def random_fingerprint(rng: random.Random | None = None) -> Fingerprint:
    """Build a random self-consistent browser fingerprint."""
    r = rng or random

    chrome_major = r.choice(_CHROME_MAJORS)
    chrome_version = f"{chrome_major}.0.0.0"

    os_pick = r.choices(
        population=["windows", "mac", "linux"],
        weights=[0.62, 0.28, 0.10],
        k=1,
    )[0]

    if os_pick == "windows":
        vendor, renderer = r.choice(_WINDOWS_PROFILES)
        ua = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version} Safari/537.36"
        )
        platform = "Win32"
        ua_platform = "Windows"
    elif os_pick == "mac":
        vendor, renderer = r.choice(_MAC_PROFILES)
        mac_minor = r.choice([3, 4, 5, 6, 7])
        ua = (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_{mac_minor}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version} Safari/537.36"
        )
        platform = "MacIntel"
        ua_platform = "macOS"
    else:
        vendor, renderer = r.choice(_LINUX_PROFILES)
        ua = (
            f"Mozilla/5.0 (X11; Linux x86_64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version} Safari/537.36"
        )
        platform = "Linux x86_64"
        ua_platform = "Linux"

    languages, accept_language = r.choice(_LANG_SETS)
    timezone = r.choice(_TIMEZONES)
    sw, sh = r.choice(_COMMON_RESOLUTIONS)
    cores = r.choice([4, 8, 8, 12, 16])
    mem = r.choice([4, 8, 8, 16])

    return Fingerprint(
        user_agent=ua,
        platform=platform,
        ua_platform=ua_platform,
        languages=languages,
        accept_language=accept_language,
        timezone=timezone,
        webgl_vendor=vendor,
        webgl_renderer=renderer,
        hardware_concurrency=cores,
        device_memory=mem,
        screen_width=sw,
        screen_height=sh,
    )


def build_stealth_js(fp: Fingerprint) -> str:
    """Return JS that, when injected via Page.addScriptToEvaluateOnNewDocument,
    overrides navigator, screen, and WebGL to match the given fingerprint."""
    payload = {
        "platform": fp.platform,
        "languages": fp.languages,
        "hardwareConcurrency": fp.hardware_concurrency,
        "deviceMemory": fp.device_memory,
        "screenWidth": fp.screen_width,
        "screenHeight": fp.screen_height,
        "colorDepth": fp.color_depth,
        "webglVendor": fp.webgl_vendor,
        "webglRenderer": fp.webgl_renderer,
    }
    payload_js = json.dumps(payload)

    return (
        "(function() {\n"
        f"  var FP = {payload_js};\n"
        + r"""
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'platform', { get: () => FP.platform });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'languages', { get: () => FP.languages });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => FP.hardwareConcurrency });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'deviceMemory', { get: () => FP.deviceMemory });
  } catch (e) {}
  try {
    Object.defineProperty(screen, 'width', { get: () => FP.screenWidth });
    Object.defineProperty(screen, 'height', { get: () => FP.screenHeight });
    Object.defineProperty(screen, 'availWidth', { get: () => FP.screenWidth });
    Object.defineProperty(screen, 'availHeight', { get: () => FP.screenHeight - 40 });
    Object.defineProperty(screen, 'colorDepth', { get: () => FP.colorDepth });
    Object.defineProperty(screen, 'pixelDepth', { get: () => FP.colorDepth });
  } catch (e) {}

  window.chrome = window.chrome || { runtime: {} };

  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => [
        { name: 'PDF Viewer' },
        { name: 'Chrome PDF Viewer' },
        { name: 'Chromium PDF Viewer' },
        { name: 'Microsoft Edge PDF Viewer' },
        { name: 'WebKit built-in PDF' }
      ]
    });
  } catch (e) {}

  var origQuery = navigator.permissions && navigator.permissions.query;
  if (origQuery) {
    navigator.permissions.query = function (p) {
      return p && p.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : origQuery.call(navigator.permissions, p);
    };
  }

  try {
    var gp = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (parameter) {
      if (parameter === 37445) return FP.webglVendor;      // UNMASKED_VENDOR_WEBGL
      if (parameter === 37446) return FP.webglRenderer;    // UNMASKED_RENDERER_WEBGL
      if (parameter === 7936)  return FP.webglVendor;      // VENDOR
      if (parameter === 7937)  return FP.webglRenderer;    // RENDERER
      return gp.call(this, parameter);
    };
    if (window.WebGL2RenderingContext) {
      var gp2 = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = function (parameter) {
        if (parameter === 37445) return FP.webglVendor;
        if (parameter === 37446) return FP.webglRenderer;
        if (parameter === 7936)  return FP.webglVendor;
        if (parameter === 7937)  return FP.webglRenderer;
        return gp2.call(this, parameter);
      };
    }
  } catch (e) {}

  try {
    var toDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function () {
      var ctx = this.getContext('2d');
      if (ctx) {
        var d = ctx.getImageData(0, 0, this.width, this.height);
        for (var i = 0; i < d.data.length; i += 97) {
          d.data[i] = d.data[i] ^ 1;
        }
        ctx.putImageData(d, 0, 0);
      }
      return toDataURL.apply(this, arguments);
    };
  } catch (e) {}
})();
"""
    )
