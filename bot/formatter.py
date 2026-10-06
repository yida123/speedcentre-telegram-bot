"""测试预设与结果格式化。"""
import html
import json
from dataclasses import dataclass


# 可勾选的测试项：key -> (按钮文字, 对应的矩阵类型)
TEST_OPTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "rtt": ("延迟 RTT", ("TEST_PING_RTT",)),
    "conn": ("HTTPS 延迟", ("TEST_PING_CONN",)),
    "loss": ("丢包率", ("TEST_PING_PACKET_LOSS",)),
    "http": ("HTTP 状态码", ("TEST_HTTP_CODE",)),
    "speed": ("测速", ("SPEED_AVERAGE", "SPEED_MAX", "SPEED_PER_SECOND")),
    "udp": ("UDP 类型", ("UDP_TYPE",)),
    "geo": ("出入口拓扑", ("GEOIP_INBOUND", "GEOIP_OUTBOUND")),
    "hijack": ("劫持检测", ("TEST_HIJACK_DETECTION",)),
}


@dataclass(frozen=True)
class Preset:
    command: str
    title: str
    options: tuple[str, ...]


PRESETS: dict[str, Preset] = {
    "test": Preset("test", "全面测试", ("rtt", "conn", "speed", "udp")),
    "speed": Preset("speed", "测速", ("rtt", "speed")),
    "ping": Preset("ping", "延迟测试", ("rtt", "conn", "http", "loss")),
    "udp": Preset("udp", "UDP 类型测试", ("rtt", "udp")),
    "topo": Preset("topo", "拓扑分析", ("geo",)),
}


@dataclass(frozen=True)
class TestPlan:
    """一次要提交的测试：矩阵列表、要导出的图片视图和排序方式。"""
    title: str
    matrices: tuple[dict, ...]
    views: tuple[str, ...]
    sort: str | None


def build_plan(title: str, options: set[str] | tuple[str, ...], script_ids: tuple[str, ...] = ()) -> TestPlan:
    """根据勾选的测试项与流媒体脚本生成测试计划。"""
    options = set(options)
    matrices = [
        {"Type": t, "Params": ""}
        for key in TEST_OPTIONS if key in options
        for t in TEST_OPTIONS[key][1]
    ]
    matrices += [{"Type": "TEST_SCRIPT", "Params": f"INTERNAL::{sid}"} for sid in script_ids]

    views: list[str] = []
    if options - {"geo"} or script_ids:
        views.append("normalview")
    if "geo" in options:
        views.append("topologyview")
    sort = "avg_speed_desc" if "speed" in options else "rtt_asc" if "rtt" in options else None
    return TestPlan(title, tuple(matrices), tuple(views), sort)


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
        if "TEST_HIJACK_DETECTION" in m:
            h = m["TEST_HIJACK_DETECTION"]
            parts.append("劫持 " + ("-" if not h.get("RealIP") else "无" if h.get("SpeedIP") == h.get("RealIP") else "疑似"))
        for mx in e.get("Matrices") or []:
            if mx.get("Type") == "TEST_SCRIPT":
                p = _payload(mx)
                parts.append(f"{str(p.get('Key') or '脚本').removeprefix('INTERNAL::')} {p.get('Text') or '-'}")
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


def node_speed(entry: dict) -> float:
    """单个节点的平均速度（字节/秒），没有测出速度时为 0。"""
    value = (summarize_entry(entry).get("SPEED_AVERAGE") or {}).get("Value")
    return float(value) if isinstance(value, (int, float)) else 0.0


def fmt_duration(seconds: float) -> str:
    """把秒数写成“1 小时 5 分钟”“45 秒”这样的形式。"""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{days} 天" if days else "", f"{hours} 小时" if hours else "", f"{minutes} 分钟" if minutes else ""]
    return " ".join(p for p in parts if p) or "0 秒"
