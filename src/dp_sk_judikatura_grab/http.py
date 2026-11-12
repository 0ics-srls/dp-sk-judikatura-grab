"""HTTP client factory: browser User-Agent, no redirect following, host allow-list enforced before every request, TLS from certifi plus pinned intermediates."""
from __future__ import annotations

import ssl
from pathlib import Path

import certifi
import httpx

from dp_sk_judikatura_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError

# Intermediates a source fails to send are pinned as certs/*.pem (certs/README.md has URL and SHA-256, P79b-2). www.nsud.sk sends its chain: none today.
CERTS_DIR = Path(__file__).resolve().parent / "certs"

# the Supreme Court OpenData host; host.docker.internal serves a replay server of recorded answers (Source setting baseUrl)
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("www.nsud.sk", "host.docker.internal")
TIMEOUT_SEC = 60.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.5"}


def ssl_context(*, verify_tls: bool, certs_dir: Path = CERTS_DIR) -> ssl.SSLContext:
    """certifi roots plus every pinned ``*.pem`` of ``certs_dir``; ``verify_tls=False`` gives a context that does not verify (replay servers only)."""
    context = ssl.create_default_context(cafile=certifi.where())
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    for pinned in sorted(certs_dir.glob("*.pem")):
        context.load_verify_locations(cafile=str(pinned))
    return context


def build_client(
    allowed_hosts: tuple[str, ...] | list[str],
    transport: httpx.BaseTransport | None = None,
    *,
    verify_tls: bool = True,
) -> httpx.Client:
    """A client that refuses (config_error) any request whose host is not in ``allowed_hosts``; redirects are never followed."""
    allowed = frozenset(host.lower() for host in allowed_hosts)

    def check_host(request: httpx.Request) -> None:
        host = request.url.host.lower()
        if host not in allowed:
            msg = f"host {host} is not in the allowed hosts {sorted(allowed)}"
            raise GrabError(CONFIG_ERROR, msg)

    return httpx.Client(
        transport=transport,
        follow_redirects=False,
        timeout=TIMEOUT_SEC,
        verify=ssl_context(verify_tls=verify_tls),
        headers=DEFAULT_HEADERS,
        event_hooks={"request": [check_host]},
    )


def status_error(url: str, status: int) -> GrabError:
    """5xx and 429 are worth retrying; any other failing status means the request itself is wrong or the resource is gone."""
    code = TRANSIENT_ERROR if status >= 500 or status == 429 else CONFIG_ERROR  # noqa: PLR2004
    return GrabError(code, f"HTTP {status} from {url}")
