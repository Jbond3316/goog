"""
IMAP-based delivery verification.

After a Google Form submission is accepted by Google, this module polls
the recipient's inbox to confirm the receipt email actually arrives.
This catches cases where Google's response counter increments but the
form's email receipt is dropped (rate-limited by Gmail, not requested
because the 'Send me a copy' opt-in was missing on the form, etc).

Designed for Gmail (imap.gmail.com:993, SSL), but works with any IMAP
provider that supports SEARCH and PEEKing FETCH RFC822.HEADER.

NOTE: Gmail blocks IMAP login with the regular account password. The
account must have 2-Step Verification enabled and the IMAP password
must be a 16-character App Password generated at
https://myaccount.google.com/apppasswords.
"""

from __future__ import annotations

import email
import imaplib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Callable, List, Optional


Logger = Callable[[str], None]


@dataclass
class InboxConfig:
    host: str
    port: int = 993
    username: str = ""
    password: str = ""
    use_ssl: bool = True
    mailbox: str = "INBOX"

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.username and self.password)


@dataclass
class ReceiptInfo:
    uid: str
    from_addr: str
    to_addr: str
    subject: str
    date: Optional[datetime]


# Form receipt emails always come from this address.
RECEIPT_SENDERS = (
    "forms-receipts-noreply@google.com",
)


def _normalize_address(addr: str) -> str:
    """Normalize a Gmail address for comparison: lowercase, drop +suffix
    on the local part, treat googlemail.com == gmail.com."""
    if not addr:
        return ""
    name, email_addr = parseaddr(addr)
    addr_l = (email_addr or addr).strip().lower()
    if "@" not in addr_l:
        return addr_l
    local, _, domain = addr_l.partition("@")
    local = local.split("+", 1)[0]
    if domain == "googlemail.com":
        domain = "gmail.com"
    return f"{local}@{domain}"


def _connect(cfg: InboxConfig) -> imaplib.IMAP4:
    if cfg.use_ssl:
        conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(cfg.host, cfg.port)
    else:
        conn = imaplib.IMAP4(cfg.host, cfg.port)
    conn.login(cfg.username, cfg.password)
    conn.select(cfg.mailbox, readonly=True)
    return conn


def _imap_date(dt: datetime) -> str:
    """Format an IMAP SEARCH SINCE date (DD-Mon-YYYY)."""
    return dt.strftime("%d-%b-%Y")


def _parse_uid_list(data: bytes) -> List[str]:
    if not data:
        return []
    return [u.decode() for u in data.split() if u]


def wait_for_receipt(
    cfg: InboxConfig,
    recipient: str,
    since: Optional[datetime] = None,
    timeout: float = 120.0,
    poll_interval: float = 6.0,
    logger: Optional[Logger] = None,
) -> Optional[ReceiptInfo]:
    """Poll the inbox until a Google Form receipt addressed to
    ``recipient`` arrives, or timeout elapses.

    A match must satisfy ALL of:
      * From address is forms-receipts-noreply@google.com
      * To address (or any header recipient) normalizes to the same
        address as ``recipient`` (handles +tags and gmail/googlemail).
      * Internal date >= ``since`` (defaults to now-2min).

    Returns the ``ReceiptInfo`` of the first match, or None on timeout.
    """
    log: Logger = logger or (lambda msg: None)

    if not cfg.is_configured:
        raise ValueError("IMAP credentials are not configured")

    target = _normalize_address(recipient)
    if not target:
        raise ValueError(f"recipient {recipient!r} is not a valid email")

    if since is None:
        since = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    seen_uids: set[str] = set()
    deadline = time.time() + timeout

    log(
        f"IMAP: watching {cfg.username}@{cfg.host} for receipt to "
        f"{target} (timeout {int(timeout)}s) ..."
    )

    while time.time() < deadline:
        try:
            conn = _connect(cfg)
        except imaplib.IMAP4.error as exc:
            raise RuntimeError(
                f"IMAP login failed for {cfg.username}: {exc}. "
                "If this is Gmail, ensure 2-Step Verification is on and "
                "use a 16-character App Password "
                "(https://myaccount.google.com/apppasswords)."
            ) from exc

        try:
            search_clauses = [
                "SINCE",
                _imap_date(since - _one_day()),
                "FROM",
                f'"{RECEIPT_SENDERS[0]}"',
            ]
            status, data = conn.search(None, *search_clauses)
            if status != "OK":
                log(f"IMAP search returned {status}, retrying ...")
            else:
                uids = _parse_uid_list(data[0] if data else b"")
                new_uids = [u for u in uids if u not in seen_uids]
                if new_uids:
                    log(
                        f"IMAP: {len(new_uids)} new receipt(s) since last poll, "
                        f"checking recipients ..."
                    )
                for uid in reversed(new_uids):
                    seen_uids.add(uid)
                    info = _fetch_headers(conn, uid)
                    if info is None:
                        continue
                    if info.date is not None and info.date < since:
                        continue
                    if _normalize_address(info.to_addr) == target:
                        log(
                            f"IMAP: receipt found! "
                            f"subject={info.subject!r}, "
                            f"to={info.to_addr}, "
                            f"date={info.date.isoformat() if info.date else '?'}"
                        )
                        return info
        finally:
            try:
                conn.logout()
            except Exception:
                pass

        time.sleep(poll_interval)

    log(f"IMAP: no receipt arrived for {target} within {int(timeout)}s.")
    return None


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


def _fetch_headers(conn: imaplib.IMAP4, uid: str) -> Optional[ReceiptInfo]:
    try:
        status, msg_data = conn.fetch(uid, "(BODY.PEEK[HEADER])")
    except imaplib.IMAP4.error:
        return None
    if status != "OK" or not msg_data:
        return None

    header_bytes = b""
    for part in msg_data:
        if isinstance(part, tuple) and len(part) >= 2:
            header_bytes = part[1]
            break
    if not header_bytes:
        return None

    msg = email.message_from_bytes(header_bytes)
    from_addr = msg.get("From", "")
    subject = msg.get("Subject", "")
    date_hdr = msg.get("Date", "")

    to_candidates = []
    for header_name in ("Delivered-To", "To", "Cc", "Bcc", "X-Original-To"):
        for v in msg.get_all(header_name, []):
            to_candidates.extend(addr for _, addr in getaddresses([v]))

    parsed_dt: Optional[datetime] = None
    if date_hdr:
        try:
            parsed_dt = parsedate_to_datetime(date_hdr)
            if parsed_dt and parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            parsed_dt = None

    primary_to = to_candidates[0] if to_candidates else ""
    return ReceiptInfo(
        uid=uid,
        from_addr=from_addr,
        to_addr=primary_to,
        subject=subject,
        date=parsed_dt,
    )


def test_login(cfg: InboxConfig) -> None:
    """Raise on failure, return on success. Used for the 'Test' button."""
    if not cfg.is_configured:
        raise ValueError("IMAP credentials are not configured")
    conn = _connect(cfg)
    try:
        conn.noop()
    finally:
        try:
            conn.logout()
        except Exception:
            pass


_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def looks_like_email(s: str) -> bool:
    return bool(_EMAIL_RE.fullmatch((s or "").strip()))
