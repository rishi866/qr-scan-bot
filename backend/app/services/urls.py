"""Strict validation of the ChatGPT URL a seller submits.

The URL is forwarded to another human, so we only accept plain ``https`` links whose *host* is
(a subdomain of) an allow-listed domain. Everything else - look-alike hosts, ``user@host`` tricks,
other ports, control characters, several links in one message - is rejected.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048
_CONTROL_OR_SPACE = re.compile(r"[\s\x00-\x1f\x7f​-‏  ﻿]")


class InvalidUrl(ValueError):
    """Message is safe to show to the seller."""


def _host_allowed(host: str, allowed: Iterable[str]) -> bool:
    host = host.rstrip(".").lower()
    for domain in allowed:
        domain = domain.strip().lower().lstrip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            return True
    return False


def validate_chatgpt_url(raw: str, allowed_domains: Iterable[str]) -> str:
    """Return the cleaned URL or raise :class:`InvalidUrl`."""
    url = (raw or "").strip()
    if not url:
        raise InvalidUrl("Please send the ChatGPT URL as text.")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidUrl("That link is too long.")
    if _CONTROL_OR_SPACE.search(url):
        raise InvalidUrl("Please send only one link, without spaces or line breaks.")
    if not url.lower().startswith("https://"):
        raise InvalidUrl("The link must start with https://")

    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError for garbage ports
    except ValueError as exc:
        raise InvalidUrl("That doesn't look like a valid link.") from exc

    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise InvalidUrl("Links containing a username/password are not allowed.")
    if port not in (None, 443):
        raise InvalidUrl("Links with a custom port are not allowed.")

    host = (parts.hostname or "").rstrip(".").lower()
    if not host or "." not in host:
        raise InvalidUrl("That doesn't look like a valid link.")
    try:
        host_ascii = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidUrl("That doesn't look like a valid link.") from exc
    if host_ascii != host:  # internationalised hostnames are a classic look-alike vector
        raise InvalidUrl("Only official ChatGPT links are accepted.")

    if not _host_allowed(host, allowed_domains):
        raise InvalidUrl("Only official ChatGPT links are accepted (chatgpt.com).")
    return url
