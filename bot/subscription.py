"""把订阅链接 / 节点分享链接 / Clash 配置解析成 API 需要的 Node 列表。

API 的 Node.Payload 是单个节点的 Clash YAML，所以这里统一先转成 Clash 的 proxy dict。
"""
import base64
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import yaml

# SpeedCentre+ 支持的 Clash 节点类型
SUPPORTED_TYPES = {
    "ss", "ssr", "snell", "socks5", "http", "vmess", "trojan", "vless",
    "hysteria", "hysteria2", "tuic", "wireguard", "anytls", "mieru",
}

NODE_SCHEMES = ("ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2", "hy2", "tuic", "socks", "socks5", "anytls")
_NODE_RE = re.compile(r"(?:%s)://[^\s]+" % "|".join(NODE_SCHEMES), re.IGNORECASE)
_SUB_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


class SubscriptionError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def b64decode(s: str) -> str:
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s).decode("utf-8", errors="ignore")


def _q(query: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}


def _truthy(v: str | None) -> bool:
    return (v or "").lower() in ("1", "true", "yes")


def _host_port(netloc: str) -> tuple[str, int]:
    host, _, port = netloc.rpartition(":")
    host = host.strip("[]")
    return host, int(port)


def _apply_transport(proxy: dict, q: dict[str, str], default_sni: str) -> None:
    """vmess / vless / trojan 共用的传输层与 TLS 参数。"""
    net = (q.get("type") or q.get("net") or "tcp").lower()
    host = q.get("host", "")
    path = unquote(q.get("path", "")) or "/"
    if net == "ws":
        proxy["network"] = "ws"
        opts: dict = {"path": path}
        if host:
            opts["headers"] = {"Host": host}
        proxy["ws-opts"] = opts
    elif net == "grpc":
        proxy["network"] = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": unquote(q.get("serviceName", q.get("path", "")))}
    elif net in ("h2", "http"):
        proxy["network"] = "h2" if net == "h2" else "http"
        if net == "h2":
            proxy["h2-opts"] = {"path": path, **({"host": [host]} if host else {})}
        else:
            proxy["http-opts"] = {"path": [path], **({"headers": {"Host": [host]}} if host else {})}
    elif net in ("httpupgrade",):
        proxy["network"] = "ws"
        proxy["ws-opts"] = {"path": path, "v2ray-http-upgrade": True, **({"headers": {"Host": host}} if host else {})}

    security = (q.get("security") or q.get("tls") or "").lower()
    sni = q.get("sni") or q.get("peer") or host or default_sni
    if security in ("tls", "reality"):
        proxy["tls"] = True
        proxy["servername"] = sni
        if q.get("fp"):
            proxy["client-fingerprint"] = q["fp"]
        if q.get("alpn"):
            proxy["alpn"] = unquote(q["alpn"]).split(",")
    if security == "reality":
        proxy["reality-opts"] = {"public-key": q.get("pbk", ""), "short-id": q.get("sid", "")}
    if _truthy(q.get("allowInsecure")) or _truthy(q.get("insecure")):
        proxy["skip-cert-verify"] = True


# ---------------------------------------------------------------- per-scheme parsers

def _parse_ss(uri: str) -> dict:
    body = uri[len("ss://"):]
    body, _, name = body.partition("#")
    body, _, query = body.partition("?")
    body = body.rstrip("/")
    if "@" in body:
        userinfo, _, server = body.rpartition("@")
        userinfo = unquote(userinfo)
        if ":" not in userinfo:
            userinfo = b64decode(userinfo)
    else:
        decoded = b64decode(body)
        userinfo, _, server = decoded.rpartition("@")
    method, _, password = userinfo.partition(":")
    host, port = _host_port(server)
    proxy = {"name": unquote(name) or f"{host}:{port}", "type": "ss", "server": host, "port": port,
             "cipher": method, "password": password, "udp": True}
    q = _q(query)
    plugin = unquote(q.get("plugin", ""))
    if plugin:
        parts = plugin.split(";")
        pname, opts = parts[0], dict(p.split("=", 1) if "=" in p else (p, "true") for p in parts[1:])
        if pname in ("obfs-local", "simple-obfs", "obfs"):
            proxy["plugin"] = "obfs"
            proxy["plugin-opts"] = {"mode": opts.get("obfs", "http"), "host": opts.get("obfs-host", "")}
        elif pname == "v2ray-plugin":
            proxy["plugin"] = "v2ray-plugin"
            proxy["plugin-opts"] = {"mode": opts.get("mode", "websocket"), "tls": "tls" in opts,
                                    "host": opts.get("host", ""), "path": opts.get("path", "/")}
        else:
            proxy["plugin"] = pname
            proxy["plugin-opts"] = opts
    return proxy


def _parse_ssr(uri: str) -> dict:
    decoded = b64decode(uri[len("ssr://"):])
    main, _, query = decoded.partition("/?")
    host_port, protocol, method, obfs, pwd_b64 = main.rsplit(":", 4)
    host, port = _host_port(host_port)
    q = _q(query)

    def d(key: str) -> str:
        return b64decode(q[key]) if q.get(key) else ""

    return {"name": d("remarks") or f"{host}:{port}", "type": "ssr", "server": host, "port": port,
            "cipher": method, "password": b64decode(pwd_b64), "protocol": protocol,
            "protocol-param": d("protoparam"), "obfs": obfs, "obfs-param": d("obfsparam"), "udp": True}


def _parse_vmess(uri: str) -> dict:
    j = json.loads(b64decode(uri[len("vmess://"):]))
    host, port = j.get("add", ""), int(j.get("port", 0))
    proxy = {"name": j.get("ps") or f"{host}:{port}", "type": "vmess", "server": host, "port": port,
             "uuid": j.get("id", ""), "alterId": int(j.get("aid", 0) or 0),
             "cipher": j.get("scy") or "auto", "udp": True}
    q = {"type": j.get("net", "tcp"), "host": j.get("host", ""), "path": j.get("path", ""),
         "security": j.get("tls", ""), "sni": j.get("sni", ""), "fp": j.get("fp", ""), "alpn": j.get("alpn", "")}
    if q["type"] == "grpc":
        q["serviceName"] = j.get("path", "")
    _apply_transport(proxy, q, host)
    return proxy


def _parse_standard(uri: str) -> tuple:
    u = urlsplit(uri)
    host, port = _host_port(u.netloc.rpartition("@")[2])
    user = unquote(u.netloc.rpartition("@")[0]) if "@" in u.netloc else ""
    return u, host, port, user, _q(u.query), unquote(u.fragment) or f"{host}:{port}"


def _parse_trojan(uri: str) -> dict:
    u, host, port, user, q, name = _parse_standard(uri)
    proxy = {"name": name, "type": "trojan", "server": host, "port": port, "password": user, "udp": True,
             "sni": q.get("sni") or q.get("peer") or host}
    q.setdefault("security", "tls")
    _apply_transport(proxy, q, host)
    # trojan 在 clash 中使用 sni 字段而非 servername
    proxy.pop("tls", None)
    proxy["sni"] = proxy.pop("servername", proxy["sni"])
    return proxy


def _parse_vless(uri: str) -> dict:
    u, host, port, user, q, name = _parse_standard(uri)
    proxy = {"name": name, "type": "vless", "server": host, "port": port, "uuid": user, "udp": True}
    if q.get("flow"):
        proxy["flow"] = q["flow"]
    _apply_transport(proxy, q, host)
    return proxy


def _parse_hy2(uri: str) -> dict:
    u, host, port, user, q, name = _parse_standard(uri)
    proxy = {"name": name, "type": "hysteria2", "server": host, "port": port,
             "password": user or q.get("auth", ""), "sni": q.get("sni") or host}
    if q.get("obfs"):
        proxy["obfs"] = q["obfs"]
        proxy["obfs-password"] = q.get("obfs-password", "")
    if _truthy(q.get("insecure")):
        proxy["skip-cert-verify"] = True
    if q.get("mport"):
        proxy["ports"] = q["mport"]
    return proxy


def _parse_hysteria(uri: str) -> dict:
    u, host, port, user, q, name = _parse_standard(uri)
    proxy = {"name": name, "type": "hysteria", "server": host, "port": port,
             "auth-str": q.get("auth", ""), "sni": q.get("peer") or q.get("sni") or host,
             "up": q.get("upmbps", "10"), "down": q.get("downmbps", "50"),
             "protocol": q.get("protocol", "udp")}
    if q.get("alpn"):
        proxy["alpn"] = q["alpn"].split(",")
    if q.get("obfsParam") or q.get("obfs"):
        proxy["obfs"] = q.get("obfsParam") or q.get("obfs")
    if _truthy(q.get("insecure")):
        proxy["skip-cert-verify"] = True
    return proxy


def _parse_tuic(uri: str) -> dict:
    u, host, port, user, q, name = _parse_standard(uri)
    uuid, _, password = user.partition(":")
    proxy = {"name": name, "type": "tuic", "server": host, "port": port, "uuid": uuid, "password": password,
             "sni": q.get("sni") or host, "congestion-controller": q.get("congestion_control", "bbr"),
             "udp-relay-mode": q.get("udp_relay_mode", "native")}
    if q.get("alpn"):
        proxy["alpn"] = unquote(q["alpn"]).split(",")
    if _truthy(q.get("allow_insecure")) or _truthy(q.get("insecure")):
        proxy["skip-cert-verify"] = True
    return proxy


def _parse_socks(uri: str) -> dict:
    u, host, port, user, q, name = _parse_standard(uri)
    proxy = {"name": name, "type": "socks5", "server": host, "port": port, "udp": True}
    if user:
        if ":" not in user:
            try:
                user = b64decode(user)
            except Exception:
                pass
        username, _, password = user.partition(":")
        proxy["username"], proxy["password"] = username, password
    return proxy


def _parse_anytls(uri: str) -> dict:
    u, host, port, user, q, name = _parse_standard(uri)
    proxy = {"name": name, "type": "anytls", "server": host, "port": port, "password": user,
             "sni": q.get("sni") or host, "udp": True}
    if _truthy(q.get("insecure")):
        proxy["skip-cert-verify"] = True
    return proxy


_PARSERS = {
    "ss": _parse_ss, "ssr": _parse_ssr, "vmess": _parse_vmess, "vless": _parse_vless,
    "trojan": _parse_trojan, "hysteria2": _parse_hy2, "hy2": _parse_hy2, "hysteria": _parse_hysteria,
    "tuic": _parse_tuic, "socks": _parse_socks, "socks5": _parse_socks, "anytls": _parse_anytls,
}


def parse_uri(uri: str) -> dict | None:
    scheme = uri.split("://", 1)[0].lower()
    parser = _PARSERS.get(scheme)
    if not parser:
        return None
    try:
        return parser(uri.strip())
    except Exception:
        return None


# ---------------------------------------------------------------- content / subscription

def parse_content(text: str) -> list[dict]:
    """解析订阅内容：Clash YAML、base64 的分享链接列表、或明文分享链接列表。"""
    text = text.strip().lstrip("﻿")
    if not text:
        return []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("proxies"), list):
        return [p for p in data["proxies"] if isinstance(p, dict)]
    if isinstance(data, list) and data and all(isinstance(p, dict) for p in data):
        return data

    if not _NODE_RE.search(text):
        try:
            text = b64decode("".join(text.split()))
        except Exception:
            return []
    proxies = []
    for uri in _NODE_RE.findall(text):
        p = parse_uri(uri)
        if p:
            proxies.append(p)
    return proxies


async def fetch_subscription(url: str, timeout: float = 30.0) -> list[dict]:
    headers = {"User-Agent": "clash.meta"}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise SubscriptionError(f"获取订阅失败：{type(e).__name__}") from e
    if resp.status_code != 200:
        raise SubscriptionError(f"获取订阅失败：HTTP {resp.status_code}")
    return parse_content(resp.text)


def extract_sources(text: str) -> tuple[list[str], list[str]]:
    """从消息文本中提取订阅链接与节点分享链接。"""
    nodes = _NODE_RE.findall(text or "")
    subs = [u for u in _SUB_RE.findall(text or "") if u not in nodes]
    return subs, nodes


def to_api_nodes(proxies: list[dict], name_filter: str | None = None, limit: int | None = None) -> tuple[list[dict], int]:
    """过滤并转换成 API 的 Node 列表。返回 (nodes, 被跳过的数量)。"""
    pattern = re.compile(name_filter, re.IGNORECASE) if name_filter else None
    seen: set[str] = set()
    nodes, skipped = [], 0
    for p in proxies:
        ptype = str(p.get("type", "")).lower()
        name = str(p.get("name") or f"{p.get('server')}:{p.get('port')}")
        if ptype not in SUPPORTED_TYPES or not p.get("server"):
            skipped += 1
            continue
        if pattern and not pattern.search(name):
            continue
        # 节点重名会让结果难以区分，追加序号
        base, i = name, 2
        while name in seen:
            name, i = f"{base} ({i})", i + 1
        seen.add(name)
        p = {**p, "name": name}
        nodes.append({"Name": name, "Payload": yaml.safe_dump(p, allow_unicode=True, sort_keys=False)})
    if limit is not None and len(nodes) > limit:
        skipped += len(nodes) - limit
        nodes = nodes[:limit]
    return nodes, skipped
