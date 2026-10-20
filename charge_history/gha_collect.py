"""
gha_collect.py — GitHub Actions 用的一次性采样器

和 charge_history_backend.py 的分工：
  - 后端是常驻进程，按整点刻度无限循环，直接写 SQLite；
  - 本脚本只采一轮就退出，把结果写成 **gzip 压缩的 JSONL 分片**，
    由 workflow 提交到仓库的 data 分支。回到本地再用 gha_merge.py 合并成库。

为什么不在 Actions 里直接写 SQLite：
  每 20 分钟跑一次，如果每次都要把整库拉下来再提交回去，仓库会被历史版本撑爆
  （一天 30 MB 级 × 72 次提交）。分片是「一次运行一个文件」的追加式写入，
  单个文件几十 KB，git 历史只增不重写，也不会互相冲突。

分片格式（每行一个 JSON，gzip 压缩）：
    {"v":1,"kind":"meta","sample_time":"...","started_at":"...",...}
    {"v":1,"kind":"station","station_id":120204,"station_name":"...","outlets":[...]}
    {"v":1,"kind":"failed","station_id":120204,"station_name":"...","error":"..."}

用法：
    python charge_history/gha_collect.py                     # 采一轮，分片写到 gha_data/
    python charge_history/gha_collect.py --station-limit 3   # 只采前 3 站，联调用
    python charge_history/gha_collect.py --db /tmp/t.db      # 顺手直接写一个 SQLite 验证
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from charger_api import fetch_station  # noqa: E402
from charge_history_backend import HistoryDatabase, iso_utc, load_station_map  # noqa: E402

SHARD_VERSION = 1
DEFAULT_STATIONS = RUNTIME_DIR / "all.json"
DEFAULT_OUT_DIR = "gha_data"
DEFAULT_WORKERS = 8


def collect_once(
    station_map: Dict[str, int],
    workers: int,
) -> Tuple[str, str, List[Dict[str, Any]]]:
    """
    采一轮，返回 (started_at, sample_time, results)。

    站点并发策略与后端一致：线程池按站点并发，站点内部再并发拉占用插座详情。
    """
    started_at = iso_utc()
    t0 = time.monotonic()
    results: List[Dict[str, Any]] = []

    total = len(station_map)
    print("[gha] 开始采集 {} 站，并发 {}，sample_time={}".format(total, workers, started_at))

    worker_count = max(1, min(workers, len(station_map)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(fetch_station, station_id): (name, station_id)
            for name, station_id in station_map.items()
        }
        for done, future in enumerate(as_completed(futures), start=1):
            station_name, station_id = futures[future]
            error: Optional[str] = None
            try:
                outlets = future.result()
            except Exception as exc:  # 单站异常不该拖垮整轮
                outlets = None
                error = str(exc)
            else:
                if outlets is None:
                    error = "接口返回空数据"
            results.append(
                {
                    "station_name": station_name,
                    "station_id": station_id,
                    "outlets": outlets,
                    "error": error,
                }
            )
            # 每站都打一行，Actions 日志里能实时看到进度，不至于以为卡死
            if error:
                print("[gha] {}/{} {}({}) 失败: {}".format(
                    done, total, station_name, station_id, error))
            else:
                print("[gha] {}/{} {}({}) {} 个插座".format(
                    done, total, station_name, station_id, len(outlets or [])))

    results.sort(key=lambda item: item["station_id"])

    # 整轮共用一个 sample_time，和后端的约定保持一致
    sample_time = started_at
    elapsed = time.monotonic() - t0
    print("[gha] 采集完成：{} 站，耗时 {:.1f}s".format(len(results), elapsed))
    return started_at, sample_time, results


def write_shard(
    out_dir: Path,
    started_at: str,
    sample_time: str,
    results: List[Dict[str, Any]],
    duration_s: float,
    station_total: int,
) -> Optional[Path]:
    """把一轮结果写成一个 gzip JSONL 分片，返回路径。"""
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    # 时间戳取 sample_time（UTC），形如 2026-10-20T15:30:00+00:00
    stamp = sample_time.replace("+00:00", "Z")
    day = stamp[:10]
    clock = stamp[11:19].replace(":", "")
    suffix = "_{}".format(run_id) if run_id else ""

    target_dir = out_dir / day
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "{}{}.jsonl.gz".format(clock, suffix)

    station_ok = sum(1 for item in results if item["outlets"] is not None)
    outlet_total = sum(len(item["outlets"] or []) for item in results)
    busy_total = sum(
        1
        for item in results
        for outlet in (item["outlets"] or [])
        if outlet.get("status") == 2
    )

    meta = {
        "v": SHARD_VERSION,
        "kind": "meta",
        "sample_time": sample_time,
        "started_at": started_at,
        "duration_s": round(duration_s, 1),
        "station_total": station_total,
        "station_ok": station_ok,
        "station_failed": station_total - station_ok,
        "outlet_total": outlet_total,
        "busy_total": busy_total,
        "source": "github-actions",
        "run_id": run_id,
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
    }

    with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as fh:
        fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for item in results:
            if item["outlets"] is None:
                fh.write(
                    json.dumps(
                        {
                            "v": SHARD_VERSION,
                            "kind": "failed",
                            "station_id": item["station_id"],
                            "station_name": item["station_name"],
                            "error": item["error"] or "未知错误",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                continue
            fh.write(
                json.dumps(
                    {
                        "v": SHARD_VERSION,
                        "kind": "station",
                        "station_id": item["station_id"],
                        "station_name": item["station_name"],
                        "outlets": item["outlets"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    return path


def write_sqlite(
    db_path: Path,
    sample_time: str,
    results: List[Dict[str, Any]],
) -> Dict[str, int]:
    """可选：直接把这一轮写进 SQLite，用于本地联调 / 验证。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    database = HistoryDatabase(db_path)
    try:
        for item in results:
            if item["outlets"] is None:
                continue
            database.record_station_snapshot(
                station_name=item["station_name"],
                station_id=item["station_id"],
                outlets=item["outlets"],
                sample_time=sample_time,
            )
        return database.totals()
    finally:
        database.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GitHub Actions 一次性采样")
    parser.add_argument(
        "--stations",
        default=str(DEFAULT_STATIONS),
        help="站点清单 JSON，默认脚本同目录下的 all.json",
    )
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help="分片输出目录，默认当前目录下的 gha_data/",
    )
    parser.add_argument(
        "--station-workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="站点并发线程数，默认 8",
    )
    parser.add_argument(
        "--station-limit",
        type=int,
        default=0,
        help="只采前 N 个站点（0 表示全部），联调用",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="额外把本轮写进这个 SQLite，用于本地验证",
    )
    parser.add_argument(
        "--max-fail-ratio",
        type=float,
        default=0.3,
        help="失败站点占比超过该值时以非零码退出（让 Actions 标红），默认 0.3",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    stations_path = Path(args.stations)
    if not stations_path.is_absolute():
        stations_path = (RUNTIME_DIR / stations_path).resolve()
    limit = args.station_limit if args.station_limit and args.station_limit > 0 else None
    station_map = load_station_map(stations_path, limit)

    print("[gha] 站点清单：{}（{} 站）".format(stations_path, len(station_map)))
    if not station_map:
        print("[gha] 站点清单为空，退出")
        return 1

    t0 = time.monotonic()
    started_at, sample_time, results = collect_once(station_map, args.station_workers)
    duration_s = time.monotonic() - t0

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = (Path.cwd() / out_dir).resolve()
    shard = write_shard(out_dir, started_at, sample_time, results, duration_s, len(station_map))
    if shard:
        size_kb = shard.stat().st_size / 1024
        print("[gha] 分片已写入：{}（{:.1f} KB）".format(shard, size_kb))

    failed = [item for item in results if item["outlets"] is None]
    if args.db:
        totals = write_sqlite(Path(args.db), sample_time, results)
        print("[gha] 已写入 SQLite：{} {}".format(args.db, totals))

    if failed:
        print("[gha] 失败站点 {} 个：".format(len(failed)))
        for item in failed[:10]:
            print("  - {}({}): {}".format(item["station_name"], item["station_id"], item["error"]))

    fail_ratio = len(failed) / max(1, len(results))
    if fail_ratio > args.max_fail_ratio:
        print("[gha] 失败占比 {:.0%} 超过阈值 {:.0%}，本轮标记为失败".format(fail_ratio, args.max_fail_ratio))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
