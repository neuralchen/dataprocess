#!/usr/bin/env python3
"""下载与处理的自动流水线：边下边切。

监视下载目录，文件一旦下载完成且稳定，立即送进镜头拆分流程，
不必等整批下完。处理过的记录在状态文件里，重跑不会重复劳动。

仅用于你拥有相应权利的内容。

用法:
    # 一体化：下载 + 自动处理
    python3 pipeline.py --links links.txt --download-dir /mnt/hd2/downloads \\
                        --output /mnt/hd2/clips -P amule口令

    # 只监视已有目录（下载由别的方式完成）
    python3 pipeline.py --watch /mnt/hd2/downloads --output /mnt/hd2/clips

    # 处理参数透传给 split_shots.py
    python3 pipeline.py --watch ./dl --output ./clips \\
                        --split-args "--encoder nvenc --cq 23 --jobs 6 --min-clip 15"
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm",
              ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".rmvb", ".rm", ".vob"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class StateStore:
    """记录每个文件的处理状态，支持中断续跑。"""

    def __init__(self, path: Path):
        self.path = path
        self.data = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        self._lock = threading.Lock()

    def get(self, key):
        return self.data.get(key, {})

    def set(self, key, **kw):
        with self._lock:
            self.data.setdefault(key, {}).update(kw)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)


def is_stable(path: Path, wait=20):
    """确认文件不再增长——下载中的文件大小会持续变化，直接处理会拿到半截数据。"""
    try:
        s1 = path.stat().st_size
        if s1 == 0:
            return False
        time.sleep(wait)
        return path.stat().st_size == s1
    except OSError:
        return False


def probe_ok(path: Path):
    """确认是能解码的视频，排除下载不完整或根本不是视频的文件。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def process_one(video: Path, args, state: StateStore):
    """对单个视频跑镜头拆分。"""
    key = str(video)
    split_args = args.split_args.split() if args.split_args else []
    # 每个视频单独建输入目录，让 split_shots 的目录结构逻辑保持一致。
    # 必须放在输出目录之外——split_shots 会主动排除位于输出目录下的视频，
    # 否则这里的软链接会被当成产出而被跳过，一个都扫不到。
    stage = Path(args.staging).expanduser() / video.stem
    stage.mkdir(parents=True, exist_ok=True)
    link = stage / video.name
    if not link.exists():
        try:
            link.symlink_to(video)
        except OSError:
            link = video
            stage = video.parent

    cmd = [sys.executable, str(HERE / "split_shots.py"),
           str(stage), str(args.output), "--skip-processed", "--skip-existing"] + split_args
    log(f"开始处理 {video.name[:60]}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    ok = r.returncode == 0
    tail = (r.stdout or r.stderr or "").strip().splitlines()
    detail = tail[-1][:120] if tail else ""
    state.set(key, processed=ok, at=time.strftime("%F %T"), detail=detail)
    log(("完成 " if ok else "失败 ") + video.name[:60] + (f" — {detail}" if detail else ""))
    # 清理软链接暂存目录
    try:
        if link.is_symlink():
            link.unlink()
            stage.rmdir()
    except OSError:
        pass
    return ok


def watch_loop(args, state: StateStore, stop_evt):
    """轮询下载目录，把稳定且可解码的新视频送去处理。"""
    watch = Path(args.watch or args.download_dir).expanduser()
    watch.mkdir(parents=True, exist_ok=True)
    pending = {}
    while not stop_evt.is_set():
        try:
            for f in sorted(watch.rglob("*")):
                if not f.is_file() or ".staging" in f.parts:
                    continue
                if f.suffix.lower() in ARCHIVE_EXTS:
                    if not state.get(str(f)).get("noted_archive"):
                        log(f"跳过压缩包（需先解压）: {f.name[:60]}")
                        state.set(str(f), noted_archive=True)
                    continue
                if f.suffix.lower() not in VIDEO_EXTS:
                    continue
                st = state.get(str(f))
                if st.get("processed"):
                    continue
                # aMule 下载中的临时文件通常带 .part
                if f.name.endswith(".part"):
                    continue
                size = f.stat().st_size
                prev = pending.get(str(f))
                if prev != size:
                    pending[str(f)] = size      # 还在变化，下一轮再看
                    continue
                if not is_stable(f, wait=args.stable_wait):
                    continue
                if not probe_ok(f):
                    if not st.get("noted_bad"):
                        log(f"跳过：无法解码（可能未下完）{f.name[:60]}")
                        state.set(str(f), noted_bad=True)
                    continue
                process_one(f, args, state)
                pending.pop(str(f), None)
        except Exception as e:
            log(f"监视循环异常: {e}")
        stop_evt.wait(args.interval)


def main():
    ap = argparse.ArgumentParser(
        description="下载与镜头拆分的自动流水线",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--links", help="ed2k 清单文件，给出则同时启动下载")
    ap.add_argument("--download-dir", default="./downloads", help="下载目标目录")
    ap.add_argument("--watch", help="只监视这个目录（不启动下载）")
    ap.add_argument("-o", "--output", required=True, help="拆分片段的输出目录")
    ap.add_argument("--split-args", default="--encoder auto --cq 23 --min-clip 15",
                    help="透传给 split_shots.py 的参数")
    ap.add_argument("--interval", type=int, default=60, help="扫描间隔（秒）")
    ap.add_argument("--stable-wait", type=int, default=20,
                    help="判定文件写入完成的静默时长（秒）")
    ap.add_argument("-P", "--password", default="", help="amuled 远程口令")
    ap.add_argument("--staging", default="", help="暂存目录，留空则用系统临时目录")
    ap.add_argument("--state", default=".pipeline_state.json", help="状态记录文件")
    args = ap.parse_args()

    if not args.links and not args.watch:
        sys.exit("请指定 --links（下载+处理）或 --watch（只处理已有目录）")

    out = Path(args.output).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    if not args.staging:
        import tempfile
        args.staging = str(Path(tempfile.gettempdir()) / "pipeline_staging")
    Path(args.staging).expanduser().mkdir(parents=True, exist_ok=True)
    state = StateStore(out / args.state)

    stop_evt = threading.Event()
    watcher = threading.Thread(target=watch_loop, args=(args, state, stop_evt),
                               daemon=True)
    watcher.start()
    log(f"已开始监视 {args.watch or args.download_dir} -> 输出 {out}")

    dl = None
    if args.links:
        cmd = [sys.executable, str(HERE / "ed2k_downloader.py"), args.links,
               "-o", args.download_dir]
        if args.password:
            cmd += ["-P", args.password]
        log("启动下载 ...")
        dl = subprocess.Popen(cmd)

    try:
        if dl:
            dl.wait()
            log("下载进程结束，继续处理剩余文件 ...")
            time.sleep(args.interval * 2)
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        log("收到中断，正在停止 ...")
    finally:
        stop_evt.set()
        if dl and dl.poll() is None:
            dl.terminate()
        done = sum(1 for v in state.data.values() if v.get("processed"))
        log(f"已处理 {done} 个文件，状态记录在 {out / args.state}")


if __name__ == "__main__":
    main()
