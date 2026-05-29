"""Inject network latency between the app and postgres via Toxiproxy.

Toxiproxy sits in front of postgres as a TCP proxy; a ``latency`` toxic adds a
fixed delay (plus optional jitter) to every response. Pointing the benchmark
app at the proxy port makes each query pay a realistic round-trip cost, which
is what lets the async parallel prefetch win over the sequential one: N
independent queries overlap their latency instead of paying it N times.

Start the proxy once (host networking so it can reach the postgres container
and bind host ports):

    docker run -d --name django-asyncio-toxi --network host \\
        ghcr.io/shopify/toxiproxy:latest

``ensure()`` will start it for you if it isn't already running.
"""

import subprocess
import time

import httpx

API = "http://127.0.0.1:8474"
PROXY_NAME = "pg"
PROXY_LISTEN_PORT = 55433
CONTAINER = "django-asyncio-toxi"


def _api_up():
    try:
        return httpx.get(f"{API}/version", timeout=1.0).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def ensure():
    """Make sure the Toxiproxy server is reachable, starting it if needed."""
    if _api_up():
        return
    # Try to (re)start the container.
    subprocess.run(["docker", "start", CONTAINER], capture_output=True)
    if not _api_up():
        subprocess.run(
            [
                "docker", "run", "-d", "--name", CONTAINER, "--network", "host",
                "ghcr.io/shopify/toxiproxy:latest",
            ],
            capture_output=True,
        )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _api_up():
            return
        time.sleep(0.3)
    raise RuntimeError("Toxiproxy API did not come up on " + API)


def configure(latency_ms, jitter_ms=0, upstream_host="127.0.0.1", upstream_port=55432):
    """(Re)create the postgres proxy with a fixed-latency toxic on responses.

    Returns the port the app should connect to.
    """
    ensure()
    httpx.delete(f"{API}/proxies/{PROXY_NAME}")
    httpx.post(
        f"{API}/proxies",
        json={
            "name": PROXY_NAME,
            "listen": f"127.0.0.1:{PROXY_LISTEN_PORT}",
            "upstream": f"{upstream_host}:{upstream_port}",
            "enabled": True,
        },
    ).raise_for_status()
    if latency_ms > 0:
        httpx.post(
            f"{API}/proxies/{PROXY_NAME}/toxics",
            json={
                "name": "lat",
                "type": "latency",
                "stream": "downstream",
                "attributes": {"latency": int(latency_ms), "jitter": int(jitter_ms)},
            },
        ).raise_for_status()
    return PROXY_LISTEN_PORT
