import asyncio

import httpx
import pytest

from bot import subscription
from bot.subscription import SubscriptionError, fetch_subscription

DNS = {"sub.example.com": ["93.184.216.34"], "evil.example.com": ["169.254.169.254"],
       "rebind.example.com": ["93.184.216.35", "10.0.0.1"]}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    async def resolve(host, port):
        if host not in DNS:
            raise OSError("nxdomain")
        return DNS[host]
    monkeypatch.setattr(subscription, "_resolve", resolve)


def run(url, handler):
    return asyncio.run(fetch_subscription(url, transport=httpx.MockTransport(handler)))


def test_fetch_pins_ip_and_keeps_host():
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers["Host"], req.extensions.get("sni_hostname")))
        return httpx.Response(200, text="trojan://pw@hk.com:443#HK")

    proxies = run("https://sub.example.com/api?token=1", handler)
    assert proxies[0]["name"] == "HK"
    assert seen == [("https://93.184.216.34/api?token=1", "sub.example.com", "sub.example.com")]


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/sub", "http://[::1]/sub", "http://[::ffff:127.0.0.1]/", "http://10.1.2.3/",
    "http://evil.example.com/", "http://rebind.example.com/", "http://100.64.0.1/", "file:///etc/passwd",
])
def test_rejects_internal_targets(url):
    with pytest.raises(SubscriptionError):
        run(url, lambda req: httpx.Response(200, text=""))


def test_rejects_redirect_to_internal():
    def handler(req):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    with pytest.raises(SubscriptionError, match="内网"):
        run("https://sub.example.com/", handler)


def test_follows_public_relative_redirect():
    def handler(req):
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text="trojan://pw@jp.com:443#JP")

    assert run("https://sub.example.com/old", handler)[0]["name"] == "JP"


def test_too_many_redirects():
    with pytest.raises(SubscriptionError, match="重定向"):
        run("https://sub.example.com/", lambda req: httpx.Response(302, headers={"location": "/loop"}))


def test_behind_proxy_validates_but_does_not_pin(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, text="trojan://pw@hk.com:443#HK")

    run("https://sub.example.com/api", handler)
    assert seen == ["https://sub.example.com/api"]
    with pytest.raises(SubscriptionError, match="内网"):
        run("https://evil.example.com/", handler)
