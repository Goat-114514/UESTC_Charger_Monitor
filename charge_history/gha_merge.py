"""
gha_merge.py — 把 GitHub Actions 采到的分片合并进 SQLite

为什么要重放而不是「解析分片直接 INSERT」：
  后端切分充电会话的逻辑（used_min / fee 回退判定新会话、结束状态判定、异常判定）
  全都在 charge_history_backend.HistoryDatabase.record_station_snapshot 里。
  这里直接复用同一个类按时间顺序重放，产出的表结构、会话切分结果和本地采集完全一致，
  前端 charge_history_ui.py 不用改一行就能读。

用法：
    # 1. 把 data 分支的数据同步到本地（在仓库根目录执行）
    git fetch origin data
    git archive --format=tar origin/data | tar -x -C gha_data

    # 2. 合并成一个新库
    python charge_history/gha_merge.py

    # 3. 或者直接合并进正在采集的本地库，让前端一次看到全部数据
    python charge_history/gha_merge.py --db charge_history/charge_data/charge_20261020_175905.db

已合并的分片记录在 ingest_shards 表里，重复执行不会产生重复数据。
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from charge_history_backend import HistoryDatabase, iso_utc  # noqa: E402

DEFAULT_SRC = "gha_data"
DEFAULT_DB = str(RUNTIME_DIR / "charge_data" / "gha_merged.db")


def iter_shards(src: Path) -> Iterator[Path]:
    """
    按时间顺序产出分片。

    目录结构是 gha_data/YYYY-MM-DD/HHMMSS[_runid].jsonl.gz，
    目录名 + 文件名天然就是字典序 = 时间序。
    """
    shards = list(src.rglob("*.jsonl.gz"))
    shards.sort(key=lambda path: (path.parent.name, path.name))
    return iter(shards)


def shard_key(src: Path, path: Path) -> str:
    """分片在 ingest_shards 表里的唯一键，用相对路径，换机器也能对上。"""
    try:
        return path.relative_to(src).as_posix()
    except ValueError:
        return path.name


def read_shard(path: Path) -> Dict[str, Any]:
    """读一个分片，返回 {sample_time, stations, failed}。"""
    sample_time: Optional[str] = None
    stations: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print("[merge] 跳过损坏的行：{}".format(path.name))
                continue
            kind = record.get("kind")
            if kind == "meta":
                sample_time = record.get("sample_time") or sample_time
            elif kind == "station":
                stations.append(record)
            elif kind == "failed":
                failed.append(record)

    return {"sample_time": sample_time, "stations": stations, "failed": failed}


def ensure_ingest_table(database: HistoryDatabase) -> None:
    database._conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_shards (
            name TEXT PRIMARY KEY,
            sample_time TEXT,
            station_count INTEGER NOT NULL DEFAULT 0,
            outlet_count INTEGER NOT NULL DEFAULT 0,
            merged_at TEXT NOT NULL
        )
        """
    )
    database._conn.commit()


def merged_shards(database: HistoryDatabase) -> set:
    rows = database._conn.execute("SELECT name FROM ingest_shards").fetchall()
    return {row["name"] for row in rows}


def merge(src: Path, db_path: Path, dry_run: bool = False, reset: bool = False) -> int:
    if not src.exists():
        print("[merge] 找不到分片目录：{}".format(src))
        return 1

    shards = list(iter_shards(src))
    if not shards:
        print("[merge] {} 下没有 .jsonl.gz 分片".format(src))
        return 1

    print("[merge] 分片目录：{}（{} 个分片）".format(src, len(shards)))
    print("[merge] 目标数据库：{}".format(db_path))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    database = HistoryDatabase(db_path)
    try:
        # 合并可能和本地采集进程同时写同一个库，给足等待锁的时间
        database._conn.execute("PRAGMA busy_timeout=30000")
        database._conn.execute("PRAGMA synchronous=NORMAL")
        ensure_ingest_table(database)

        if reset:
            database._conn.execute("DELETE FROM ingest_shards")
            database._conn.commit()
            print("[merge] 已清空合并记录（分片会全部重放，已有采样按 sample_time 去重）")

        done = merged_shards(database)

        new_shards = 0
        new_stations = 0
        new_outlets = 0
        skipped_failed = 0
        for path in shards:
            key = shard_key(src, path)
            if key in done:
                continue

            payload = read_shard(path)
            sample_time = payload["sample_time"]
            if not sample_time:
                print("[merge] 分片缺少 meta.sample_time，跳过：{}".format(key))
                continue

            station_count = 0
            outlet_count = 0
            for record in payload["stations"]:
                if not dry_run:
                    database.record_station_snapshot(
                        station_name=record.get("station_name") or "",
                        station_id=int(record.get("station_id") or 0),
                        outlets=record.get("outlets") or [],
                        sample_time=sample_time,
                    )
                station_count += 1
                outlet_count += len(record.get("outlets") or [])
            skipped_failed += len(payload["failed"])

            if not dry_run:
                database._conn.execute(
                    """
                    INSERT OR REPLACE INTO ingest_shards
                        (name, sample_time, station_count, outlet_count, merged_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (key, sample_time, station_count, outlet_count, iso_utc()),
                )
                database._conn.commit()

            new_shards += 1
            new_stations += station_count
            new_outlets += outlet_count
            print(
                "[merge] {} -> {} 站 / {} 条插座采样{}".format(
                    key, station_count, outlet_count, "（dry-run）" if dry_run else ""
                )
            )

        if new_shards == 0:
            print("[merge] 没有新分片，库已是最新")
        else:
            print(
                "[merge] 完成：新增 {} 个分片 / {} 站次 / {} 条插座采样，跳过 {} 个失败站点记录".format(
                    new_shards, new_stations, new_outlets, skipped_failed
                )
            )

        if not dry_run:
            print("[merge] 库内合计：{}".format(database.totals()))
        return 0
    finally:
        database.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="合并 GitHub Actions 采样分片")
    parser.add_argument(
        "--src",
        default=DEFAULT_SRC,
        help="分片目录，默认当前目录下的 gha_data/",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help="目标 SQLite，默认 charge_history/charge_data/gha_merged.db",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计不写库",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="清空合并记录，把所有分片重新重放一遍",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    src = Path(args.src)
    if not src.is_absolute():
        src = (Path.cwd() / src).resolve()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = (RUNTIME_DIR / db_path).resolve()

    return merge(src, db_path, dry_run=args.dry_run, reset=args.reset)


if __name__ == "__main__":
    sys.exit(main())
