"""Shared validation for server-facing SSO callback targets."""

import ipaddress
import re
from urllib.parse import urlsplit


BACKCHANNEL_LOGOUT_PATH = "/api/v1/auth/keycloak/backchannel-logout"
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


def _is_callback_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    if ":" in value or value.endswith(".") or len(value) > 253:
        return False
    # Refuse IPv4-looking alternatives that different URL consumers may
    # normalize differently (for example, dotted octal or a short address).
    if value.replace(".", "").isdigit():
        return False
    return all(_DNS_LABEL_RE.fullmatch(label) for label in value.split("."))


def _has_canonical_authority(value: str, hostname: str) -> bool:
    authority = urlsplit(value).netloc
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0 or authority[1:closing] != hostname:
            return False
        suffix = authority[closing + 1 :]
        return not suffix or (suffix.startswith(":") and len(suffix) > 1 and suffix[1:].isdigit())
    if authority.count(":") > 1:
        return False
    authority_host, separator, port_text = authority.rpartition(":")
    if not separator:
        return authority == hostname
    return authority_host == hostname and bool(port_text) and port_text.isdigit()


def is_backchannel_logout_uri(value: str) -> bool:
    """Accept only the exact HTTP(S) endpoint owned by AKB."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or not value.isascii()
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
        or "?" in value
        or "#" in value
        or "\\" in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except TypeError, ValueError:
        return False
    hostname = parsed.hostname
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.scheme == parsed.scheme.lower()
        and hostname
        and hostname == hostname.lower()
        and _is_callback_host(hostname)
        and _has_canonical_authority(value, hostname)
        and parsed.username is None
        and parsed.password is None
        and (port is None or 1 <= port <= 65535)
        and parsed.path == BACKCHANNEL_LOGOUT_PATH
        and not parsed.query
        and not parsed.fragment
        and parsed.geturl() == value
    )
