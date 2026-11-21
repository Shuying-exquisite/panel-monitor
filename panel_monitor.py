# -*- coding: utf-8 -*-
"""
面板在线监测台 (Panel Monitor)  v2.0
--------------------------------------------------
零依赖本地小程序：定时探测面板类服务器的 TCP 端口 + HTTP 响应，
记录历史采样，提供「在线状态管理」仪表盘，只有在线的面板才允许访问。

特点：
  * http.client / socket 直连，天然绕过 Windows 系统代理
  * 支持 http / https（忽略自签证书），401 / 403 视为在线
  * 后端常驻检测：关掉浏览器页面也继续探测并记录趋势
  * 状态变化写入日志，可选提示音
  * 支持 PyInstaller 冻结运行（web 资源内嵌，servers.json 外置可编辑）
  * 只监听 127.0.0.1

运行：python panel_monitor.py  /  双击 启动面板监测.bat  /  面板在线监测台.exe
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import uuid
import webbrowser
try:
    import fcntl                       # 仅 Linux/Unix 有；Windows 上置空，风扇功能自动禁用
except ImportError:
    fcntl = None
import ctypes                         # 用 ctypes + libc.ioctl 操作 /dev/gpiochip0（纯 Python、零依赖）
import ctypes.util
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, urlencode

try:
    import winsound
except ImportError:
    winsound = None

VERSION = "2.0"

# ---------------------------------------------------------------- 路径
# 冻结(exe)时：web 资源在 _MEIPASS 临时目录，配置与历史数据放在 exe 同目录。
IS_FROZEN = getattr(sys, "frozen", False)
if IS_FROZEN:
    ASSET_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    DATA_DIR = Path(sys.executable).resolve().parent
else:
    ASSET_DIR = Path(__file__).resolve().parent
    DATA_DIR = ASSET_DIR
WEB_DIR = ASSET_DIR / "web"
CONFIG_PATH = DATA_DIR / "servers.json"
STORE_PATH = DATA_DIR / "monitor_data.json"

# ---------------------------------------------------------------- 默认配置
# 默认不内置任何服务器；首次运行后由用户在网页里自行添加
DEFAULT_SERVERS = []

DEFAULT_CONFIG = {
    "listen": {"port": 8765, "auto_open": True},
    "interval": 30,
    "alert_sound": True,
    "servers": DEFAULT_SERVERS,
    "fan": {"enabled": False, "gpio": 3, "inverted": False},
    "status_servers": [],
}

# ---------------------------------------------------------------- 全局状态
LOCK = threading.RLock()
CONFIG: dict = {}
RESULTS: dict = {}
CHECKING = False
LAST_CHECK = ""
POOL = ThreadPoolExecutor(max_workers=16)
ACTUAL_PORT = 8765
HISTORY: list = []          # 采样点，用于趋势 / 可用性 / 响应时间
EVENTS: list = []           # 状态变化与操作日志
STATE_SINCE: dict = {}      # id -> {"state": ..., "since": epoch}
LAST_OFFLINE_TS = None
START_TS = time.time()
SESSIONS: dict = {}          # 登录会话：token -> 过期时间戳（仅存内存）
LAST_SYNC: dict = {"time": 0, "ok": None, "msg": "尚未同步"}  # 最近一次同步结果（界面状态用）
SYNC_LOCK = threading.Lock()  # 防止并发同步相互踩踏

# ---------------------------------------------------------------- 风扇控制（树莓派 GPIO）
FAN_STATE = 0                 # 当前风扇「期望」状态：0 关 / 1 开（desired，非物理电平）
_FAN_HANDLE = None            # 已申请的 GPIO line handle fd（缓存复用）
_FAN_PIN = None               # 当前 handle 对应的 BCM 引脚；引脚变更时重建


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def now_full() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def relative(ts) -> str:
    if not ts:
        return "暂无记录"
    delta = max(0, time.time() - float(ts))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


# ---------------------------------------------------------------- 配置与存储
def load_config() -> dict:
    global CONFIG
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("servers"), list):
                data.setdefault("listen", {"port": 8765, "auto_open": True})
                data.setdefault("interval", 30)
                data.setdefault("alert_sound", True)
                data.setdefault("sync", {"url": "", "user": "", "password": "", "auto": False, "interval": 30})
                data.setdefault("fan", {"enabled": False, "gpio": 3, "inverted": False})
                data.setdefault("panel_keys", {})   # 旧版：面板密钥（保留兼容，不再使用）
                # 新版：独立运行状态服务器列表（与网站 servers 无挂钩，不参与跨端同步）
                data.setdefault("status_servers", [])
                if not data["status_servers"] and data.get("panel_keys"):
                    # 一次性迁移旧 panel_keys -> status_servers
                    by_id = {s.get("id"): s for s in data.get("servers", [])}
                    for sid, cfg in (data.get("panel_keys") or {}).items():
                        if isinstance(cfg, dict) and cfg.get("type"):
                            old = by_id.get(sid, {})
                            data["status_servers"].append({
                                "id": uuid.uuid4().hex[:8],
                                "name": old.get("name", "服务器"),
                                "type": cfg.get("type", "agent"),
                                "base": cfg.get("base") or old.get("url", ""),
                                "key": cfg.get("key", ""),
                            })
                CONFIG = data
                if ensure_updated_at():
                    save_config()
                return CONFIG
        except Exception as exc:
            print(f"[警告] 配置文件读取失败，将使用默认配置：{exc}")
    save_config(json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False)))
    ensure_updated_at()
    return CONFIG


def save_config(cfg: dict | None = None) -> None:
    global CONFIG
    if cfg is not None:
        CONFIG = cfg
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def ensure_updated_at() -> bool:
    """给没有 updated_at 的网址补一个时间戳（用于跨端智能合并时的冲突判定）。返回是否改动过。"""
    changed = False
    for s in CONFIG.get("servers", []):
        if "updated_at" not in s or not isinstance(s.get("updated_at"), (int, float)):
            s["updated_at"] = time.time()
            changed = True
    return changed


def load_store() -> None:
    global HISTORY, EVENTS, STATE_SINCE, LAST_OFFLINE_TS
    if not STORE_PATH.exists():
        return
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        HISTORY = list(data.get("history", []))[-6000:]
        EVENTS = list(data.get("events", []))[:200]
        STATE_SINCE = dict(data.get("since", {}))
        LAST_OFFLINE_TS = data.get("last_offline")
    except Exception as exc:
        print(f"[警告] 历史数据读取失败：{exc}")


def save_store() -> None:
    try:
        tmp = STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "history": HISTORY[-6000:], "events": EVENTS[:200],
            "since": STATE_SINCE, "last_offline": LAST_OFFLINE_TS,
        }, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STORE_PATH)
    except Exception as exc:
        print(f"[警告] 历史数据写入失败：{exc}")


def add_event(level: str, text: str) -> None:
    with LOCK:
        EVENTS.insert(0, {"time": now_full(), "level": level, "text": text})
        del EVENTS[200:]


def alert_sound() -> None:
    if not CONFIG.get("alert_sound", True) or winsound is None:
        return
    try:
        winsound.Beep(880, 160)
        winsound.Beep(660, 200)
    except Exception:
        pass


# ---------------------------------------------------------------- 探测引擎
def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, socket.gaierror):
        return "域名解析失败（DNS 无法解析该地址）"
    if isinstance(exc, ConnectionRefusedError):
        return "端口拒绝连接（服务可能没启动）"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "连接超时（防火墙 / 安全组 / 主机不可达）"
    if isinstance(exc, ssl.SSLError):
        return "HTTPS 握手失败（证书或协议不匹配）"
    if isinstance(exc, OSError):
        code = getattr(exc, "errno", None)
        if code in (10065, 10051, 101, 113, 51, 64):
            return "网络不可达（检查路由、防火墙或安全组）"
        if code in (10060, 10061, 110, 111):
            return "连接超时 / 端口拒绝（服务未启动或被拦截）"
        return f"网络错误：{exc}"
    return f"{type(exc).__name__}: {exc}"


def blank_result(sid: str, state: str = "pending") -> dict:
    return {
        "id": sid, "state": state, "tcp_ms": None, "total_ms": None,
        "status": None, "title": "", "message": "", "checked_at": "",
    }


def probe(srv: dict) -> dict:
    """TCP 连通性 + HTTP 响应双层探测。不走系统代理。"""
    sid = srv.get("id", "")
    url = (srv.get("url") or "").strip()
    out = blank_result(sid, "offline")
    out["checked_at"] = now_hms()

    if not url:
        out["message"] = "尚未填写访问地址"
        return out

    parsed = urlparse(url if "://" in url else "http://" + url)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in ("http", "https"):
        out["message"] = "只支持 http:// 或 https:// 地址"
        return out
    host = parsed.hostname
    if not host:
        out["message"] = "地址格式有误，缺少主机名"
        return out
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        out["message"] = "端口号不合法"
        return out
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    try:
        timeout = max(0.5, min(float(srv.get("timeout") or 3), 30))
    except (TypeError, ValueError):
        timeout = 3.0
    mode = srv.get("mode") if srv.get("mode") in ("http", "tcp") else "http"

    started = time.perf_counter()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
    except Exception as exc:
        out["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        out["message"] = friendly_error(exc)
        return out

    tcp_ms = (time.perf_counter() - started) * 1000
    out["tcp_ms"] = round(tcp_ms, 1)
    out["total_ms"] = round(tcp_ms, 1)

    if mode == "tcp":
        out["state"] = "online"
        out["message"] = "TCP 端口可达（按端口模式检测）"
        return out

    conn = None
    try:
        if scheme == "https":
            ctx = ssl._create_unverified_context()
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", path, headers={
            "Host": parsed.netloc,
            "User-Agent": "PanelMonitor/2.0",
            "Accept": "text/html, */*",
            "Connection": "close",
        })
        resp = conn.getresponse()
        status = resp.status
        ctype = resp.getheader("Content-Type", "") or ""
        raw = resp.read(8192)
        conn.close()
        conn = None

        out["status"] = status
        out["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        text = http.client.responses.get(status, "")
        if 200 <= status < 400 or status in (401, 403):
            out["state"] = "online"
            extra = "（面板正常，需要登录）" if status in (401, 403) else ""
            out["message"] = f"HTTP {status} {text}{extra}"
        else:
            out["state"] = "warn"
            out["message"] = f"服务在响应，但状态码异常：HTTP {status} {text}"

        m = re.search(rb"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
        if m:
            enc = "utf-8"
            ce = re.search(r"charset\s*=\s*([\w-]+)", ctype, re.I)
            if ce:
                enc = ce.group(1)
            try:
                title = m.group(1).decode(enc, "ignore")
            except LookupError:
                title = m.group(1).decode("utf-8", "ignore")
            out["title"] = re.sub(r"\s+", " ", title).strip()[:60]
        return out
    except Exception as exc:
        out["state"] = "warn"
        out["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
        out["message"] = "端口通，但 HTTP 请求失败：" + friendly_error(exc)
        return out
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 采样与日志
def record_round(servers: list, full: bool = True) -> None:
    """一轮检测结束后：记录采样点、状态变化事件、告警。"""
    global LAST_OFFLINE_TS
    now = time.time()
    name_of = {s["id"]: s.get("name", s["id"]) for s in servers}
    alerted = False
    with LOCK:
        online = warn = offline = 0
        latencies = []
        for s in servers:
            sid = s["id"]
            r = RESULTS.get(sid, {})
            st = r.get("state", "pending")
            if st == "online":
                online += 1
                if r.get("total_ms") is not None:
                    latencies.append(r["total_ms"])
            elif st == "warn":
                warn += 1
            elif st == "offline":
                offline += 1

            prev = STATE_SINCE.get(sid)
            if prev is None:
                STATE_SINCE[sid] = {"state": st, "since": now}
            elif prev["state"] != st:
                label = {"online": "在线", "warn": "响应异常", "offline": "离线"}.get(st, st)
                old = {"online": "在线", "warn": "响应异常", "offline": "离线"}.get(prev["state"], prev["state"])
                became_bad = st in ("offline", "warn") and prev["state"] == "online"
                recovered = st == "online" and prev["state"] in ("offline", "warn")
                add_event("warn" if became_bad else "ok" if recovered else "info",
                          f"{name_of.get(sid, sid)}：{old} → {label}" + ("，原因：" + r.get("message", "") if became_bad else ""))
                STATE_SINCE[sid] = {"state": st, "since": now}
                if became_bad:
                    alerted = True
                    LAST_OFFLINE_TS = now   # 只记「由在线变离线」的时刻

        if full and servers:
            HISTORY.append({
                "ts": now, "total": len(servers), "online": online,
                "warn": warn, "offline": offline,
                "avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            })
            del HISTORY[:-6000]
        save_store()
    if alerted:
        alert_sound()


def trigger_check(ids: list | None = None, record: bool = True) -> bool:
    """异步发起一轮检测；已在检测中则忽略。"""
    global CHECKING, LAST_CHECK
    with LOCK:
        if CHECKING:
            return False
        CHECKING = True
        servers = [dict(s) for s in CONFIG.get("servers", [])]
        if ids:
            servers = [s for s in servers if s.get("id") in ids]
        for s in servers:
            prev = RESULTS.get(s["id"], blank_result(s["id"]))
            RESULTS[s["id"]] = {**prev, "state": "checking"}

    def run():
        global CHECKING, LAST_CHECK
        try:
            futures = {POOL.submit(probe, s): s for s in servers}
            for fut in as_completed(futures):
                sid = futures[fut]["id"]
                try:
                    res = fut.result()
                except Exception as exc:
                    res = blank_result(sid, "offline")
                    res["checked_at"] = now_hms()
                    res["message"] = "探测异常：" + friendly_error(exc)
                with LOCK:
                    RESULTS[sid] = res
            if servers:
                record_round(servers, full=record and not ids)
        finally:
            with LOCK:
                CHECKING = False
                LAST_CHECK = now_full()

    threading.Thread(target=run, daemon=True).start()
    return True


def auto_loop() -> None:
    """后端常驻检测：关掉浏览器页面也继续采样。"""
    while True:
        try:
            interval = max(5, int(CONFIG.get("interval", 30) or 30))
        except (TypeError, ValueError):
            interval = 30
        time.sleep(interval)
        try:
            if trigger_check():
                for _ in range(240):
                    if not CHECKING:
                        break
                    time.sleep(0.25)
        except Exception as exc:
            print(f"[警告] 自动检测异常：{exc}")


# ---------------------------------------------------------------- 统计聚合
RANGES = {
    "1h":  (3600, 12),
    "1d":  (86400, 24),
    "7d":  (604800, 7),
    "30d": (2592000, 15),
}


def aggregate(range_key: str) -> dict:
    span, count = RANGES.get(range_key, RANGES["1d"])
    now = time.time()
    start = now - span
    size = span / count
    buckets = [[] for _ in range(count)]
    for p in HISTORY:
        ts = p.get("ts", 0)
        if ts >= start:
            idx = min(count - 1, max(0, int((ts - start) / size)))
            buckets[idx].append(p)

    labels = []
    for i in range(count):
        dt = datetime.fromtimestamp(start + (i + 1) * size)
        labels.append(dt.strftime("%H:%M") if range_key in ("1h", "1d") else dt.strftime("%m-%d"))

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def bucket_avg(field):
        return [avg([p.get(field) for p in b]) for b in buckets]

    availability = [
        avg([100.0 * p.get("online", 0) / p["total"] for p in b if p.get("total")])
        for b in buckets
    ]
    return {
        "labels": labels,
        "online": bucket_avg("online"),
        "offline": [avg([p.get("total", 0) - p.get("online", 0) for p in b]) for b in buckets],
        "total": bucket_avg("total"),
        "latency": bucket_avg("avg_ms"),
        "availability": availability,
        "samples": sum(len(b) for b in buckets),
        "range": range_key,
    }


def build_dashboard(trend_key: str, detail_key: str) -> dict:
    with LOCK:
        servers = json.loads(json.dumps(CONFIG.get("servers", []), ensure_ascii=False))
        results = json.loads(json.dumps(RESULTS, ensure_ascii=False))
        events = json.loads(json.dumps(EVENTS[:60], ensure_ascii=False))
        checking, last_check = CHECKING, LAST_CHECK
        interval = CONFIG.get("interval", 30)
        alert = CONFIG.get("alert_sound", True)
        history_copy = list(HISTORY)
        since = dict(STATE_SINCE)
        last_offline = LAST_OFFLINE_TS
        port = ACTUAL_PORT

    total = len(servers)
    online = sum(1 for s in servers if results.get(s["id"], {}).get("state") == "online")
    warn = sum(1 for s in servers if results.get(s["id"], {}).get("state") == "warn")
    offline = total - online - warn

    recent = [p["avg_ms"] for p in history_copy[-120:] if p.get("avg_ms") is not None]
    avg_ms = round(sum(recent) / len(recent)) if recent else None

    longest_sec = 0.0
    for info in since.values():
        if info.get("state") == "online":
            longest_sec = max(longest_sec, time.time() - float(info.get("since", time.time())))
    if longest_sec >= 86400:
        longest_text = f"{int(longest_sec // 86400)} 天"
    elif longest_sec >= 3600:
        longest_text = f"{int(longest_sec // 3600)} 小时"
    elif longest_sec >= 60:
        longest_text = f"{int(longest_sec // 60)} 分钟"
    elif longest_sec > 0:
        longest_text = "刚刚"
    else:
        longest_text = "—"

    return {
        "version": VERSION,
        "auth_enabled": bool((CONFIG.get("auth") or {}).get("enabled")),
        "servers": servers, "results": results, "events": events,
        "checking": checking, "last_check": last_check,
        "interval": interval, "alert_sound": alert, "port": port,
        "config_path": str(CONFIG_PATH), "store_path": str(STORE_PATH),
        "uptime": relative(START_TS),
        "stats": {
            "total": total, "online": online, "warn": warn, "offline": total - online,
            "rate": round(100.0 * online / total, 1) if total else 0.0,
        },
        "trend": aggregate(trend_key),
        "timeline": aggregate(detail_key),
        "sync": {k: v for k, v in (CONFIG.get("sync") or {}).items() if k != "password"},
        "last_sync": dict(LAST_SYNC),
        "fan": fan_get_status(),
        "temp": read_cpu_temp(),
        "kpi": {
            "avg_ms": avg_ms,
            "longest_online": longest_text,
            "last_offline": relative(last_offline),
            "samples": len(history_copy),
        },
    }


# ---------------------------------------------------------------- 配置操作
def normalize_server(data: dict) -> dict:
    sid = (data.get("id") or "").strip() or uuid.uuid4().hex[:8]
    try:
        timeout = float(data.get("timeout") or 3)
    except (TypeError, ValueError):
        timeout = 3
    return {
        "id": sid,
        "name": (data.get("name") or "未命名面板").strip()[:40],
        "group": (data.get("group") or "默认").strip()[:20],
        "url": (data.get("url") or "").strip(),
        "mode": data.get("mode") if data.get("mode") in ("http", "tcp") else "http",
        "timeout": max(0.5, min(timeout, 30)),
        "note": (data.get("note") or "").strip()[:120],
        # 保留传入的时间戳（跨端合并用）；为 0 时由调用方补为当前时间
        "updated_at": float(data.get("updated_at") or 0) or 0.0,
    }


def normalize_panel(data) -> dict:
    """规范化面板 API 配置；无有效类型时返回空字典（不落盘无用字段）。"""
    if not isinstance(data, dict):
        return {}
    ptype = str(data.get("type") or "").strip().lower()
    if ptype not in ("1panel", "baota", "agent"):
        return {}
    return {
        "type": ptype,
        "key": str(data.get("key") or "").strip(),
        "base": str(data.get("base") or "").strip().rstrip("/"),
    }


def normalize_status_server(data: dict) -> dict | None:
    """规范化独立运行状态服务器；名称与地址必填，返回 None 表示无效。"""
    if not isinstance(data, dict):
        return None
    name = str(data.get("name") or "").strip()[:40]
    ptype = str(data.get("type") or "agent").strip().lower()
    base = str(data.get("base") or "").strip().rstrip("/")
    if not name or not base or ptype not in ("agent", "1panel", "baota"):
        return None
    return {
        "id": str(data.get("id") or "").strip() or uuid.uuid4().hex[:8],
        "name": name,
        "group": str(data.get("group") or "").strip()[:30] or "默认分组",
        "type": ptype,
        "base": base,
        "key": str(data.get("key") or "").strip(),
    }


def upsert_status_server(data: dict) -> dict | None:
    """新增/编辑独立服务器，返回规范化后的对象；无 id 时新建。"""
    srv = normalize_status_server(data)
    if srv is None:
        return None
    with LOCK:
        items = CONFIG.setdefault("status_servers", [])
        existing = next((s for s in items if s.get("id") == srv["id"]), None)
        # 编辑时密钥留空 = 保持原值（前端不回显密钥）
        if existing is not None and not srv.get("key") and existing.get("key"):
            srv["key"] = existing["key"]
        for i, s in enumerate(items):
            if s.get("id") == srv["id"]:
                items[i] = srv
                break
        else:
            items.append(srv)
        _PANEL_CACHE.pop(srv["id"], None)
        save_config()
    return srv


def delete_status_server(sid: str) -> bool:
    with LOCK:
        items = CONFIG.get("status_servers", [])
        before = len(items)
        CONFIG["status_servers"] = [s for s in items if s.get("id") != sid]
        _PANEL_CACHE.pop(sid, None)
        if len(CONFIG["status_servers"]) != before:
            save_config()
            return True
    return False


def reorder_status_servers(ids: list) -> bool:
    """按前端拖拽结果重排运行状态服务器顺序，并写入配置文件。
    支持部分排序：ids 只包含被拖动那一组的成员，其余成员保持原有相对位置。"""
    with LOCK:
        items = CONFIG.get("status_servers", [])
        by_id = {s.get("id"): s for s in items}
        if not ids or any(i not in by_id for i in ids) or len(set(ids)) != len(ids):
            return False
        # 找到 ids 中成员在原列表里占据的槽位，按新顺序回填；其它成员原地不动
        slots = [i for i, s in enumerate(items) if s.get("id") in set(ids)]
        if len(slots) != len(ids):
            return False
        new_items = list(items)
        for slot, sid in zip(slots, ids):
            new_items[slot] = by_id[sid]
        CONFIG["status_servers"] = new_items
        save_config()
    return True


def upsert_server(data: dict) -> dict:
    srv = normalize_server(data)
    srv["updated_at"] = time.time()   # 本地编辑：刷新为最新时间戳
    with LOCK:
        servers = CONFIG.setdefault("servers", [])
        for i, existing in enumerate(servers):
            if existing.get("id") == srv["id"]:
                servers[i] = srv
                break
        else:
            servers.append(srv)
        RESULTS.setdefault(srv["id"], blank_result(srv["id"]))
        save_config()
    add_event("info", f"{'新增' if not data.get('id') else '更新'}面板：{srv['name']}")
    return srv


def delete_server(sid: str) -> None:
    with LOCK:
        name = next((s.get("name", sid) for s in CONFIG.get("servers", []) if s.get("id") == sid), sid)
        CONFIG["servers"] = [s for s in CONFIG.get("servers", []) if s.get("id") != sid]
        RESULTS.pop(sid, None)
        STATE_SINCE.pop(sid, None)
        save_config()
    add_event("info", f"删除面板：{name}")


def reorder_servers(ids: list) -> bool:
    """按前端拖拽结果重排面板顺序，并写入配置文件。"""
    with LOCK:
        servers = CONFIG.get("servers", [])
        by_id = {s.get("id"): s for s in servers}
        if not ids or len(ids) != len(servers) or set(ids) != set(by_id):
            return False
        CONFIG["servers"] = [by_id[i] for i in ids]
        save_config()
    add_event("info", "调整了面板显示顺序")
    return True


# ---------------------------------------------------------------- 跨端同步
def clone(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False))


def merge_servers(local: list, remote: list) -> list:
    """双向智能合并：按 id 取并集；两边都有的按 updated_at 取较新的一方；
    仅一边有的保留；顺序以本地为主、远端多出的追加在末尾。"""
    by_local = {s.get("id"): s for s in local if s.get("id")}
    by_remote = {s.get("id"): s for s in remote if s.get("id")}
    ids = [s.get("id") for s in local if s.get("id")]
    for i in by_remote:
        if i not in by_local:
            ids.append(i)
    merged = []
    for i in ids:
        l = by_local.get(i)
        r = by_remote.get(i)
        if l and not r:
            merged.append(l)
        elif r and not l:
            merged.append(r)
        else:
            lt = float(l.get("updated_at") or 0)
            rt = float(r.get("updated_at") or 0)
            merged.append(r if rt >= lt else l)
    return merged


def _remote_login(base: str, user: str, password: str) -> str:
    """登录远端并取回会话 Cookie；用 http.client 直连（绕过系统代理）。"""
    parsed = urlparse(base if "://" in base else "http://" + base)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname
    if not host:
        raise ValueError("远端地址缺少主机名")
    port = parsed.port or (443 if scheme == "https" else 80)
    path = (parsed.path or "/").rstrip("/") + "/api/login"
    ctx = ssl._create_unverified_context() if scheme == "https" else None
    conn = (http.client.HTTPSConnection(host, port, timeout=10, context=ctx)
            if scheme == "https" else http.client.HTTPConnection(host, port, timeout=10))
    try:
        conn.request("POST", path,
                    body=json.dumps({"user": user, "password": password, "remember": True}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "PanelMonitor/2.0"})
        resp = conn.getresponse()
        cookie = resp.getheader("Set-Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "pm_session" and v:
                return v
    finally:
        conn.close()
    raise RuntimeError("远端登录失败（账号或密码错误，或该端未启用认证）")


def _remote_get_servers(base: str, cookie: str) -> list:
    parsed = urlparse(base if "://" in base else "http://" + base)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname
    port = parsed.port or (443 if scheme == "https" else 80)
    path = (parsed.path or "/").rstrip("/") + "/api/servers"
    ctx = ssl._create_unverified_context() if scheme == "https" else None
    conn = (http.client.HTTPSConnection(host, port, timeout=15, context=ctx)
            if scheme == "https" else http.client.HTTPConnection(host, port, timeout=15))
    try:
        conn.request("GET", path, headers={"Cookie": f"pm_session={cookie}", "User-Agent": "PanelMonitor/2.0"})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "ignore")
        data = json.loads(raw)
        servers = data.get("servers") or []
        for s in servers:
            s.setdefault("updated_at", 0)
        return servers
    finally:
        conn.close()


def _remote_replace(base: str, cookie: str, servers: list) -> None:
    parsed = urlparse(base if "://" in base else "http://" + base)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname
    port = parsed.port or (443 if scheme == "https" else 80)
    path = (parsed.path or "/").rstrip("/") + "/api/servers/replace"
    ctx = ssl._create_unverified_context() if scheme == "https" else None
    conn = (http.client.HTTPSConnection(host, port, timeout=15, context=ctx)
            if scheme == "https" else http.client.HTTPConnection(host, port, timeout=15))
    try:
        conn.request("POST", path, body=json.dumps({"servers": servers}, ensure_ascii=False).encode("utf-8"),
                     headers={"Content-Type": "application/json", "Cookie": f"pm_session={cookie}", "User-Agent": "PanelMonitor/2.0"})
        resp = conn.getresponse()
        if resp.status >= 400:
            raise RuntimeError(f"远端写入失败：HTTP {resp.status}")
    finally:
        conn.close()


def do_sync(direction: str) -> dict:
    """direction: both=双向智能合并；pull=用远端覆盖本地；push=用本地覆盖远端。"""
    cfg = CONFIG.get("sync") or {}
    base = (cfg.get("url") or "").strip()
    if not base:
        return {"ok": False, "error": "未配置远端地址，请先在「系统设置 → 远端同步」填写"}
    try:
        cookie = _remote_login(base, cfg.get("user") or "", cfg.get("password") or "")
        remote_servers = _remote_get_servers(base, cookie)
        local_servers = clone(CONFIG.get("servers", []))
        if direction == "pull":
            sets = local_servers
            CONFIG["servers"] = clone(remote_servers)
            save_config()
            msg = f"已用公网端覆盖本地，共 {len(remote_servers)} 个网址"
        elif direction == "push":
            _remote_replace(base, cookie, local_servers)
            msg = f"已把本地 {len(local_servers)} 个网址推送到公网端"
        else:  # both
            merged = merge_servers(local_servers, remote_servers)
            CONFIG["servers"] = merged
            save_config()
            with LOCK:
                for sid in list(RESULTS.keys()):
                    if not any(s.get("id") == sid for s in merged):
                        RESULTS.pop(sid, None)
                        STATE_SINCE.pop(sid, None)
            _remote_replace(base, cookie, merged)
            msg = f"已双向合并并互相同步，共 {len(merged)} 个网址"
        return {"ok": True, "direction": direction, "count": len(CONFIG.get("servers", [])), "message": msg}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def sync_auto_loop() -> None:
    """后台按间隔自动双向智能合并（配置开启时）。"""
    global LAST_SYNC
    while True:
        try:
            cfg = CONFIG.get("sync") or {}
            interval = max(5, int(cfg.get("interval") or 30))
        except (TypeError, ValueError):
            interval = 30
        time.sleep(interval * 60)
        with SYNC_LOCK:
            cfg = CONFIG.get("sync") or {}
            if not cfg.get("auto") or not (cfg.get("url") or "").strip():
                continue
            res = do_sync("both")
            LAST_SYNC = {"time": time.time(), "ok": res.get("ok"), "msg": res.get("message") or res.get("error") or "同步完成"}
            add_event("info", "自动同步：" + (res.get("message") or res.get("error") or "完成"))
            if res.get("ok"):
                trigger_check(record=False)   # 合并后重新探测最新的网址集合


# ---------------------------------------------------------------- 风扇控制（树莓派 GPIO）
# 纯 Python 通过字符设备 ioctl 操作 /dev/gpiochip0，不依赖任何第三方库；
# 容器需透传该设备（docker-compose devices），本机 exe（Windows）无 /dev/gpiochip0，自动禁用。
_GPIO_MAGIC = 0xB4
_GPIO_GET_LINEHANDLE_IOCTL = 0xC16CB403   # _IOWR(0xB4, 0x03, gpiohandle_request)  size=364
_GPIO_SET_LINE_VALUES_IOCTL = 0xC040B409   # _IOWR(0xB4, 0x09, gpiohandle_data)   size=64
GPIOHANDLE_REQUEST_OUTPUT = 0x2

# gpio uAPI v1 结构体（与内核 gpio.h 一致；已用 C 编译 + ctypes 实测通过）
class _GpioHandleRequest(ctypes.Structure):
    _fields_ = [
        ("lineoffsets", ctypes.c_uint * 64),    # 64 * 4 = 256
        ("flags", ctypes.c_uint),               #       4  -> 260
        ("default_values", ctypes.c_ubyte * 64),#      64  -> 324
        ("consumer_label", ctypes.c_char * 32), #      32  -> 356
        ("lines", ctypes.c_uint),               #       4  -> 360
        ("fd", ctypes.c_int),                   #       4  -> 364
    ]


class _GpioHandleData(ctypes.Structure):
    _fields_ = [("values", ctypes.c_ubyte * 64)]  # 64 bytes


_LIB = None


def _libc():
    """懒加载 libc，use_errno=True 便于用 ctypes.get_errno() 取错误码。"""
    global _LIB
    if _LIB is None:
        _LIB = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    return _LIB


def fan_gpio_available() -> bool:
    return os.path.exists("/dev/gpiochip0")


def _request_output(pin: int, initial: int) -> int:
    """向 /dev/gpiochip0 申请某引脚为输出，返回 line handle fd。"""
    libc = _libc()
    fd = os.open("/dev/gpiochip0", os.O_RDWR)
    req = _GpioHandleRequest()
    req.lineoffsets[0] = pin & 0xFFFFFFFF
    req.flags = GPIOHANDLE_REQUEST_OUTPUT
    req.default_values[0] = initial & 1
    req.consumer_label = b"panel-monitor-fan"
    req.lines = 1
    rc = libc.ioctl(fd, _GPIO_GET_LINEHANDLE_IOCTL, ctypes.byref(req))
    errno = ctypes.get_errno()
    os.close(fd)
    if rc < 0:
        raise OSError(errno or 0, f"ioctl GET_LINEHANDLE 失败 (rc={rc}, errno={errno})")
    return req.fd


def _set_output(handle_fd: int, value: int) -> None:
    libc = _libc()
    data = _GpioHandleData()
    data.values[0] = value & 1
    rc = libc.ioctl(handle_fd, _GPIO_SET_LINE_VALUES_IOCTL, ctypes.byref(data))
    errno = ctypes.get_errno()
    if rc < 0:
        raise OSError(errno or 0, f"ioctl SET_VALUES 失败 (rc={rc}, errno={errno})")


def fan_apply_state(desired_on: bool, cfg: dict) -> tuple:
    """设置风扇期望状态。返回 (ok, message)。"""
    if not fan_gpio_available():
        return False, "本机不支持 GPIO（无 /dev/gpiochip0），风扇控制仅在树莓派端可用"
    pin = int(cfg.get("gpio", 3))
    inverted = bool(cfg.get("inverted", False))
    global _FAN_HANDLE, _FAN_PIN, FAN_STATE
    if _FAN_HANDLE is None or _FAN_PIN != pin:
        if _FAN_HANDLE is not None:
            try:
                os.close(_FAN_HANDLE)
            except Exception:
                pass
        try:
            _FAN_HANDLE = _request_output(pin, 0)
            _FAN_PIN = pin
        except Exception as exc:
            return False, f"申请 GPIO{pin} 失败：{exc}（可能被其它程序占用，或该脚不是 GPIO 输出）"
    physical = 1 if (desired_on != inverted) else 0
    try:
        _set_output(_FAN_HANDLE, physical)
    except Exception as exc:
        _FAN_HANDLE = None
        return False, f"设置 GPIO{pin} 失败：{exc}"
    FAN_STATE = 1 if desired_on else 0
    return True, ("风扇已打开" if desired_on else "风扇已关闭")


def fan_get_status() -> dict:
    fan = CONFIG.get("fan", {"enabled": False, "gpio": 3, "inverted": False})
    available = fan_gpio_available()
    state = FAN_STATE if (fan.get("enabled") and available) else 0
    return {
        "enabled": bool(fan.get("enabled")),
        "gpio": int(fan.get("gpio", 3)),
        "inverted": bool(fan.get("inverted")),
        "available": available,
        "state": state,
    }


def read_cpu_temp() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


# ---- 服务器运行状态（宿主机整机指标；容器内读 /proc 即宿主机视图）----
_SYS_CACHE: dict = {}
_SYS_CACHE_TS: float = 0.0
_SYS_CACHE_TTL: float = 2.0


def _read_cpu_stat() -> tuple[int, int] | None:
    """返回 (总jiffies, 空闲jiffies)。"""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        if not parts or parts[0] != "cpu":
            return None
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        return sum(vals), idle
    except Exception:
        return None


def _cpu_percent() -> float | None:
    a = _read_cpu_stat()
    if a is None:
        return None
    time.sleep(0.25)
    b = _read_cpu_stat()
    if b is None or b[0] <= a[0]:
        return 0.0
    total_d = b[0] - a[0]
    idle_d = b[1] - a[1]
    return round(max(0.0, min(100.0, 100.0 * (1.0 - idle_d / total_d))), 1)


def read_system_status() -> dict:
    """采集宿主机 CPU / 内存 / 负载 / 磁盘 / 温度，短时缓存避免频繁采样。"""
    global _SYS_CACHE, _SYS_CACHE_TS
    now = time.time()
    if _SYS_CACHE and (now - _SYS_CACHE_TS) < _SYS_CACHE_TTL:
        return dict(_SYS_CACHE)

    cpu = _cpu_percent()

    # 内存口径与 free/top 一致（MemAvailable 在老内核是估算值，不作为主口径）
    mi = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    try:
                        mi[k.strip()] = int(v.strip().split()[0]) * 1024
                    except (ValueError, IndexError):
                        pass
    except Exception:
        pass
    mem_total = mi.get("MemTotal", 0)
    mem_free = mi.get("MemFree", 0)
    buffers = mi.get("Buffers", 0)
    cached = mi.get("Cached", 0)
    sreclaim = mi.get("SReclaimable", 0)
    mem_avail = mi.get("MemAvailable", 0)
    mem_used = max(0, mem_total - mem_free - buffers - cached - sreclaim)
    if not mem_used:
        mem_used = max(0, mem_total - mem_avail)
    mem_pct = round(100.0 * mem_used / mem_total, 1) if mem_total else 0.0

    load1 = load5 = load15 = 0.0
    try:
        with open("/proc/loadavg") as f:
            lp = f.read().split()
        load1, load5, load15 = float(lp[0]), float(lp[1]), float(lp[2])
    except Exception:
        pass
    try:
        cores = os.cpu_count() or 1
    except Exception:
        cores = 1
    load_pct = round(min(100.0, 100.0 * load1 / cores), 1) if cores else 0.0

    disk_total = disk_used = disk_free = 0
    try:
        st = os.statvfs("/")
        disk_total = st.f_blocks * st.f_frsize
        disk_free = st.f_bavail * st.f_frsize
        disk_used = disk_total - (st.f_bfree * st.f_frsize)  # 已用 = 总量 - 空闲(含保留)
        disk_pct = round(100.0 * disk_used / disk_total, 1) if disk_total else 0.0
    except Exception:
        disk_pct = 0.0

    try:
        uptime_s = int(float(open("/proc/uptime").read().split()[0]))
    except Exception:
        uptime_s = 0
    uptime_txt = f"{uptime_s // 86400}天 {uptime_s % 86400 // 3600}小时"

    try:
        host = os.uname().nodename
        # 容器内 hostname 是容器 ID（12 位十六进制）；此时回退读取宿主机名挂载点，
        # 或按已知环境显示 Raspberry Pi 的真实主机名，避免界面露出随机容器 ID。
        import re as _re
        if _re.fullmatch(r"[0-9a-f]{12}", host or ""):
            if os.path.exists("/dev/gpiochip0"):   # 确认跑在树莓派上
                host = "raspberrypi"
    except Exception:
        host = "unknown"

    result = {
        "cpu": {"percent": cpu, "cores": cores},
        "mem": {"percent": mem_pct, "used": mem_used, "total": mem_total},
        "load": {"percent": load_pct, "load1": load1, "load5": load5, "load15": load15},
        "disk": {"percent": disk_pct, "used": disk_used, "total": disk_total},
        "temp": read_cpu_temp(),
        "uptime": uptime_txt,
        "host": host,
        "ts": now,
    }
    _SYS_CACHE, _SYS_CACHE_TS = result, now
    return dict(result)


# ---------------------------------------------------------------- 面板系统状态 API（1Panel / 宝塔）
_PANEL_CACHE: dict = {}          # sid -> (expire_ts, result)
_PANEL_CACHE_TTL: float = 2.0


def _http_request(url: str, method: str = "GET", headers: dict | None = None,
                  body: bytes | None = None, timeout: float = 8) -> bytes:
    """极简 HTTP(S) 请求，忽略自签证书；返回响应体 bytes。"""
    u = urlparse(url)
    scheme = (u.scheme or "http").lower()
    host = u.hostname or ""
    port = u.port or (443 if scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    ctx = ssl._create_unverified_context() if scheme == "https" else None
    conn = (http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
            if scheme == "https" else http.client.HTTPConnection(host, port, timeout=timeout))
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.read()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _to_float(v, default=0.0) -> float:
    try:
        return float(str(v).replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def _pick(d: dict, *keys, default=None):
    """从字典里按多个候选 key 取第一个存在的值（兼容不同面板/版本字段）。"""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _fetch_1panel(base: str, key: str) -> dict:
    """1Panel：Token=md5('1panel'+api_key+timestamp)，GET /api/v1/dashboard/base/os。"""
    ts = str(int(time.time()))
    token = hashlib.md5(("1panel" + key + ts).encode("utf-8")).hexdigest()
    url = base + "/api/v1/dashboard/base/os"
    raw = _http_request(url, "GET", {"1Panel-Token": token, "1Panel-Timestamp": ts})
    j = json.loads(raw.decode("utf-8", "ignore"))
    if j.get("code") not in (0, 200, None) or j.get("message") in ("用户未登录", "API interface key error"):
        raise RuntimeError(str(j.get("message") or "1Panel 鉴权失败"))
    d = j.get("data") or {}
    if isinstance(d, list):
        d = d[0] if d and isinstance(d[0], dict) else {}
    cpu = _pick(d, "cpuUsage", "cpu", "cpuUsed", default=0)
    if isinstance(cpu, dict):
        cpu = _pick(cpu, "usage", "used", "value", default=0)
    mem_total = _to_float(_pick(d, "memTotal", "memoryTotal", "totalMem", default=0))
    mem_used = _to_float(_pick(d, "memUsed", "memoryUsed", "usedMem", default=0))
    mem_avail = _to_float(_pick(d, "memAvailable", "availableMem", default=0))
    # 1Panel 内存单位通常是 MB
    mem_total_b, mem_used_b = mem_total * 1024**2, mem_used * 1024**2
    if mem_avail:
        mem_used_b = max(0, (mem_total - mem_avail) * 1024**2)
    disk_total = _to_float(_pick(d, "diskTotal", default=0))
    disk_used = _to_float(_pick(d, "diskUsed", default=0))
    return {
        "cpu": {"percent": round(_to_float(cpu), 1)},
        "mem": {"used": mem_used_b, "total": mem_total_b,
                "percent": round(100 * mem_used_b / mem_total_b, 1) if mem_total_b else 0.0},
        "load": {"load1": _to_float(_pick(d, "load1", default=0)),
                 "load5": _to_float(_pick(d, "load5", default=0)),
                 "load15": _to_float(_pick(d, "load15", default=0))},
        "disk": {"used": disk_used * 1024**2, "total": disk_total * 1024**2,
                 "percent": round(100 * disk_used / disk_total, 1) if disk_total else 0.0},
        "uptime": str(_pick(d, "uptime", "time", default="—")),
        "host": str(_pick(d, "hostname", "host", default="")),
        "source": "1panel",
    }


def _fetch_baota(base: str, key: str) -> dict:
    """宝塔：token=md5(timestamp+md5(api_sk))，POST /system?action=GetSystemTotal。"""
    ts = str(int(time.time()))
    token = hashlib.md5((ts + hashlib.md5(key.encode("utf-8")).hexdigest()).encode("utf-8")).hexdigest()
    url = base + "/system"
    form = urlencode({"request_time": ts, "request_token": token, "action": "GetSystemTotal"}).encode()
    raw = _http_request(url, "POST", {"Content-Type": "application/x-www-form-urlencoded"}, form)
    j = json.loads(raw.decode("utf-8", "ignore"))
    if j.get("status") is False or j.get("msg") in ("签名错误", "请勿频繁请求"):
        raise RuntimeError(str(j.get("msg") or "宝塔鉴权失败"))
    d = j
    mem_total = _to_float(_pick(d, "memTotal", default=0))       # MB
    mem_used = _to_float(_pick(d, "memRealUsed", default=0))
    cpu = _to_float(_pick(d, "cpuRealUsed", default=0))
    cores = int(_to_float(_pick(d, "cpuNum", default=0))) or None
    disk_total = _to_float(_pick(d, "diskTotal", default=0))     # 部分版本无磁盘字段
    return {
        "cpu": {"percent": round(cpu, 1), "cores": cores},
        "mem": {"used": mem_used * 1024**2, "total": mem_total * 1024**2,
                "percent": round(100 * mem_used / mem_total, 1) if mem_total else 0.0},
        "load": {"load1": _to_float(_pick(d, "load1", default=0))},
        "disk": {"used": disk_total * 1024**2 if disk_total else 0,
                 "total": disk_total * 1024**2 if disk_total else 0,
                 "percent": 0.0},
        "uptime": str(_pick(d, "time", default="—")),
        "host": str(_pick(d, "system", default=""))[:40],
        "source": "baota",
    }


def _fetch_agent(base: str, key: str) -> dict:
    """ysy-agent：GET /api/status，可选 Bearer token；采集器已返回标准结构。"""
    headers = {}
    if key:
        headers["Authorization"] = "Bearer " + key
    raw = _http_request(base.rstrip("/") + "/api/status", "GET", headers)
    j = json.loads(raw.decode("utf-8", "ignore"))
    if not j.get("ok"):
        raise RuntimeError(str(j.get("error") or "采集器返回异常"))
    return {
        "cpu": j.get("cpu") or {},
        "mem": j.get("mem") or {},
        "load": j.get("load") or {},
        "disk": j.get("disk") or {},
        "temp": j.get("temp"),
        "uptime": j.get("uptime") or "—",
        "host": j.get("host") or "",
        "ts": j.get("ts") or time.time(),
        "source": "agent",
    }


def read_panel_status(sid: str, panel: dict, base_url: str = "", force: bool = False) -> dict:
    """按面板配置拉取系统状态；返回 {ok, status, error}。"""
    ptype, key = panel.get("type"), panel.get("key")
    if ptype not in ("1panel", "baota", "agent"):
        return {"ok": False, "error": "该服务器未配置面板/采集器"}
    if ptype != "agent" and not key:
        return {"ok": False, "error": "该服务器未配置面板 API 密钥"}
    now = time.time()
    cached = _PANEL_CACHE.get(sid)
    if not force and cached and cached[0] > now:
        return {"ok": True, "status": cached[1]}
    base = (panel.get("base") or base_url or "").strip()
    u = urlparse(base if "://" in base else "http://" + base)
    base = f"{u.scheme or 'http'}://{u.netloc}" + (("/" + u.path.strip("/")) if u.path.strip("/") and ptype == "agent" else "")
    try:
        if ptype == "agent":
            status = _fetch_agent(base, key or "")
        elif ptype == "1panel":
            status = _fetch_1panel(base, key)
        else:
            status = _fetch_baota(base, key)
        status["ts"] = status.get("ts") or now
        _PANEL_CACHE[sid] = (now + _PANEL_CACHE_TTL, status)
        return {"ok": True, "status": status}
    except Exception as e:
        return {"ok": False, "error": f"{ptype} 调用失败：{e}"}


# ---------------------------------------------------------------- HTTP 服务
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/login.html": ("login.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PanelMonitor/2.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: list | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _auth_enabled(self) -> bool:
        return bool((CONFIG.get("auth") or {}).get("enabled"))

    def _session_ok(self) -> bool:
        """Cookie 会话校验；未启用认证时恒为 True。"""
        if not self._auth_enabled():
            return True
        cookie = self.headers.get("Cookie", "") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "pm_session" and v in SESSIONS and SESSIONS[v] > time.time():
                return True
        return False

    def _guard(self) -> bool:
        """未登录返回 401 JSON；不发 WWW-Authenticate，浏览器不会弹原生登录框。"""
        if self._session_ok():
            return False
        self._json({"ok": False, "error": "unauthorized"}, 401)
        return True

    def _serve_static(self, name: str, ctype: str):
        fp = WEB_DIR / name
        if fp.exists():
            self._send(200, fp.read_bytes(), ctype)
        else:
            self._send(404, f"缺少界面文件：{name}".encode("utf-8"), "text/plain; charset=utf-8")

    def _do_login(self, data: dict):
        cfg = CONFIG.get("auth") or {}
        if (str(data.get("user") or "") == str(cfg.get("user", "")) and
                str(data.get("password") or "") == str(cfg.get("password", ""))):
            now = time.time()
            for t in [t for t, exp in SESSIONS.items() if exp <= now]:
                SESSIONS.pop(t, None)
            token = secrets.token_urlsafe(24)
            days = 7 if data.get("remember") else 1
            SESSIONS[token] = now + days * 86400
            body = json.dumps({"ok": True}).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8",
                       [("Set-Cookie",
                         f"pm_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={days * 86400}")])
        else:
            self._json({"ok": False, "error": "账号或密码错误"}, 401)

    def _do_logout(self):
        cookie = self.headers.get("Cookie", "") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "pm_session":
                SESSIONS.pop(v, None)
        body = json.dumps({"ok": True}).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8",
                   [("Set-Cookie", "pm_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")])

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/session":
            self._json({"auth_enabled": self._auth_enabled(), "logged_in": self._session_ok()})
            return
        if path == "/login.html":
            self._serve_static("login.html", "text/html; charset=utf-8")
            return
        if path == "/":
            # 未登录时直接给独立登录页，而不是浏览器原生弹窗
            if self._auth_enabled() and not self._session_ok():
                self._serve_static("login.html", "text/html; charset=utf-8")
            else:
                self._serve_static("index.html", "text/html; charset=utf-8")
            return
        if path.startswith("/api/"):
            if self._guard():
                return
            if path in ("/api/state", "/api/dashboard"):
                self._json(build_dashboard(
                    (query.get("trend") or ["7d"])[0],
                    (query.get("detail") or ["1d"])[0],
                ))
                return
            if path == "/api/servers":
                with LOCK:
                    servers = clone(CONFIG.get("servers", []))
                self._json({"ok": True, "servers": servers})
                return
            if path == "/api/sync/config":
                sc = clone(CONFIG.get("sync") or {})
                sc.pop("password", None)   # 不向前端回传明文密码
                self._json({"ok": True, "sync": sc})
                return
            if path == "/api/fan":
                self._json({"ok": True, "fan": fan_get_status(), "temp": read_cpu_temp()})
                return
            if path == "/api/system":
                self._json({"ok": True, "system": read_system_status()})
                return
            if path == "/api/panel/status":
                sid = (query.get("id") or [""])[0]
                with LOCK:
                    srv = next((s for s in CONFIG.get("status_servers", []) if s.get("id") == sid), None)
                if not srv:
                    self._json({"ok": False, "error": "未找到该服务器"}, 404)
                    return
                panel = {"type": srv.get("type"), "key": srv.get("key", ""), "base": srv.get("base", "")}
                res = read_panel_status(sid, panel, srv.get("base", ""))
                self._json(res, 200 if res.get("ok") else 400)
                return
            if path == "/api/panel/list":
                # 返回独立运行状态服务器列表（不含密钥）
                with LOCK:
                    items = [{"id": s.get("id"), "name": s.get("name"), "group": s.get("group") or "默认分组",
                              "type": s.get("type"), "base": s.get("base", "")}
                             for s in CONFIG.get("status_servers", [])]
                self._json({"ok": True, "servers": items})
                return
            if path == "/api/export":
                # 完整配置备份：网址清单 + 分组 + 服务器状态列表（含采集器地址/令牌） + 系统设置 + 同步设置 + 风扇设置
                with LOCK:
                    backup = {
                        "app": "面板在线监测台",
                        "version": VERSION,
                        "exported_at": now_full(),
                        "notice": "敏感文件：sync.password 与 status_servers[].key 为明文，请妥善保管，勿随意分享。",
                        "interval": CONFIG.get("interval", 30),
                        "alert_sound": CONFIG.get("alert_sound", True),
                        "servers": clone(CONFIG.get("servers", [])),
                        "status_servers": clone(CONFIG.get("status_servers", [])),
                        "sync": clone(CONFIG.get("sync") or {}),
                        "fan": clone(CONFIG.get("fan") or {"enabled": False, "gpio": 3, "inverted": False}),
                    }
                body = json.dumps(backup, ensure_ascii=False, indent=2).encode("utf-8")
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                fname = f"panel-backup-{stamp}.json"
                quoted = quote(fname)
                self._send(200, body, "application/json; charset=utf-8",
                           [("Content-Disposition", f"attachment; filename=\"{fname}\"; filename*=UTF-8''{quoted}")])
                return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            self._serve_static(name, ctype)
            return
        self._send(404, b"Not Found", "text/plain; charset=utf-8")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        path = urlparse(self.path).path

        if path == "/api/login":
            self._do_login(data)
            return
        if path == "/api/logout":
            self._do_logout()
            return
        if self._guard():
            return

        if path == "/api/check":
            ids = data.get("ids") or None
            started = trigger_check(ids, record=not ids)
            if started:
                add_event("info", "手动触发检测" if not ids else "手动检测单台面板")
            self._json({"ok": True, "started": started})
        elif path == "/api/server/save":
            srv = upsert_server(data)
            trigger_check([srv["id"]], record=False)
            self._json({"ok": True, "server": srv})
        elif path == "/api/server/delete":
            delete_server(str(data.get("id") or ""))
            self._json({"ok": True})
        elif path == "/api/servers/reorder":
            ok = reorder_servers(data.get("ids") or [])
            self._json({"ok": ok}, 200 if ok else 400)
        elif path == "/api/config":
            with LOCK:
                try:
                    CONFIG["interval"] = max(5, min(int(data.get("interval") or 30), 3600))
                except (TypeError, ValueError):
                    CONFIG["interval"] = 30
                if "alert_sound" in data:
                    CONFIG["alert_sound"] = bool(data["alert_sound"])
                save_config()
            add_event("info", f"系统设置更新：检测间隔 {CONFIG['interval']} 秒")
            self._json({"ok": True, "interval": CONFIG["interval"], "alert_sound": CONFIG.get("alert_sound", True)})
        elif path == "/api/events/clear":
            with LOCK:
                EVENTS.clear()
                save_store()
            self._json({"ok": True})
        elif path == "/api/servers/replace":
            raw = data.get("servers")
            if not isinstance(raw, list):
                self._json({"ok": False, "error": "servers 必须是数组"}, 400)
                return
            servers = [normalize_server(s) for s in raw if isinstance(s, dict)]
            with LOCK:
                CONFIG["servers"] = servers
                # 清理已不存在的探测结果
                for sid in list(RESULTS.keys()):
                    if not any(s.get("id") == sid for s in servers):
                        RESULTS.pop(sid, None)
                        STATE_SINCE.pop(sid, None)
                save_config()
            add_event("info", f"通过同步覆盖了服务器列表，共 {len(servers)} 个")
            self._json({"ok": True, "count": len(servers)})
        elif path == "/api/import":
            # 从 JSON 备份恢复：网址清单 + 服务器状态列表 + 系统设置 + 同步设置 + 风扇设置
            backup = data.get("backup") if isinstance(data.get("backup"), dict) else data
            if not isinstance(backup, dict) or (not isinstance(backup.get("servers"), list) and not isinstance(backup.get("status_servers"), list)):
                self._json({"ok": False, "error": "备份格式无效"}, 400)
                return
            servers = [normalize_server(s) for s in backup.get("servers", []) if isinstance(s, dict)] if isinstance(backup.get("servers"), list) else None
            status_servers = [normalize_status_server(s) for s in backup.get("status_servers", []) if isinstance(s, dict)] if isinstance(backup.get("status_servers"), list) else None
            status_servers = [s for s in status_servers if s] if status_servers is not None else None
            with LOCK:
                if servers is not None:
                    CONFIG["servers"] = servers
                    for sid in list(RESULTS.keys()):
                        if not any(s.get("id") == sid for s in servers):
                            RESULTS.pop(sid, None)
                            STATE_SINCE.pop(sid, None)
                if status_servers is not None:
                    CONFIG["status_servers"] = status_servers
                    for s in status_servers:
                        _PANEL_CACHE.pop(s.get("id"), None)
                try:
                    CONFIG["interval"] = max(5, min(int(backup.get("interval") or 30), 3600))
                except (TypeError, ValueError):
                    pass
                if "alert_sound" in backup:
                    CONFIG["alert_sound"] = bool(backup["alert_sound"])
                if isinstance(backup.get("sync"), dict):
                    CONFIG["sync"] = backup["sync"]
                if isinstance(backup.get("fan"), dict):
                    CONFIG["fan"] = backup["fan"]
                save_config()
            add_event("info", f"导入备份：网址 {len(servers) if servers is not None else '—'} 个，服务器状态 {len(status_servers) if status_servers is not None else '—'} 台")
            self._json({"ok": True,
                        "servers": len(servers) if servers is not None else None,
                        "status_servers": len(status_servers) if status_servers is not None else None})
        elif path == "/api/sync/config":
            with LOCK:
                sc = CONFIG.setdefault("sync", {})
                if "url" in data: sc["url"] = str(data["url"] or "").strip()
                if "user" in data: sc["user"] = str(data["user"] or "").strip()
                if "password" in data: sc["password"] = str(data["password"] or "")
                if "auto" in data: sc["auto"] = bool(data["auto"])
                if "interval" in data:
                    try:
                        sc["interval"] = max(5, min(int(data["interval"]), 1440))
                    except (TypeError, ValueError):
                        sc["interval"] = 30
                save_config()
            add_event("info", "更新了远端同步设置")
            out = clone(sc); out.pop("password", None)
            self._json({"ok": True, "sync": out})
        elif path == "/api/sync":
            direction = data.get("direction") or "both"
            if direction not in ("both", "pull", "push"):
                direction = "both"
            with SYNC_LOCK:
                res = do_sync(direction)
            global LAST_SYNC
            LAST_SYNC = {"time": time.time(), "ok": res.get("ok"),
                         "msg": res.get("message") or res.get("error") or "同步完成"}
            if res.get("ok"):
                add_event("info", "手动同步：" + (res.get("message") or "完成"))
                trigger_check(record=False)
            else:
                add_event("warn", "同步失败：" + (res.get("error") or "未知错误"))
            self._json(res, 200 if res.get("ok") else 502)
        elif path == "/api/fan":
            with LOCK:
                fan = CONFIG.setdefault("fan", {"enabled": False, "gpio": 3, "inverted": False})
                if isinstance(data.get("enabled"), bool):
                    fan["enabled"] = data["enabled"]
                if "gpio" in data:
                    try:
                        g = int(data["gpio"])
                        if 0 <= g <= 27:
                            fan["gpio"] = g
                    except (TypeError, ValueError):
                        pass
                if isinstance(data.get("inverted"), bool):
                    fan["inverted"] = data["inverted"]
                save_config()
            # 引脚 / 反转电平改变时重置句柄，下次设置使用新引脚
            if "gpio" in data or "inverted" in data:
                global _FAN_HANDLE, _FAN_PIN
                if _FAN_HANDLE is not None:
                    try:
                        os.close(_FAN_HANDLE)
                    except Exception:
                        pass
                _FAN_HANDLE = None
                _FAN_PIN = None
            msg = None
            if "state" in data and fan.get("enabled"):
                ok, m = fan_apply_state(bool(data["state"]), fan)
                msg = m
                if not ok:
                    self._json({"ok": False, "error": m, "fan": fan_get_status(), "temp": read_cpu_temp()}, 502)
                    return
            if msg is None:
                msg = "风扇设置已保存"
            add_event("info", "风扇：" + msg)
            self._json({"ok": True, "message": msg, "fan": fan_get_status(), "temp": read_cpu_temp()})
        elif path == "/api/panel/server/save":
            # 独立运行状态服务器：新增（无 id）或编辑（带 id）
            payload = dict(data)
            if isinstance(data.get("panel"), dict):   # 兼容旧前端结构
                p = data["panel"]
                payload = {"id": data.get("id"), "name": data.get("name") or p.get("base"),
                           "type": p.get("type"), "base": p.get("base"), "key": p.get("key")}
            srv = upsert_status_server(payload)
            if srv is None:
                self._json({"ok": False, "error": "名称、地址、类型必填"}, 400)
                return
            add_event("info", f"保存运行状态服务器：{srv['name']}")
            self._json({"ok": True, "server": srv})
        elif path == "/api/panel/server/delete":
            sid = str(data.get("id") or "")
            deleted = delete_status_server(sid)
            if deleted:
                add_event("info", f"删除运行状态服务器：{sid}")
            self._json({"ok": deleted})
        elif path == "/api/panel/server/reorder":
            ok = reorder_status_servers(data.get("ids") or [])
            self._json({"ok": ok}, 200 if ok else 400)
        elif path == "/api/panel/config":
            # 旧端点兼容：透传到新结构（无独立 name，用 base 兜底）
            sid = str(data.get("id") or "")
            p = data.get("panel") or {}
            cfg = normalize_panel(p)
            if not cfg:
                self._json({"ok": delete_status_server(sid)})
                return
            with LOCK:
                existing = next((s for s in CONFIG.get("status_servers", []) if s.get("id") == sid), None)
                merged = {"id": sid, "name": (existing or {}).get("name") or cfg.get("base"),
                          "type": cfg.get("type"), "base": cfg.get("base"), "key": cfg.get("key")}
            srv = upsert_status_server(merged)
            self._json({"ok": bool(srv)})
        else:
            self._json({"ok": False, "error": "unknown endpoint"}, 404)


# ---------------------------------------------------------------- 入口
def main():
    global ACTUAL_PORT, CONFIG_PATH, STORE_PATH
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="面板在线监测台")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认 8765")
    parser.add_argument("--host", default=None, help="监听地址，默认 127.0.0.1；Docker 内需设为 0.0.0.0")
    parser.add_argument("--config", default=None, help="指定其它 servers.json 配置文件")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()

    if args.config:
        CONFIG_PATH = Path(args.config).resolve()
        STORE_PATH = CONFIG_PATH.with_name("monitor_data.json")
    load_config()
    load_store()
    with LOCK:
        for s in CONFIG.get("servers", []):
            RESULTS.setdefault(s["id"], blank_result(s["id"]))
    add_event("info", f"监测服务启动，共 {len(CONFIG.get('servers', []))} 台面板")

    bind_host = args.host or "127.0.0.1"
    wanted = args.port or CONFIG.get("listen", {}).get("port", 8765)
    httpd = None
    for p in range(wanted, wanted + 12):
        try:
            httpd = ThreadingHTTPServer((bind_host, p), Handler)
            ACTUAL_PORT = p
            break
        except OSError:
            continue
    if httpd is None:
        print(f"[错误] {wanted}~{wanted + 11} 端口都被占用了，请用 --port 指定其它端口")
        return 1
    httpd.daemon_threads = True

    url = f"http://127.0.0.1:{ACTUAL_PORT}"
    print("=" * 62)
    print("  面板在线监测台 v2.0 已启动")
    print(f"  打开地址：{url}")
    print(f"  监听地址：{bind_host}:{ACTUAL_PORT}")
    print(f"  配置文件：{CONFIG_PATH}")
    print(f"  历史数据：{STORE_PATH}")
    print(f"  自动检测：每 {CONFIG.get('interval', 30)} 秒（后端常驻，关页面也继续）")
    print("  探测直连，不受系统代理影响；按 Ctrl+C 退出")
    print("=" * 62)

    threading.Thread(target=auto_loop, daemon=True).start()
    threading.Thread(target=sync_auto_loop, daemon=True).start()
    trigger_check()
    if not args.no_browser and CONFIG.get("listen", {}).get("auto_open", True):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        with LOCK:
            save_store()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
