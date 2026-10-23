import re
from pathlib import Path
from urllib.parse import urlsplit

SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,160}\.service\Z")
DOCKER_ID = re.compile(r"[a-f0-9]{64}\Z")
CRITICAL = re.compile(r"(?:pi[-_ ]?app[-_ ]?hub|tailscale|docker|containerd|ssh|systemd|network|netplan|wifi|wpa|dhcp|dhcpcd|connman|firewall|nftables|iptables|ufw|resolved|dbus|cloudflare|wireguard|openvpn|nm-|modem|avahi|polkit)", re.I)

def protected(*values):
    return any(CRITICAL.search(str(v)) for v in values if v)

def service_name(value):
    if not SERVICE.fullmatch(value):
        raise ValueError("Invalid service name")
    return value

def docker_id(value):
    if not DOCKER_ID.fullmatch(value):
        raise ValueError("Invalid Docker ID")
    return value

def safe_path(value):
    if value is None or value == "":
        return None
    if len(value) > 1024 or not value.startswith("/") or "\x00" in value or ".." in Path(value).parts:
        raise ValueError("Paths must be absolute and cannot contain traversal")
    resolved = Path(value).resolve()
    if str(resolved) in {"/", "/home", "/etc", "/var", "/usr", "/opt", "/root"}:
        raise ValueError("A broad filesystem root cannot be an application path")
    return str(resolved)

def safe_url(value):
    if not value:
        return None
    url = urlsplit(value)
    if len(value) > 2048 or url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or any(ord(c) < 32 for c in value):
        raise ValueError("Use an HTTP(S) URL without credentials")
    # Validate malformed/out-of-range ports too.
    _ = url.port
    return value

def redact(text):
    text = str(text)
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", text, flags=re.S)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r'''(?ix)((?:[\w-]*(?:password|passwd|secret|token|api[_-]?key|credential|authorization)[\w-]*)["']?\s*(?:=|:|\s)\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''', r"\1[REDACTED]", text)
    # Environment assignments are not useful in process/log views and may contain secrets under arbitrary names.
    return re.sub(r"\b[A-Z][A-Z0-9_]{2,}=(?:\"[^\"]*\"|'[^']*'|[^\s]+)", "[ENV REDACTED]", text)
