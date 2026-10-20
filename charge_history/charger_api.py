"""
charger_api.py — 充电桩数据获取层

插座数据格式（fetch_all_stations 返回）：
  {
    "站点名": [
      {
        "serial":   int,
        "outletNo": str,
        "status":   int,        # 1=空闲 2=占用 3=损坏
        "power_w":  int/None,   # 当前充电功率（W），仅占用时有值
        "fee":      float/None, # 当前累计费用（元），仅占用时有值
        "used_min": int/None,   # 已充时长（分钟），仅占用时有值
      },
      ...
    ],
    ...
  }
"""

import json
import random
import threading
import time

import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ══════════════════════════════════════════════════════════
#  网络配置
# ══════════════════════════════════════════════════════════
PROXIES          = {"http": None, "https": None}
TIMEOUT          = 10
RETRIES          = 2          # 失败重试次数（不含首次）
BACKOFF_BASE     = 0.4        # 退避基数（秒）
POOL_SIZE        = 16         # 单线程连接池大小

# ══════════════════════════════════════════════════════════
#  请求头
# ══════════════════════════════════════════════════════════
HEADERS = {
    "sec-ch-ua-platform": "\"Android\"",
    "sec-ch-ua": "\"Chromium\";v=\"142\", \"Android WebView\";v=\"142\", \"Not_A Brand\";v=\"99\"",
    "systemphone": "Android 16",
    "sec-ch-ua-mobile": "?1",
    "brands": "2407FRK8EC",
    "user-agent": (
        "Mozilla/5.0 (Linux; Android 16; 2407FRK8EC Build/BP2A.250605.031.A3; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Mobile Safari/537.36"
    ),
    "content-type": "application/json;charset=utf-8",
    # "token": "YOUR_TOKEN_HERE",
    "accept": "*/*",
    "origin": "https://api.issks.com",
    "x-requested-with": "com.tencent.mm",
    "sec-fetch-site": "same-site",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
    "referer": "https://api.issks.com/",
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "priority": "u=1, i",
}

STATUS_FREE    = 1
STATUS_BUSY    = 2
STATUS_BROKEN  = 3
KNOWN_STATUSES = (STATUS_FREE, STATUS_BUSY, STATUS_BROKEN)


# ══════════════════════════════════════════════════════════
#  HTTP 连接复用（线程级 Session）
#
#  上游每个请求都要重新做一次 TLS 握手，实测占总耗时 80% 以上。
#  复用连接后同一批请求快 4 倍左右，直接决定全量采样一轮要多久。
# ══════════════════════════════════════════════════════════
_local = threading.local()


def _session():
    """每个线程一个 Session，复用 TCP/TLS 连接。"""
    sess = getattr(_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        # 让 requests 自己协商能解码的压缩方式，避免 br/zstd 解不开
        sess.headers.pop("accept-encoding", None)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=POOL_SIZE,
            pool_maxsize=POOL_SIZE,
            max_retries=0,
        )
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _local.session = sess
    return sess


def _http_get(url):
    """
    GET 请求，带指数退避重试。
    成功返回 requests.Response，失败返回 None。
    """
    last_error = None
    for attempt in range(RETRIES + 1):
        try:
            resp = _session().get(url, proxies=PROXIES, timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp
            last_error = "HTTP {}".format(resp.status_code)
        except Exception as exc:
            last_error = str(exc)
        if attempt < RETRIES:
            time.sleep(BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.2))
    print("[API] {} 请求失败（已重试 {} 次）: {}".format(url, RETRIES, last_error))
    return None


# ══════════════════════════════════════════════════════════
#  站点配置
# ══════════════════════════════════════════════════════════

def _find_station_json():
    import sys
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    return base / "station.json"


def load_stations():
    """读取 station.json，返回 {name: station_id} 字典。"""
    path = _find_station_json()
    if not path.exists():
        raise FileNotFoundError(
            "找不到 station.json，请将其放在程序同目录下：{}".format(path)
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════
#  单插座详情（仅对占用中插座调用）
# ══════════════════════════════════════════════════════════

def fetch_outlet_detail(outlet_no):
    """
    获取单个插座详情。
    返回 {"power_w": int, "fee": float, "used_min": int}，失败返回 None。
    """
    url = "https://wemp.issks.com/charge/v1/charging/outlet/{}".format(outlet_no)
    try:
        resp = _http_get(url)
        if resp is None:
            return None
        data = resp.json()
        if str(data.get("code")) != "1":
            return None

        # 上游偶尔会返回 data: null，这里必须兜底，否则整条插座数据会丢
        d = data.get("data") or {}

        # 功率：powerFee.billingPower 形如 "223W"
        power_str = (d.get("powerFee") or {}).get("billingPower", "0W")
        try:
            power_w = int("".join(c for c in power_str if c.isdigit()) or "0")
        except Exception:
            power_w = 0

        fee      = float(d.get("usedfee") or 0.0)
        used_min = int(d.get("usedmin") or 0)

        return {"power_w": power_w, "fee": fee, "used_min": used_min}

    except Exception as e:
        print("[API] 插座 {} 详情请求失败: {}".format(outlet_no, e))
        return None


# ══════════════════════════════════════════════════════════
#  单站查询（含并发获取占用插座详情）
# ══════════════════════════════════════════════════════════

def fetch_station(station_id):
    """
    查询单站所有插座基础状态，对占用插座并发获取详情。
    """
    url = "https://wemp.issks.com/charge/v1/outlet/station/outlets/{}".format(station_id)
    try:
        resp = _http_get(url)
        if resp is None:
            return None
        data = resp.json()
        if str(data.get("code")) != "1":
            return None

        outlets = []
        for outlet in data.get("data", []):
            raw    = outlet.get("currentChargingRecordId")
            status = raw if raw in KNOWN_STATUSES else STATUS_BROKEN
            outlets.append({
                "serial":   outlet.get("outletSerialNo", 0),
                "outletNo": outlet.get("outletNo", ""),
                "status":   status,
                "power_w":  None,
                "fee":      None,
                "used_min": None,
            })

        # 并发获取所有占用插座的详情
        busy = [o for o in outlets if o["status"] == STATUS_BUSY and o["outletNo"]]
        if busy:
            with ThreadPoolExecutor(max_workers=min(len(busy), 6)) as ex:
                futures = {ex.submit(fetch_outlet_detail, o["outletNo"]): o
                           for o in busy}
                for fut in as_completed(futures):
                    detail = fut.result()
                    if detail:
                        futures[fut].update(detail)

        return outlets

    except Exception as e:
        print("[API] 站点 {} 请求失败: {}".format(station_id, e))
        return None


# ══════════════════════════════════════════════════════════
#  全量查询（顺序请求，站点间sleep，保持原始顺序）
# ══════════════════════════════════════════════════════════

def fetch_all_stations():
    """
    读取 station.json，顺序查询所有站点，保持原始顺序返回。
    返回 {"站点名": [outlet, ...], ...}
    """
    stations = load_stations()
    result   = {}
    names    = list(stations.keys())

    for name in names:
        sid     = stations[name]
        outlets = fetch_station(sid)
        result[name] = outlets if outlets is not None else []
        print("[API] {}({}): {}".format(
            name, sid,
            "失败" if outlets is None else "{} 个插座".format(len(outlets))
        ))


    return result
