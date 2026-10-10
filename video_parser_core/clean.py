"""解析缓存保留策略：按「保留天数」与「总大小上限」清理。

为什么需要：解析下载的中间文件（m4s/音频/封面）**只增不减** —— 线上实测
2338 个文件 / 11 GB。上游原本的 `CacheCleaner` 是"到点 `shutil.rmtree` 整个目录"，
那样会连**正在下载的文件**和 B站登录二维码一起删掉，所以这里改成保留策略：

  · 双阈值：`max_age_days`（保留天数）与 `max_size_mb`（总大小上限），都可配 0 = 不限制
  · **绝不删最近 `min_keep_seconds`（默认 1 小时）内改动过的文件** —— 可能正在下载/合并/待发送
  · 只删文件、不动目录本身；删不掉的只记进 errors，**绝不抛异常**
    （缓存清理永远不能影响正常解析）
  · 按大小清理时清到阈值的 90%（滞回），避免每轮都刚好卡在阈值上来回抖

纯标准库实现，Linux / Windows（AsterCore）行为一致。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

MIN_KEEP_SECONDS = 3600      # 最近 1 小时动过的文件不删（可能正在用）
SIZE_HYSTERESIS = 0.9        # 按大小清理时清到阈值的 90%
DEFAULT_INTERVAL_HOURS = 6   # 自动清理间隔


def scan_cache(cache_dir: str | Path) -> list[tuple[Path, int, float]]:
    """列出缓存目录里的文件 → [(路径, 字节数, mtime)]（递归；不含目录本身）"""
    out: list[tuple[Path, int, float]] = []
    root = Path(cache_dir)
    if not root.is_dir():
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            if not p.is_file():
                continue
            out.append((p, st.st_size, st.st_mtime))
    return out


def clean_cache(
    cache_dir: str | Path,
    max_age_days: float = 7,
    max_size_mb: float = 2048,
    *,
    min_keep_seconds: float = MIN_KEEP_SECONDS,
    dry_run: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """按「保留天数 + 总大小上限」清理缓存目录，返回统计信息（不抛异常）。

    Args:
        max_age_days: 保留天数；<=0 表示不按时间清
        max_size_mb: 总大小上限（MB）；<=0 表示不按大小清
        min_keep_seconds: 最近这么久内改动过的文件一律不删（保护在用文件）
        dry_run: 只统计不真删
    """
    now = time.time() if now is None else now
    files = scan_cache(cache_dir)
    stat: dict[str, Any] = {
        "scanned": len(files),
        "scanned_mb": round(sum(s for _p, s, _m in files) / 1048576, 2),
        "age_deleted": 0,
        "size_deleted": 0,
        "deleted": 0,
        "freed_bytes": 0,
        "freed_mb": 0.0,
        "skipped_in_use": 0,
        "errors": [],
        "dry_run": bool(dry_run),
    }

    def _unlink(p: Path) -> bool:
        if dry_run:
            return True
        try:
            p.unlink()
            return True
        except OSError as exc:
            stat["errors"].append(f"{p.name}: {exc}")
            return False

    def _in_use(mtime: float) -> bool:
        return (now - mtime) <= min_keep_seconds

    # ① 按时间：超过保留天数的删掉
    keep: list[tuple[Path, int, float]] = []
    cutoff = (now - float(max_age_days) * 86400) if (max_age_days and max_age_days > 0) else None
    for item in files:
        p, size, mtime = item
        if cutoff is not None and mtime < cutoff:
            if _in_use(mtime):                       # 刚动过 → 放过（阈值比 min_keep 还短时）
                stat["skipped_in_use"] += 1
            elif _unlink(p):
                stat["age_deleted"] += 1
                stat["freed_bytes"] += size
                continue
        keep.append(item)

    # ② 按大小：超了就按 mtime 从旧到新删到阈值的 90%
    total = sum(s for _p, s, _m in keep)
    limit = float(max_size_mb) * 1048576 if (max_size_mb and max_size_mb > 0) else 0.0
    if limit and total > limit:
        target = limit * SIZE_HYSTERESIS
        for item in sorted(keep, key=lambda x: x[2]):     # 旧 → 新
            if total <= target:
                break
            p, size, mtime = item
            if _in_use(mtime):
                stat["skipped_in_use"] += 1
                continue
            if _unlink(p):
                stat["size_deleted"] += 1
                stat["freed_bytes"] += size
                total -= size
                keep.remove(item)

    stat["deleted"] = stat["age_deleted"] + stat["size_deleted"]
    stat["freed_mb"] = round(stat["freed_bytes"] / 1048576, 2)
    stat["remaining_count"] = len(keep)
    stat["remaining_bytes"] = sum(s for _p, s, _m in keep)
    stat["remaining_mb"] = round(stat["remaining_bytes"] / 1048576, 2)
    return stat


class CacheCleaner:
    """后台定时清理（守护线程，不依赖 apscheduler / cron）。

    与上游同名类在同一个文件，但语义改成"保留策略"：启动后先清一次，
    之后每 interval_hours 清一次；配置**每次循环重读**，所以面板改完下一轮生效。
    """

    JOBNAME = "CacheCleaner"

    def __init__(self, cache_dir: str | Path, get_config=None,
                 interval_hours: float = DEFAULT_INTERVAL_HOURS,
                 startup_delay: float = 90.0):
        self.cache_dir = Path(cache_dir)
        self.get_config = get_config or (lambda: {})
        self.interval_hours = interval_hours
        self.startup_delay = startup_delay
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_stat: dict[str, Any] | None = None

    def clean_once(self, dry_run: bool = False) -> dict[str, Any]:
        """跑一次清理，返回统计（供日志 / 手动指令复用）"""
        conf = self.get_config() or {}
        stat = clean_cache(
            self.cache_dir,
            max_age_days=float(conf.get("cache_max_age_days", 7) or 0),
            max_size_mb=float(conf.get("cache_max_size_mb", 2048) or 0),
            dry_run=dry_run,
        )
        self.last_stat = stat
        return stat

    def start(self) -> "CacheCleaner":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"{self.JOBNAME}-thread")
        self._thread.start()
        logger.info(f"{self.JOBNAME} 已启动：按「保留天数 + 大小上限」周期清理 "
                    f"{self.cache_dir}")
        return self

    def _loop(self) -> None:
        if self._stop.wait(self.startup_delay):
            return
        while not self._stop.is_set():
            try:
                conf = self.get_config() or {}
                if conf.get("cache_cleanup_enabled", True):
                    st = self.clean_once()
                    if st["deleted"]:
                        logger.info(f"{self.JOBNAME} 清理：删除 {st['deleted']} 个文件 / "
                                    f"释放 {st['freed_mb']} MB；剩余 {st['remaining_count']} 个 / "
                                    f"{st['remaining_mb']} MB")
            except Exception:
                logger.exception(f"{self.JOBNAME} 清理异常（已忽略，不影响解析）")
            hours = float((self.get_config() or {}).get(
                "cache_cleanup_interval_hours", self.interval_hours) or self.interval_hours)
            if self._stop.wait(max(600.0, hours * 3600.0)):
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        logger.info(f"[{self.JOBNAME}] 已停止")
