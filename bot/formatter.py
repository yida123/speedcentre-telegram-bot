"""测试预设与结果格式化。"""
import html
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    command: str
    title: str
    matrices: tuple[str, ...]
    view: str = "normalview"
    sort: str | None = None


PRESETS: dict[str, Preset] = {
    "test": Preset("test", "全面测试", (
        "TEST_PING_RTT", "TEST_PING_CONN", "SPEED_AVERAGE", "SPEED_MAX", "SPEED_PER_SECOND", "UDP_TYPE",
    ), sort="avg_speed_desc"),
    "speed": Preset("speed", "测速", (
        "TEST_PING_RTT", "SPEED_AVERAGE", "SPEED_MAX", "SPEED_PER_SECOND",
    ), sort="avg_speed_desc"),
    "ping": Preset("ping", "延迟测试", (
        "TEST_PING_RTT", "TEST_PING_CONN", "TEST_HTTP_CODE", "TEST_PING_PACKET_LOSS",
    ), sort="rtt_asc"),
    "udp": Preset("udp", "UDP 类型测试", ("TEST_PING_RTT", "UDP_TYPE"), sort="rtt_asc"),
    "topo": Preset("topo", "拓扑分析", ("GEOIP_INBOUND", "GEOIP_OUTBOUND"), view="topologyview"),
}

STATUS_TEXT = {
    "pending": "⏳ 排队中",
    "running": "🏃 测试中",
    "completed": "✅ 已完成",
    "failed": "❌ 失败",
    "canceled": "🚫 已取消",
    "unspecified": "❔ 未知",
}


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def fmt_speed(bps) -> str:
    if not bps:
        return "-"
    bps = float(bps)
    for unit in ("B", "KB", "MB", "GB"):
        if bps < 1024:
            return f"{bps:.1f}{unit}/s" if unit != "B" else f"{bps:.0f}B/s"
        bps /= 1024
    return f"{bps:.1f}TB/s"


def fmt_ms(v) -> str:
    return f"{int(v)}ms" if v else "-"


def progress_bar(done: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    filled = round(width * done / total)
    return "█" * filled + "░" * (width - filled)


def _payload(m: dict) -> dict:
    p = m.get("Payload")
    if isinstance(p, dict):
        return p
    try:
        return json.loads(p or "{}")
    except (TypeError, ValueError):
        return {}


def summarize_entry(entry: dict) -> dict:
    """把单节点结果中的矩阵展开成 {Type: payload}。"""
    out = {}
    for m in entry.get("Matrices") or []:
        out[m.get("Type")] = _payload(m)
    return out


def _geo(stack: dict) -> str:
    main = (stack or {}).get("MainStack") or {}
    if not main:
        return "-"
    cc = main.get("country_code") or ""
    org = main.get("asn_organization") or main.get("organization") or ""
    asn = f"AS{main['asn']}" if main.get("asn") else ""
    return " ".join(x for x in (cc, asn, org[:20]) if x) or "-"


def format_result_text(entries: list[dict], limit: int = 40) -> str:
    """生成文本版结果（图片导出不可用时的兜底）。"""
    if not entries:
        return "没有可显示的结果。"
    lines = []
    for e in entries[:limit]:
        name = (e.get("ProxyInfo") or {}).get("Name") or "?"
        m = summarize_entry(e)
        parts = []
        if "TEST_PING_RTT" in m:
            parts.append(f"RTT {fmt_ms(m['TEST_PING_RTT'].get('Value'))}")
        if "TEST_PING_CONN" in m:
            parts.append(f"HTTPS {fmt_ms(m['TEST_PING_CONN'].get('Value'))}")
        if "TEST_PING_PACKET_LOSS" in m:
            parts.append(f"丢包 {m['TEST_PING_PACKET_LOSS'].get('Value', 0):.1f}%")
        if "SPEED_AVERAGE" in m:
            parts.append(f"均速 {fmt_speed(m['SPEED_AVERAGE'].get('Value'))}")
        if "SPEED_MAX" in m:
            parts.append(f"峰值 {fmt_speed(m['SPEED_MAX'].get('Value'))}")
        if "UDP_TYPE" in m:
            parts.append(f"UDP {m['UDP_TYPE'].get('Value') or '-'}")
        if "GEOIP_INBOUND" in m:
            parts.append(f"入口 {_geo(m['GEOIP_INBOUND'])}")
        if "GEOIP_OUTBOUND" in m:
            parts.append(f"出口 {_geo(m['GEOIP_OUTBOUND'])}")
        lines.append(f"<b>{esc(name[:40])}</b>\n  " + " | ".join(esc(p) for p in parts))
    if len(entries) > limit:
        lines.append(f"… 还有 {len(entries) - limit} 个节点未显示")
    return "\n".join(lines)


def format_stats(entries: list[dict]) -> str:
    """一行统计：可用节点数、最快节点等。"""
    total = len(entries)
    alive, best_name, best_speed = 0, None, 0
    for e in entries:
        m = summarize_entry(e)
        rtt = (m.get("TEST_PING_RTT") or {}).get("Value") or (m.get("TEST_PING_CONN") or {}).get("Value")
        if rtt:
            alive += 1
        speed = (m.get("SPEED_AVERAGE") or {}).get("Value") or 0
        if speed > best_speed:
            best_speed, best_name = speed, (e.get("ProxyInfo") or {}).get("Name")
    s = f"可用 {alive}/{total}"
    if best_name:
        s += f"，最快：{esc(best_name)}（{fmt_speed(best_speed)}）"
    return s
