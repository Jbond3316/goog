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

    def label(self) -> str:
        return f"{self.host}:{self.port}"


def parse_proxy_line(line: str) -> "ProxyConfig | None":
    """Parse one line of the multi-proxy textarea. Returns None for
    blanks, comments, or unparseable lines.

    Accepts (auto-detected):
      * host:port:username:password   (Bright Data / SmartProxy style)
      * host:port
      * username:password@host:port
      * http(s)://username:password@host:port
    """
    from urllib.parse import urlparse

    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None

    if s.startswith(("http://", "https://", "socks5://", "socks4://")):
        try:
            p = urlparse(s)
        except ValueError:
            return None
        if not p.hostname or not p.port:
            return None
        return ProxyConfig(
            host=p.hostname,
            port=int(p.port),
            username=p.username or "",
            password=p.password or "",
            scheme=p.scheme or "http",
        )

    if "@" in s:
        creds, _, hostport = s.rpartition("@")
        host, _, port = hostport.rpartition(":")
        if not host or not port.isdigit():
            return None
        user, _, pwd = creds.partition(":")
        return ProxyConfig(
            host=host, port=int(port), username=user, password=pwd
        )

    parts = s.split(":")
    if len(parts) == 4 and parts[1].isdigit():
        host, port, user, pwd = parts
        return ProxyConfig(
            host=host, port=int(port), username=user, password=pwd
        )
    if len(parts) == 2 and parts[1].isdigit():
        host, port = parts
        return ProxyConfig(host=host, port=int(port))

    return None


def parse_proxy_lines(text: str) -> "list[ProxyConfig]":
    """Parse the multi-proxy textarea content. Silently skips blanks,
    comments, and unparseable lines."""
    out: "list[ProxyConfig]" = []
    if not text:
        return out
    for raw in str(text).splitlines():
        cfg = parse_proxy_line(raw)
        if cfg is not None:
            out.append(cfg)
    return out


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
    """Recursively remove a directory. Safe for both the proxy-auth
    extension and per-browser user-data-dirs."""
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
