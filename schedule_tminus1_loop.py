#!/usr/bin/env python3
"""
不依赖「任务计划程序」的定时方式：本进程常驻，每到本地时间指定时刻执行一次
    python run_pipeline.py --t-minus-1

适用：任务计划程序不稳定、或希望用「开机自启 + 后台脚本」代替。

注意：
- 电脑须在触发时刻处于开机且未休眠，否则会顺延到唤醒后再等下一个周期。
- 可配合「电源选项 → 从不休眠」或仅在服务器上跑。

用法：
  python schedule_tminus1_loop.py
  python schedule_tminus1_loop.py --hour 6 --minute 0
  python schedule_tminus1_loop.py --once        # 只跑一次 T-1（等同手动跑链路）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PIPELINE = ROOT / "run_pipeline.py"


def seconds_until_next(hour: int, minute: int) -> float:
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_pipeline_tminus1() -> int:
    return subprocess.call(
        [sys.executable, str(PIPELINE), "--t-minus-1"],
        cwd=str(ROOT),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="按本地时钟定时执行 run_pipeline --t-minus-1")
    parser.add_argument("--hour", type=int, default=6, help="每天触发的小时（0-23），默认 6")
    parser.add_argument("--minute", type=int, default=0, help="分钟，默认 0")
    parser.add_argument(
        "--once",
        action="store_true",
        help="不循环，立即执行一次 T-1 后退出（与直接跑 run_pipeline 等价）",
    )
    args = parser.parse_args()

    if not PIPELINE.is_file():
        print("错误：找不到", PIPELINE, file=sys.stderr)
        sys.exit(1)

    if args.once:
        sys.exit(run_pipeline_tminus1())

    print(
        "schedule_tminus1_loop：将每天在",
        f"{args.hour:02d}:{args.minute:02d}",
        "执行 run_pipeline.py --t-minus-1；Ctrl+C 结束。",
        flush=True,
    )

    while True:
        wait_s = seconds_until_next(args.hour, args.minute)
        next_at = datetime.now() + timedelta(seconds=wait_s)
        print(
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 距下次执行约 {wait_s / 3600:.2f} 小时（约 {next_at:%Y-%m-%d %H:%M:%S}）",
            flush=True,
        )
        time.sleep(wait_s)
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 开始执行 T-1 链路…", flush=True)
        rc = run_pipeline_tminus1()
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 结束，退出码 {rc}", flush=True)
        time.sleep(90)


if __name__ == "__main__":
    main()
