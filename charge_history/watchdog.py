"""采集看门狗 —— 检测采样中断并自动重启 ChargerHistory 计划任务。

设计要点：
- 由计划任务每 5 分钟拉起一次（--once），检查完立即退出。
  刻意不用常驻进程：常驻进程自己死掉就没人管了，而"每 5 分钟拉起一次"
  由 Windows 任务计划程序保证，看门狗永远不会"失联"。
- 判定逻辑：
  1. 最新 sample_time 距今 <= STALE_MIN 分钟  -> 正常，退出
  2. 有一轮正在采（started_at 比最新轮次新，且未超过 MAX_ROUND_MIN）-> 等待，不干预
  3. 其余情况（进程已死 / 单轮卡死）-> 强制结束并重启任务
- 每次检查写 logs/watchdog.log，中断历史可追溯。

用法：
    python watchdog.py --once      单次检查（计划任务用，默认）
    python watchdog.py --loop      常驻循环，每 5 分钟检查一次（手动调试）
    python watchdog.py --report    输出近 24 小时轮次完整性报告
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "charge_data"
LOG_FILE = BASE_DIR / "logs" / "watchdog.log"

TASK_NAME = "ChargerHistory"
SAMPLE_INTERVAL_MIN = 20      # 采集间隔
STALE_MIN = 35                # 超过这个时长没有新轮次，判定为中断（20 + 15 容差）
MAX_ROUND_MIN = 30            # 单轮正常约 4 分钟，超过这个时长仍未完成视为卡死
CHECK_INTERVAL_S = 300        # --loop 模式的检查周期
GAP_MIN = 35                  # 相邻两轮超过这个间隔记为缺口（20 + 15 容差，
                              # 取 25 会把启动时的非整点首轮误报成缺口）

UTC = dt.timezone.utc
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def log(msg: str) -> None:
    line = "[{}] {}".format(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line)
    except Exception:
        pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def latest_db() -> Path | None:
    if not DATA_DIR.is_dir():
        return None
    dbs = [p for p in DATA_DIR.glob("charge_*.db") if p.is_file()]
    return max(dbs, key=lambda p: p.stat().st_mtime) if dbs else None


def read_state(db_path: Path) -> tuple[str | None, str | None, str | None]:
    conn = sqlite3.connect("file:{}?mode=ro".format(db_path.as_posix()), uri=True, timeout=15)
    try:
        row = conn.execute("select max(sample_time) from outlet_samples").fetchone()
        latest = row[0] if row else None
        meta = dict(conn.execute("select key, value from service_meta").fetchall())
        return latest, meta.get("last_collection_started_at"), meta.get("last_collection_finished_at")
    finally:
        conn.close()


def run_schtasks(*args: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["schtasks", *args],
            capture_output=True, text=True, timeout=60,
            encoding="gbk", errors="replace", creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0, (proc.stdout or "").strip() or (proc.stderr or "").strip()
    except Exception as exc:
        return False, str(exc)


def restart_task(reason: str) -> bool:
    log("重启任务：{}".format(reason))
    run_schtasks("/End", "/TN", TASK_NAME)          # 任务未在跑会报错，忽略
    time.sleep(2)
    ok, out = run_schtasks("/Run", "/TN", TASK_NAME)
    if ok:
        log("已提交重启")
        return True

    log("重启失败（{}），尝试先启用任务".format(out))
    run_schtasks("/Change", "/TN", TASK_NAME, "/ENABLE")
    time.sleep(1)
    ok, out = run_schtasks("/Run", "/TN", TASK_NAME)
    log("启用后重启{}".format("成功" if ok else "仍失败：{}".format(out)))
    return ok


def check_once() -> int:
    """返回 0 = 正常/已处理，1 = 需要人工介入。"""
    db_path = latest_db()
    if db_path is None:
        log("找不到采样库，尝试重启任务")
        return 0 if restart_task("charge_data 下没有数据库文件") else 1

    try:
        latest, started, finished = read_state(db_path)
    except sqlite3.Error as exc:
        log("读库失败（{}），本轮跳过".format(exc))
        return 0

    now = utc_now()
    latest_dt = parse_ts(latest)
    if latest_dt is None:
        log("库中无采样数据，尝试重启任务")
        return 0 if restart_task("outlet_samples 为空") else 1

    age_min = (now - latest_dt).total_seconds() / 60
    if age_min <= STALE_MIN:
        return 0

    started_dt = parse_ts(started)
    running_min = (now - started_dt).total_seconds() / 60 if started_dt else None

    if started_dt and started_dt > latest_dt and running_min is not None and running_min < MAX_ROUND_MIN:
        log("最新轮次 {:.0f} 分钟前，当前轮已运行 {:.0f} 分钟，判定为采集中，等待".format(age_min, running_min))
        return 0

    if running_min is not None and running_min >= MAX_ROUND_MIN:
        reason = "单轮已运行 {:.0f} 分钟仍未完成（最新轮次 {:.0f} 分钟前），判定卡死".format(running_min, age_min)
    else:
        reason = "最新轮次已是 {:.0f} 分钟前，超过 {} 分钟阈值，进程疑似已退出".format(age_min, STALE_MIN)

    return 0 if restart_task(reason) else 1


def report(hours: int = 24) -> None:
    db_path = latest_db()
    if db_path is None:
        print("找不到采样库")
        return
    conn = sqlite3.connect("file:{}?mode=ro".format(db_path.as_posix()), uri=True, timeout=15)
    since = (utc_now() - dt.timedelta(hours=hours)).isoformat()
    times = [r[0] for r in conn.execute(
        "select distinct sample_time from outlet_samples where sample_time >= ? order by sample_time",
        (since,),
    )]
    conn.close()

    if not times:
        print("近 {} 小时无采样".format(hours))
        return

    parsed = [p for p in (parse_ts(t) for t in times) if p is not None]
    now = utc_now()

    # 末尾加一个"现在"作为哨兵，否则最新一轮之后的空档检测不出来
    gaps = []
    for prev, cur in zip(parsed, parsed[1:] + [now]):
        delta = (cur - prev).total_seconds() / 60
        if delta > GAP_MIN:
            gaps.append((prev, cur, delta))

    span_min = (now - parsed[0]).total_seconds() / 60
    expected = int(span_min / SAMPLE_INTERVAL_MIN) + 1

    print("库：{}".format(db_path.name))
    print("近 {} 小时：按 {} 分钟间隔应有约 {} 轮，实有 {} 轮，完整度 {:.1f}%".format(
        hours, SAMPLE_INTERVAL_MIN, expected, len(times), len(times) / expected * 100))
    if gaps:
        total_missing = 0
        print("缺口 {} 处：".format(len(gaps)))
        for prev, cur, delta in gaps:
            missing = max(1, int(round(delta / SAMPLE_INTERVAL_MIN)) - 1)
            total_missing += missing
            tail = "（至今仍未恢复）" if cur == now else ""
            print("  {} -> {}  约缺 {} 轮（间隔 {:.0f} 分钟）{}".format(
                prev.astimezone().strftime("%m-%d %H:%M"),
                cur.astimezone().strftime("%m-%d %H:%M"),
                missing, delta, tail))
        print("合计约缺 {} 轮".format(total_missing))
    else:
        print("无缺口")


def main() -> int:
    ap = argparse.ArgumentParser(description="充电采集看门狗")
    ap.add_argument("--once", action="store_true", help="单次检查（默认）")
    ap.add_argument("--loop", action="store_true", help="常驻循环检查")
    ap.add_argument("--report", action="store_true", help="输出完整性报告")
    ap.add_argument("--hours", type=int, default=24, help="报告回看小时数")
    args = ap.parse_args()

    if args.report:
        report(args.hours)
        return 0

    if args.loop:
        log("看门狗启动，每 {} 秒检查一次".format(CHECK_INTERVAL_S))
        while True:
            try:
                check_once()
            except Exception as exc:
                log("检查异常：{}".format(exc))
            time.sleep(CHECK_INTERVAL_S)

    return check_once()


if __name__ == "__main__":
    sys.exit(main())
