#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UCSD 排课助手
-------------
纯 Python 标准库实现，无需安装任何第三方包。

功能：
  1. 用 UCSD Class Planner 官方接口拉取课程分班
  2. 生成所有不冲突的周一到周五课表
  3. 锁定指定老师
  4. 每个老师提供 Rate My Professor 直达链接
  5. 用 UCSD 官方 Wayfinding 路线服务估算楼宇间步行时间
  6. 调用 DeepSeek API 对可行组合给出推荐（需要你自己的 API Key）

运行方式：
  python scheduler.py            # 默认端口 8777，自动打开浏览器
  python scheduler.py --port 9000
"""

import argparse
import json
import math
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(SCRIPT_DIR, "index.html")
BUILDINGS_CACHE = os.path.join(SCRIPT_DIR, "buildings_cache.json")
ROUTES_CACHE = os.path.join(SCRIPT_DIR, "routes_cache.json")
SAVED_DATA = os.path.join(SCRIPT_DIR, "saved_data.json")
RMP_CACHE_FILE = os.path.join(SCRIPT_DIR, "rmp_cache.json")

CLASS_PLANNER = "https://classplanner.apps.ucsd.edu"
BUILDINGS_URL = (
    "https://admin-enterprise-gis.ucsd.edu/server/rest/services/"
    "AdministrationServices/Buildings_Public/MapServer/0/query"
)
ROUTE_URL = (
    "https://admin-enterprise-gis.ucsd.edu/server/rest/services/"
    "Wayfinding/Campus_Wayfinding_Network/NAServer/Route/solve"
)
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

RMP_SEARCH_URL = "https://www.ratemyprofessors.com/search/professors/1079"
_rmp_memory_cache = {}

# 官方 Buildings 图层里缺失、但课表会用到的重要楼宇（坐标来自 UCSD 教室图层）
MANUAL_BUILDINGS = {
    "LEDDN": {
        "name": "Ledden Auditorium",
        "lat": 32.87824998,
        "lon": -117.24161500,
        "aliases": ["Ledden Auditorium", "LEDDN"],
    },
}

# 少数课程不是 4 学分（例如语言课），在这里覆盖
UNITS_OVERRIDE = {
    "JAPN 020A": 5,
}

DAY_ORDER = ["M", "T", "W", "R", "F"]
DAY_LABEL = {"M": "周一", "T": "周二", "W": "周三", "R": "周四", "F": "周五", "S": "周六", "U": "周日"}


# ----------------------------- 网络请求 -----------------------------

_insecure_fallback_notice = [False]


def _urlopen_with_fallback(req, timeout):
    """正常请求；遇到 HTTPS 证书校验失败时（常见于代理/本地证书问题），
    自动改用不校验证书的方式重试一次。仅用于 UCSD 公开课程/楼宇/RMP 数据，
    不用于 DeepSeek（涉及 API Key，保持严格校验）。"""
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as e:
        reason = e.reason
        is_cert_error = isinstance(reason, ssl.SSLCertVerificationError) or (
            reason is not None and "CERTIFICATE_VERIFY_FAILED" in str(reason)
        )
        if not is_cert_error:
            raise
        if not _insecure_fallback_notice[0]:
            _insecure_fallback_notice[0] = True
            print("[警告] HTTPS 证书校验失败，已自动改用不校验证书的方式重试（仅用于 UCSD 公开数据）")
        return urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context())


def _http_json(url, method="GET", data=None, headers=None, timeout=60):
    """发送 HTTP 请求并解析 JSON；失败时抛出带信息的异常。"""
    req_headers = {"User-Agent": "Mozilla/5.0 (course-scheduler)"}
    if headers:
        req_headers.update(headers)
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req_headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with _urlopen_with_fallback(req, timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {url}\n{detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason} ({url})")
    return json.loads(raw)


def _http_json_body(url, payload, headers=None, timeout=60):
    """发送 JSON body 的请求（用于 Class Planner 的 POST 搜索）。"""
    req_headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (course-scheduler)"}
    if headers:
        req_headers.update(headers)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with _urlopen_with_fallback(req, timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {url}\n{detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason} ({url})")
    return json.loads(raw)


# ----------------------------- Class Planner -----------------------------

_course_search_cache = {}
_detail_cache = {}
_saved_lock = threading.Lock()
_jobs_lock = threading.Lock()
_jobs = {}
_job_counter = 0


def _load_saved():
    """读取本地保存的课程状态和对话记录。"""
    try:
        with open(SAVED_DATA, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"state": {}, "conversations": [], "combos": [], "bookmarks": []}


def _save_saved(data):
    """原子写入本地保存文件。"""
    with _saved_lock:
        tmp = SAVED_DATA + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SAVED_DATA)


def get_terms():
    d = _http_json(f"{CLASS_PLANNER}/api/v1/planner/terms", timeout=30)
    terms = d.get("terms", [])
    codes = [t["term_code"] for t in terms]
    updated = {
        t["term_code"]: t["last_full_refresh_at"].strip().replace(" ", "T").replace("+00", "+00:00")
        for t in terms if t.get("last_full_refresh_at")
    }
    return codes, updated


def search_courses(term, query):
    key = (term, query.lower())
    if key in _course_search_cache:
        return _course_search_cache[key]
    url = f"{CLASS_PLANNER}/api/v1/planner/courses?{urllib.parse.urlencode({'term_code': term, 'q': query, 'limit': 10})}"
    d = _http_json(url, timeout=30)
    out = []
    for c in d.get("candidates", []):
        out.append({
            "module_code": c.get("module_code"),
            "subject_code": c.get("subject_code"),
            "course_code": c.get("course_code"),
            "module_name": c.get("module_name"),
            "instructors": c.get("instructors", []),
            "section_count": c.get("section_count", 0),
            "open_section_count": c.get("open_section_count", 0),
        })
    _course_search_cache[key] = out
    return out


def fetch_course_detail(term, module_code):
    """按 module_code（如 MATH-020D）获取完整分班数据。"""
    key = (term, module_code)
    if key in _detail_cache:
        return _detail_cache[key]
    parts = module_code.split("-", 1)
    if len(parts) != 2:
        raise RuntimeError(f"课程代码格式不对: {module_code}")
    course_key = f"{parts[0]} {parts[1]}"
    payload = {
        "term_code": term,
        "q": module_code,
        "course_key": [course_key],
        "subject_code": [],
        "academic_level": [],
        "instructor": [],
        "instruction_type": [],
        "availability": "any",
        "delivery": "any",
        "day_code": [],
        "earliest_start": None,
        "latest_end": None,
        "sort": "relevance",
        "direction": "asc",
        "offset": 0,
        "limit": 48,
    }
    d = _http_json_body(f"{CLASS_PLANNER}/api/v1/catalog/courses/search", payload, timeout=45)
    courses = d.get("courses", [])
    if not courses:
        raise RuntimeError(f"没有找到 {module_code} 的分班信息")
    model = _normalize_course(courses[0])
    _detail_cache[key] = model
    return model


def _normalize_course(c):
    """把 Class Planner 的课程 JSON 转成内部结构。"""
    families = {}
    instructors = set()
    raw_sections = []
    for s in c.get("sections", []):
        m = re.match(r"^(\d+)-", s.get("section_code", ""))
        family = m.group(1) if m else "0"
        sec = {
            "section_code": s.get("section_code"),
            "section_id": s.get("section_id"),
            "type": s.get("instruction_type_name") or s.get("instruction_type"),
            "instructors": s.get("instructors") or [],
            "seats_available": s.get("seats_available"),
            "capacity": s.get("capacity"),
            "status": s.get("status"),
            "waitlist_available": s.get("waitlist_available"),
            "meetings": [],
        }
        for mt in s.get("meetings", []):
            sec["meetings"].append({
                "kind": mt.get("meeting_kind", "class"),
                "day": mt.get("day_code", ""),
                "date": mt.get("specific_date") or "",
                "start": mt.get("start_minutes"),
                "end": mt.get("end_minutes"),
                "start_display": mt.get("start_time_display"),
                "end_display": mt.get("end_time_display"),
                "tba": bool(mt.get("is_tba")),
                "building": mt.get("building_code") or "",
                "room": mt.get("room_code") or "",
            })
        for i in sec["instructors"]:
            instructors.add(i)
        families.setdefault(family, {}).setdefault(sec["type"], []).append(sec)
        raw_sections.append(sec)

    # 每个分组的组件组合：每种 type 选一个
    family_combos = {}
    for fam, comps in families.items():
        lists = [list(v) for v in comps.values()]
        combos = [[]]
        for lst in lists:
            combos = [c + [s] for c in combos for s in lst]
        family_combos[fam] = combos

    return {
        "module_code": c.get("module_code"),
        "subject_code": c.get("subject_code"),
        "course_code": c.get("course_code"),
        "module_name": c.get("module_name"),
        "units": UNITS_OVERRIDE.get(f"{c.get('subject_code')} {c.get('course_code')}", 4),
        "instructors": sorted(instructors),
        "family_combos": family_combos,
        "sections": raw_sections,
    }


# ----------------------------- 冲突检测 -----------------------------

def _overlap(a, b):
    if a.get("day") != b.get("day"):
        return False
    if a.get("tba") or b.get("tba"):
        return False
    sa, ea = a.get("start"), a.get("end")
    sb, eb = b.get("start"), b.get("end")
    if sa is None or ea is None or sb is None or eb is None:
        return False
    return sa < eb and sb < ea


def meetings_conflict(sec_a, sec_b):
    """检查两个 section 是否上课时间冲突（final 只和 final 比）。"""
    for ma in sec_a["meetings"]:
        for mb in sec_b["meetings"]:
            if ma.get("kind") == "final" or mb.get("kind") == "final":
                if ma.get("kind") == "final" and mb.get("kind") == "final" and _overlap(ma, mb):
                    return True
                continue
            if _overlap(ma, mb):
                return True
    return False


def schedule_conflict(combo):
    """combo 是若干 section 的列表，检查两两冲突。"""
    for i in range(len(combo)):
        for j in range(i + 1, len(combo)):
            if meetings_conflict(combo[i], combo[j]):
                return True
    return False


# ----------------------------- 楼宇与步行时间 -----------------------------

_buildings = None
_route_cache = {}


def _load_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def ensure_buildings(force=False):
    """拉取 UCSD 楼宇坐标，建立代码 -> 坐标 索引。"""
    global _buildings
    if _buildings is not None and not force:
        return _buildings
    disk = _load_cache(BUILDINGS_CACHE)
    if disk and not force:
        _buildings = _merge_manual_buildings(disk)
        return _buildings
    url = BUILDINGS_URL + "?" + urllib.parse.urlencode({
        "where": "1=1",
        "outFields": "FacilityLongName,BuildingAliases,Longitude,Latitude",
        "returnGeometry": "false",
        "f": "json",
        "resultRecordCount": 2000,
    })
    d = _http_json(url, timeout=60)
    index = {}
    for feat in d.get("features", []):
        a = feat.get("attributes", {})
        name = (a.get("FacilityLongName") or "").strip()
        lat = a.get("Latitude")
        lon = a.get("Longitude")
        if not name or lat is None or lon is None:
            continue
        aliases = [x.strip() for x in re.split(r"[|\/]", str(a.get("BuildingAliases") or "")) if x.strip()]
        aliases.append(name)
        for al in aliases:
            code = al.upper().replace(" ", "")
            if code not in index:
                index[code] = {"name": name, "lat": float(lat), "lon": float(lon), "aliases": aliases}
    _buildings = _merge_manual_buildings(index)
    _save_cache(BUILDINGS_CACHE, index)
    return _buildings


def _merge_manual_buildings(index):
    for code, v in MANUAL_BUILDINGS.items():
        index.setdefault(code, dict(v))
    return index


def find_building(code):
    """根据 planner 的 building_code（如 CENTR）找楼宇坐标。"""
    if not code:
        return None
    index = ensure_buildings()
    key = code.upper().replace(" ", "")
    if key in index:
        return index[key]
    # 只做“整词”包含匹配，避免单字母别名（如 D）误匹配
    for k, v in index.items():
        if len(key) >= 3 and len(k) >= 3 and (key in k or k in key):
            return v
    return None


def _haversine_minutes(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp = math.radians(b["lat"] - a["lat"])
    dl = math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    meters = 2 * r * math.asin(math.sqrt(h))
    # 步行速度约 1.3 m/s，路径系数 1.25
    return meters / (1.3 * 60) * 1.25


def walking_minutes(code_a, code_b):
    """返回两个楼之间的步行分钟数（UCSD 官方路线优先，失败用直线估算）。"""
    if not code_a or not code_b or code_a == code_b:
        return None
    ba, bb = find_building(code_a), find_building(code_b)
    if ba is None or bb is None:
        return None
    key = "|".join(sorted([code_a.upper(), code_b.upper()]))
    if key in _route_cache:
        return _route_cache[key]
    disk = _load_cache(ROUTES_CACHE)
    if key in disk:
        _route_cache[key] = disk[key]
        return disk[key]
    minutes = None
    method = "route"
    try:
        stops = {
            "type": "features",
            "features": [
                {"geometry": {"x": ba["lon"], "y": ba["lat"]}},
                {"geometry": {"x": bb["lon"], "y": bb["lat"]}},
            ],
        }
        params = {
            "stops": json.dumps(stops),
            "f": "json",
            "inSR": "4326",
            "returnDirections": "false",
            "impedanceAttributeName": "WalkingMinutes",
        }
        d = _http_json(ROUTE_URL, method="POST", data=params, timeout=30)
        routes = d.get("routes", {}).get("features", [])
        if routes:
            minutes = routes[0]["attributes"].get("Total_WalkingMinutes")
            if minutes is not None:
                minutes = round(float(minutes), 1)
    except Exception:
        minutes = None
    if minutes is None:
        minutes = round(_haversine_minutes(ba, bb), 1)
        method = "straight"
    else:
        straight = _haversine_minutes(ba, bb)
        # 官方路线若比直线估算离谱太多（>1.6 倍 + 2 分钟），判定为绕路异常，改用直线估算
        if minutes > straight * 1.6 + 2:
            minutes = round(straight, 1)
            method = "straight"
    result = {"minutes": minutes, "method": method}
    _route_cache[key] = result
    disk[key] = result
    _save_cache(ROUTES_CACHE, disk)
    return result


# ----------------------------- 课表生成 -----------------------------

def _section_instructors(sec):
    return sec.get("instructors") or []


def _sec_matches_lock(sec, locked_name):
    if not locked_name:
        return True
    locked = locked_name.strip().lower()
    return any(locked == (i or "").strip().lower() for i in _section_instructors(sec))


def _course_combos(course, locked_name, only_open, pin_family=None, section_pins=None):
    out = []
    sec_pins = (section_pins or {}).get(course["module_code"], {})
    for fam, combos in course["family_combos"].items():
        if pin_family and fam != pin_family:
            continue
        for combo in combos:
            if locked_name:
                # 锁定老师只看正课（lecture）的老师；没有正课（如 seminar）时看第一个组件，
                # 避免讨论课/实验课由 TA 带导致正课教授被误筛掉。
                main_sec = next((s for s in combo if s.get("type") == "lecture"), combo[0])
                if not _sec_matches_lock(main_sec, locked_name):
                    continue
            if only_open and any((s.get("seats_available") is not None and s.get("seats_available") <= 0) for s in combo):
                continue
            if sec_pins:
                ok = True
                for s in combo:
                    want = sec_pins.get(s.get("type"))
                    if want and want != s.get("section_code"):
                        ok = False
                        break
                if not ok:
                    continue
            if any(s.get("type") == "lecture" for s in combo) or len(combo) >= 1:
                out.append((fam, combo))
    return out


def _meeting_sort_key(m):
    return (m.get("start") if m.get("start") is not None else 0)


def _day_meetings(schedule):
    """按天整理上课（不含 final），供步行计算。"""
    days = {d: [] for d in DAY_ORDER}
    for sec in schedule:
        for m in sec["meetings"]:
            if m.get("kind") == "final" or m.get("tba") or m.get("day") not in days:
                continue
            days[m["day"]].append({"time": m, "section": sec})
    for d in days:
        days[d].sort(key=lambda x: _meeting_sort_key(x["time"]))
    return days


def _finals(schedule, section_module):
    finals = []
    for sec in schedule:
        for m in sec["meetings"]:
            if m.get("kind") == "final" and not m.get("tba"):
                finals.append({
                    "section": sec,
                    "module": section_module.get(id(sec)),
                    "time": m,
                })
    finals.sort(key=lambda x: ((x["time"].get("date") or "9999-99-99"), x["time"].get("start") or 0))
    return finals


def _schedule_metrics(schedule, use_walking):
    seen_modules = set()
    total_units = 0
    for c in schedule["courses"]:
        if c["module_code"] in seen_modules:
            continue
        seen_modules.add(c["module_code"])
        total_units += c["units"]
    days = _day_meetings(schedule["sections"])
    walking_total = 0.0
    walking_detail = {}
    walking_ok = True
    for day, items in days.items():
        detail = []
        for i in range(len(items) - 1):
            a, b = items[i], items[i + 1]
            ba, bb = a["time"].get("building"), b["time"].get("building")
            r = walking_minutes(ba, bb) if (use_walking and ba and bb) else None
            if r is None:
                walking_ok = False
                detail.append({"from": ba or "?", "to": bb or "?", "minutes": None, "method": None})
            else:
                walking_total += r["minutes"]
                detail.append({"from": ba, "to": bb, "minutes": r["minutes"], "method": r["method"]})
        walking_detail[day] = detail
    finals = _finals(schedule["sections"], schedule.get("section_module", {}))
    class_days = [d for d in DAY_ORDER if days[d]]
    return {
        "total_units": total_units,
        "walking_total": round(walking_total, 1) if use_walking else None,
        "walking_ok": walking_ok if use_walking else True,
        "walking_detail": walking_detail,
        "finals": finals,
        "class_days": class_days,
    }


def generate_schedules(course_models, locks, options, on_progress=None, pins=None, section_pins=None):
    """
    course_models: 标准化后的课程列表
    locks: {module_code: 老师名或空}
    options: {only_open: bool, use_walking: bool}
    on_progress: 可选回调，用于汇报进度
    返回排好序的课表列表。
    """
    pool = []
    for cm in course_models:
        pin_family = (pins or {}).get(cm["module_code"])
        combos = _course_combos(cm, locks.get(cm["module_code"], ""), options.get("only_open", False), pin_family, section_pins)
        if not combos:
            raise RuntimeError(f"{cm['module_code']} 在当前条件下没有任何可选分班")
        pool.append((cm, combos))

    total_combos = 1
    for _, combos in pool:
        total_combos *= max(len(combos), 1)
    if on_progress:
        on_progress("enumerate", checked=0, total_combos=total_combos, found=0)

    results = []
    current = []
    checked = [0]

    def dfs(idx):
        if idx == len(pool):
            checked[0] += 1
            sections = [s for _, combo in current for s in combo]
            if schedule_conflict(sections):
                if on_progress and checked[0] % 50 == 0:
                    on_progress("enumerate", checked=checked[0], total_combos=total_combos, found=len(results))
                return
            schedule = {
                "courses": [],
                "sections": sections,
                "section_module": {},
            }
            seen = set()
            for cm, combo in current:
                for s in combo:
                    if id(s) in seen:
                        continue
                    seen.add(id(s))
                    schedule["section_module"][id(s)] = cm["module_code"]
                    schedule["courses"].append({
                        "module_code": cm["module_code"],
                        "subject_code": cm["subject_code"],
                        "course_code": cm["course_code"],
                        "module_name": cm["module_name"],
                        "units": cm["units"],
                        "section_code": s["section_code"],
                        "type": s["type"],
                        "instructors": s["instructors"],
                        "seats_available": s.get("seats_available"),
                        "capacity": s.get("capacity"),
                        "meetings": s["meetings"],
                    })
            results.append(schedule)
            if on_progress and checked[0] % 50 == 0:
                on_progress("enumerate", checked=checked[0], total_combos=total_combos, found=len(results))
            return

        cm, combos = pool[idx]
        for fam, combo in combos:
            current.append((cm, combo))
            dfs(idx + 1)
            current.pop()

    dfs(0)
    if on_progress:
        on_progress("enumerate", checked=checked[0], total_combos=total_combos, found=len(results))

    total_found = len(results)
    for idx, s in enumerate(results):
        s["metrics"] = _schedule_metrics(s, options.get("use_walking", True))
        if on_progress and (idx + 1) % 20 == 0:
            on_progress("metrics", metrics_done=idx + 1, metrics_total=total_found, found=total_found)
    if on_progress:
        on_progress("metrics", metrics_done=total_found, metrics_total=total_found, found=total_found)

    results.sort(key=lambda s: (
        s["metrics"]["total_units"],
        s["metrics"]["walking_total"] if s["metrics"]["walking_total"] is not None else 1e9,
        len(s["courses"]),
    ))
    # 返回全部可行方案，不做数量上限
    return results


def _serialize_schedules(schedules):
    """把内部课表结构转成可发给前端的 JSON。"""
    out = []
    for i, s in enumerate(schedules):
        m = s["metrics"]
        names = {}
        for c in s["courses"]:
            names.setdefault(c["module_code"], c["module_name"])
        out.append({
            "id": i,
            "courses": s["courses"],
            "total_units": m["total_units"],
            "walking_total": m["walking_total"],
            "walking_ok": m["walking_ok"],
            "walking_detail": m["walking_detail"],
            "finals": [{
                "course": x.get("module"),
                "course_name": names.get(x.get("module"), ""),
                "day": x["time"]["day"],
                "date": x["time"].get("date") or "",
                "start_display": x["time"]["start_display"],
                "end_display": x["time"]["end_display"],
                "room": x["time"]["room"],
                "building": x["time"]["building"],
            } for x in m["finals"]],
            "class_days": m["class_days"],
        })
    return out


def _schedule_job(job_id, term, modules, locks, options, pins=None, section_pins=None):
    """后台线程：生成全部可行方案并汇报进度。"""
    prog = {
        "status": "running",
        "phase": "prepare",
        "checked": 0,
        "total_combos": 0,
        "found": 0,
        "metrics_done": 0,
        "metrics_total": 0,
        "error": None,
    }
    with _jobs_lock:
        _jobs[job_id] = prog
    try:
        models = [fetch_course_detail(term, m) for m in modules]

        def on_progress(phase, **kw):
            prog["phase"] = phase
            prog.update(kw)

        schedules = generate_schedules(models, locks, options, on_progress, pins, section_pins)
        prog["phase"] = "serialize"
        out = _serialize_schedules(schedules)
        prog["status"] = "done"
        prog["result"] = out
    except Exception as e:
        prog["status"] = "error"
        prog["error"] = str(e)


# ----------------------------- DeepSeek -----------------------------

def call_deepseek(api_key, model, messages, thinking=None, reasoning_effort=None):
    payload = {
        "model": model or "deepseek-v4-flash",
        "messages": messages,
    }
    if thinking is not None:
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    if not thinking:
        payload["temperature"] = 0.7
    d = _http_json_body(
        DEEPSEEK_URL,
        payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=180,
    )
    return d


# ----------------------------- RMP 评分 -----------------------------

def _rmp_parse(raw):
    pattern = re.compile(
        r'"legacyId":(\d+),"avgRating":([\d.]+),"numRatings":(\d+),'
        r'"wouldTakeAgainPercent":([\d.]+),"avgDifficulty":([\d.]+),'
        r'"department":"([^"]*)","school":\{"__ref":"[^"]+"\},'
        r'"firstName":"([^"]*)","lastName":"([^"]*)"'
    )
    out = []
    for m in pattern.finditer(raw):
        legacy_id, rating, num, again, diff, dept, first, last = m.groups()
        out.append({
            "legacyId": int(legacy_id),
            "firstName": first,
            "lastName": last,
            "avgRating": float(rating),
            "numRatings": int(num),
            "wouldTakeAgainPercent": float(again),
            "avgDifficulty": float(diff),
            "department": dept,
            "url": f"https://www.ratemyprofessors.com/professor/{legacy_id}",
        })
    return out


def fetch_rmp_ratings(name):
    """按老师名字从 RMP 搜索页抓取评分（带内存+磁盘缓存）。"""
    key = re.sub(r"\s+", " ", (name or "").strip()).lower()
    if not key:
        return {"name": name, "matches": [], "best": None}
    if key in _rmp_memory_cache:
        return _rmp_memory_cache[key]
    disk = _load_cache(RMP_CACHE_FILE)
    if key in disk:
        _rmp_memory_cache[key] = disk[key]
        return disk[key]
    url = RMP_SEARCH_URL + "?" + urllib.parse.urlencode({"q": name})
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "text/html",
    })
    try:
        with _urlopen_with_fallback(req, 30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return {"name": name, "matches": [], "best": None, "error": str(e)}
    matches = _rmp_parse(raw)
    best = None
    want = key
    for m in matches:
        full = re.sub(r"\s+", " ", (m["firstName"] + " " + m["lastName"]).strip()).lower()
        if full == want:
            best = m
            break
    if best is None and matches:
        best = matches[0]
    result = {"name": name, "matches": matches, "best": best}
    _rmp_memory_cache[key] = result
    disk[key] = result
    _save_cache(RMP_CACHE_FILE, disk)
    return result


# ----------------------------- HTTP 服务 -----------------------------

def _send_json(handler, obj, status=200):
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _read_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except Exception:
        return {}


class SchedulerHandler(BaseHTTPRequestHandler):
    server_version = "UCSDScheduler/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _api(self, path, body=None, method="GET"):
        try:
            if path == "/api/terms":
                codes, updated = get_terms()
                _send_json(self, {"terms": codes, "term_updated": updated})
                return
            if path == "/api/state":
                if method == "POST":
                    data = _load_saved()
                    data["state"] = (body or {}).get("state") or {}
                    if "combos" in (body or {}):
                        data["combos"] = (body or {}).get("combos") or []
                    if "bookmarks" in (body or {}):
                        data["bookmarks"] = (body or {}).get("bookmarks") or []
                    _save_saved(data)
                    _send_json(self, {"ok": True})
                    return
                _send_json(self, _load_saved())
                return
            if path == "/api/conversations":
                if method == "POST":
                    conv = (body or {}).get("conversation") or {}
                    data = _load_saved()
                    convs = [c for c in data.get("conversations", []) if c.get("id") != conv.get("id")]
                    convs.insert(0, conv)
                    data["conversations"] = convs[:50]
                    _save_saved(data)
                    _send_json(self, {"ok": True, "id": conv.get("id")})
                    return
                if method == "DELETE":
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    cid = (qs.get("id") or [""])[0]
                    data = _load_saved()
                    if cid:
                        data["conversations"] = [c for c in data.get("conversations", []) if c.get("id") != cid]
                    else:
                        data["conversations"] = []
                    _save_saved(data)
                    _send_json(self, {"ok": True})
                    return
                _send_json(self, _load_saved())
                return
            if path == "/api/courses":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                term = (qs.get("term") or ["FA26"])[0]
                query = (qs.get("q") or [""])[0]
                _send_json(self, {"courses": search_courses(term, query)})
                return
            if path == "/api/course":
                data = body or {}
                term = data.get("term") or "FA26"
                module = data.get("module_code") or ""
                _send_json(self, {"course": fetch_course_detail(term, module)})
                return
            if path == "/api/refresh" and method == "POST":
                _course_search_cache.clear()
                _detail_cache.clear()
                _send_json(self, {"ok": True})
                return
            if path == "/api/schedules":
                data = body or {}
                global _job_counter
                with _jobs_lock:
                    _job_counter += 1
                    job_id = f"job{int(time.time() * 1000)}_{_job_counter}"
                threading.Thread(
                    target=_schedule_job,
                    args=(
                        job_id,
                        data.get("term") or "FA26",
                        data.get("modules") or [],
                        data.get("locks") or {},
                        data.get("options") or {},
                        data.get("pins") or {},
                        data.get("section_pins") or {},
                    ),
                    daemon=True,
                ).start()
                _send_json(self, {"job_id": job_id})
                return
            if path == "/api/progress":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                job_id = (qs.get("job") or [""])[0]
                with _jobs_lock:
                    prog = dict(_jobs.get(job_id) or {})
                if not prog:
                    _send_json(self, {"error": "任务不存在"}, status=404)
                    return
                prog.pop("result", None)
                _send_json(self, {"progress": prog})
                return
            if path == "/api/schedules/result":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                job_id = (qs.get("job") or [""])[0]
                with _jobs_lock:
                    prog = _jobs.get(job_id)
                if not prog:
                    _send_json(self, {"error": "任务不存在"}, status=404)
                    return
                if prog.get("status") == "running":
                    _send_json(self, {"pending": True})
                    return
                if prog.get("status") == "error":
                    _send_json(self, {"error": prog.get("error")}, status=500)
                    return
                with _jobs_lock:
                    result = prog.pop("result", [])
                    _jobs.pop(job_id, None)
                _send_json(self, {"schedules": result, "count": len(result)})
                return
            if path == "/api/rmp":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                name = (qs.get("name") or [""])[0]
                _send_json(self, fetch_rmp_ratings(name))
                return
            if path == "/api/route":
                data = body or {}
                r = walking_minutes(data.get("from") or "", data.get("to") or "")
                _send_json(self, {"result": r})
                return
            if path == "/api/building":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code = (qs.get("code") or [""])[0]
                b = find_building(code)
                _send_json(self, {"building": b})
                return
            if path == "/api/deepseek":
                data = body or {}
                api_key = data.get("api_key") or ""
                if not api_key:
                    _send_json(self, {"error": "请先填写 DeepSeek API Key"}, status=400)
                    return
                d = call_deepseek(
                    api_key,
                    data.get("model"),
                    data.get("messages") or [],
                    thinking=data.get("thinking"),
                    reasoning_effort=data.get("reasoning_effort"),
                )
                _send_json(self, {"response": d})
                return
            _send_json(self, {"error": "未知接口"}, status=404)
        except Exception as e:
            _send_json(self, {"error": str(e)}, status=500)

    def _stream_deepseek(self, data):
        """把 DeepSeek 的流式回答原样转发给浏览器（SSE）。"""
        api_key = (data or {}).get("api_key") or ""
        if not api_key:
            _send_json(self, {"error": "请先填写 DeepSeek API Key"}, status=400)
            return
        payload = {
            "model": (data or {}).get("model") or "deepseek-v4-flash",
            "messages": (data or {}).get("messages") or [],
            "stream": True,
        }
        thinking = (data or {}).get("thinking")
        if thinking is not None:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        effort = (data or {}).get("reasoning_effort")
        if effort:
            payload["reasoning_effort"] = effort
        if not thinking:
            payload["temperature"] = 0.7
        req = urllib.request.Request(
            DEEPSEEK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=240)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            _send_json(self, {"error": f"HTTP {e.code}: {detail}"}, status=500)
            return
        except urllib.error.URLError as e:
            _send_json(self, {"error": f"网络错误: {e.reason}"}, status=500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                self.wfile.write(line)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()
        try:
            # 显式发送 SSE 结束标记，并关闭连接，让浏览器知道流已结束
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/"):
            self._api(path)
            return
        if path == "/" or path == "/index.html":
            try:
                with open(INDEX_FILE, "rb") as f:
                    raw = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except FileNotFoundError:
                _send_json(self, {"error": "index.html 不存在，请和 scheduler.py 放在同一目录"}, status=500)
            return
        _send_json(self, {"error": "not found"}, status=404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = _read_body(self)
        if path == "/api/deepseek/stream":
            self._stream_deepseek(body)
            return
        if path.startswith("/api/"):
            self._api(path, body, "POST")
            return
        _send_json(self, {"error": "not found"}, status=404)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/"):
            self._api(path, None, "DELETE")
            return
        _send_json(self, {"error": "not found"}, status=404)


def main():
    parser = argparse.ArgumentParser(description="UCSD 排课助手")
    parser.add_argument("--port", type=int, default=8777, help="本地端口（默认 8777）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--refresh-buildings", action="store_true", help="强制刷新楼宇缓存")
    args = parser.parse_args()

    if args.refresh_buildings:
        ensure_buildings(force=True)

    port = args.port
    server = None
    for p in range(port, port + 6):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), SchedulerHandler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("端口都被占用，请换一个端口重试。")
        sys.exit(1)

    url = f"http://127.0.0.1:{port}/"
    print("=" * 56)
    print(" UCSD 排课助手已启动")
    print(f" 打开浏览器访问: {url}")
    print(" 按 Ctrl+C 停止服务")
    print("=" * 56)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
