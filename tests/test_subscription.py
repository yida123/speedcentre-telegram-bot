import base64
import json

import yaml

from bot.formatter import format_result_text, format_stats
from bot.subscription import contains_sensitive_link, extract_sources, parse_content, parse_uri, to_api_nodes


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def test_ss_sip002():
    p = parse_uri("ss://" + b64("aes-256-gcm:pass") + "@1.2.3.4:8388#%E9%A6%99%E6%B8%AF")
    assert p == {"name": "香港", "type": "ss", "server": "1.2.3.4", "port": 8388,
                 "cipher": "aes-256-gcm", "password": "pass", "udp": True}


def test_ss_legacy():
    p = parse_uri("ss://" + b64("chacha20-ietf-poly1305:pw@example.com:443") + "#a")
    assert (p["server"], p["port"], p["cipher"], p["password"]) == ("example.com", 443, "chacha20-ietf-poly1305", "pw")


def test_ssr():
    raw = "1.1.1.1:443:auth_aes128_md5:aes-256-cfb:tls1.2_ticket_auth:" + b64("pwd") + "/?remarks=" + b64("SSR节点")
    p = parse_uri("ssr://" + b64(raw))
    assert p["server"] == "1.1.1.1" and p["port"] == 443 and p["password"] == "pwd" and p["name"] == "SSR节点"


def test_vmess_ws_tls():
    j = {"v": "2", "ps": "vm", "add": "a.com", "port": "443", "id": "uuid", "aid": "0",
         "net": "ws", "host": "h.com", "path": "/ws", "tls": "tls"}
    p = parse_uri("vmess://" + b64(json.dumps(j)))
    assert p["network"] == "ws" and p["ws-opts"] == {"path": "/ws", "headers": {"Host": "h.com"}}
    assert p["tls"] is True and p["servername"] == "h.com"


def test_vless_reality():
    p = parse_uri("vless://uuid@1.2.3.4:443?security=reality&pbk=KEY&sid=ab&sni=www.apple.com&fp=chrome"
                  "&flow=xtls-rprx-vision&type=tcp#R")
    assert p["reality-opts"] == {"public-key": "KEY", "short-id": "ab"}
    assert p["servername"] == "www.apple.com" and p["flow"] == "xtls-rprx-vision"


def test_trojan_and_hy2():
    t = parse_uri("trojan://pw@t.com:443?sni=s.com&allowInsecure=1#T")
    assert t["sni"] == "s.com" and t["skip-cert-verify"] and "tls" not in t
    h = parse_uri("hy2://pw@h.com:8443?sni=x.com&obfs=salamander&obfs-password=o#H")
    assert h["type"] == "hysteria2" and h["obfs-password"] == "o"


def test_parse_clash_yaml():
    content = yaml.safe_dump({"proxies": [
        {"name": "a", "type": "ss", "server": "1.1.1.1", "port": 1, "cipher": "aes-128-gcm", "password": "x"},
        {"name": "a", "type": "trojan", "server": "2.2.2.2", "port": 2, "password": "y"},
        {"name": "bad", "type": "unknown", "server": "3.3.3.3", "port": 3},
    ]})
    proxies = parse_content(content)
    nodes, skipped = to_api_nodes(proxies)
    assert [n["Name"] for n in nodes] == ["a", "a (2)"] and skipped == 1
    assert yaml.safe_load(nodes[1]["Payload"])["name"] == "a (2)"


def test_parse_base64_list_and_filter():
    lines = "\n".join([
        "trojan://pw@hk.com:443#香港01",
        "trojan://pw@jp.com:443#日本01",
    ])
    nodes, _ = to_api_nodes(parse_content(b64(lines)), name_filter="香港|HK")
    assert [n["Name"] for n in nodes] == ["香港01"]


def test_extract_sources():
    subs, nodes = extract_sources("https://sub.example.com/a?token=1 vless://u@h:1#x")
    assert subs == ["https://sub.example.com/a?token=1"] and nodes == ["vless://u@h:1#x"]


def test_format_result():
    entries = [{"ProxyInfo": {"Name": "n1"}, "Matrices": [
        {"Type": "TEST_PING_RTT", "Payload": '{"Value": 120}'},
        {"Type": "SPEED_AVERAGE", "Payload": '{"Value": 10485760}'},
        {"Type": "UDP_TYPE", "Payload": '{"Value": "Full Cone"}'},
    ]}]
    text = format_result_text(entries)
    assert "120ms" in text and "10.0MB/s" in text and "Full Cone" in text
    assert "可用 1/1" in format_stats(entries)


def test_contains_sensitive_link():
    assert contains_sensitive_link("vmess://abc")
    assert contains_sensitive_link("https://a.com/api/v1/client/subscribe?token=x")
    assert contains_sensitive_link("https://a.com/sub?target=clash")
    assert contains_sensitive_link("https://a.com/link/AbC123?clash=1")
    assert not contains_sensitive_link("https://github.com/yida123/speed_bot")
    assert not contains_sensitive_link("今天测速怎么样")
