"""
Helpers for using an authenticated HTTP proxy with Selenium / Chrome.

Chrome doesn't accept credentials embedded in the `--proxy-server` flag,
so the canonical workaround is to package a tiny Chrome extension that
wires up `chrome.proxy.settings` and responds to `onAuthRequired` with
the supplied username/password.

This module builds that extension in a temp directory on the fly.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class ProxyConfig:
    host: str
    port: int
    username: str = ""
    password: str = ""
    scheme: str = "http"

    def as_requests_proxies(self) -> Dict[str, str]:
        """Return a dict suitable for `requests.get(..., proxies=...)`."""
        userinfo = ""
        if self.username:
            userinfo = f"{self.username}:{self.password}@"
        url = f"{self.scheme}://{userinfo}{self.host}:{self.port}"
        return {"http": url, "https": url}


_BACKGROUND_JS_TEMPLATE = r"""
var config = {
  mode: "fixed_servers",
  rules: {
    singleProxy: {
      scheme: "__SCHEME__",
      host: "__HOST__",
      port: parseInt(__PORT__)
    },
    bypassList: ["localhost", "127.0.0.1"]
  }
};

chrome.proxy.settings.set({ value: config, scope: "regular" }, function () {});

function callbackFn(details) {
  return {
    authCredentials: {
      username: "__USERNAME__",
      password: "__PASSWORD__"
    }
  };
}

chrome.webRequest.onAuthRequired.addListener(
  callbackFn,
  { urls: ["<all_urls>"] },
  ["blocking"]
);
"""


_MANIFEST = {
    "version": "1.0.0",
    "manifest_version": 2,
    "name": "Selenium HTTP Proxy Auth",
    "permissions": [
        "proxy",
        "tabs",
        "unlimitedStorage",
        "storage",
        "<all_urls>",
        "webRequest",
        "webRequestBlocking",
    ],
    "background": {"scripts": ["background.js"]},
    "minimum_chrome_version": "22.0.0",
}


def build_proxy_auth_extension(proxy: ProxyConfig) -> str:
    """Create a temporary directory containing an unpacked Chrome extension
    that routes traffic through the given authenticated proxy. Returns the
    path to the extension directory. Caller is responsible for cleaning
    it up (see :func:`cleanup_extension`).
    """
    ext_dir = tempfile.mkdtemp(prefix="chrome_proxy_ext_")

    with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(_MANIFEST, f)

    bg = (
        _BACKGROUND_JS_TEMPLATE.replace("__SCHEME__", _js_str(proxy.scheme))
        .replace("__HOST__", _js_str(proxy.host))
        .replace("__PORT__", str(int(proxy.port)))
        .replace("__USERNAME__", _js_str(proxy.username))
        .replace("__PASSWORD__", _js_str(proxy.password))
    )
    with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
        f.write(bg)

    return ext_dir


def cleanup_extension(path: Optional[str]) -> None:
    if not path:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _js_str(value: str) -> str:
    """Escape a value for safe inclusion inside a JS double-quoted string."""
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )
