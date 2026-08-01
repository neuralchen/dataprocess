#!/usr/bin/env python3
"""
扫描文件夹中的视频（支持多种格式），检测镜头(shot)边界并拆分成片段，
去掉片头（封面）和片尾片段，按原视频文件名建立文件夹保存拆分结果。

依赖:
    pip install scenedetect[opencv]
    系统需安装 ffmpeg / ffprobe

用法:
    python split_shots.py <输入文件夹> <输出文件夹> [选项]

示例:
    python split_shots.py ./videos ./output                     # 默认：重编码 25fps, CRF 16（接近原质量）
    python split_shots.py ./videos ./output --crf 18 --fps 30   # 调整质量与帧率
    python split_shots.py ./videos ./output --copy              # 流复制，不改帧率，速度最快
    python split_shots.py ./videos ./output --skip-head 1 --skip-tail 1

说明:
    只检测到一个镜头的视频不做拆分和去头尾，直接整段按目标质量/帧率转码输出。
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import cv2
    import numpy as np
    from scenedetect import open_video, SceneManager
    from scenedetect.detectors import AdaptiveDetector, ContentDetector, HistogramDetector
except ImportError:
    sys.exit("缺少依赖 scenedetect，请先执行: pip install 'scenedetect[opencv]'")

# 支持的视频扩展名
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".rmvb", ".rm", ".vob",
}


def find_videos(folder: Path) -> list[Path]:
    """递归扫描文件夹（包括所有子目录）中所有支持格式的视频文件。"""
    return sorted(p for p in folder.rglob("*")
                  if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def file_fingerprint(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    """计算文件内容指纹：大小 + 头/中/尾三段 SHA-256。
    对视频这类大文件比全文件哈希快得多，且几乎不会误判。"""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        if size <= 3 * chunk:
            # 小文件直接全量哈希
            for block in iter(lambda: f.read(chunk), b""):
                h.update(block)
        else:
            for offset in (0, (size - chunk) // 2, size - chunk):
                f.seek(offset)
                h.update(f.read(chunk))
    return h.hexdigest()


def dedupe_videos(videos: list[Path], in_dir: Path) -> list[Path]:
    """去掉内容重复的视频（只跳过处理，不删除文件）。保留每组中路径排序最靠前的一个。"""
    # 先按文件大小分组（零成本），大小唯一的必然不重复，无需计算哈希
    by_size = defaultdict(list)
    for v in videos:
        by_size[v.stat().st_size].append(v)

    kept, dup_count = [], 0
    for group in by_size.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        by_fp = defaultdict(list)
        for v in group:
            by_fp[file_fingerprint(v)].append(v)
        for same in by_fp.values():
            kept.append(same[0])
            for dup in same[1:]:
                dup_count += 1
                print(f"  [重复] {dup.relative_to(in_dir)}  ->  与 {same[0].relative_to(in_dir)} 内容相同，跳过")
    if dup_count:
        print(f"共发现 {dup_count} 个重复视频，将只处理去重后的 {len(kept)} 个")
    return sorted(kept)


def merge_short_scenes(shots, min_len_sec: float):
    """把短于 min_len_sec 的碎片段并入前一个镜头。
    多个检测器在同一次过渡附近先后触发时会产生零点几秒的碎片，这里统一收拢。"""
    merged = []
    for start, end in shots:
        if merged and (end - start) < min_len_sec:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    # 开头若剩下一个过短的段（没有前段可并），并入下一段
    if len(merged) >= 2 and (merged[0][1] - merged[0][0]) < min_len_sec:
        merged[1] = (merged[0][0], merged[1][1])
        merged.pop(0)
    return merged


# ---------- 后台运行机制 ----------

def pid_file_path(out_root: Path) -> Path:
    return out_root / ".split_shots.pid"


def read_pid(out_root: Path):
    try:
        return int(pid_file_path(out_root).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_running(pid: int) -> bool:
    """检查进程是否存活（跨平台）。"""
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def launch_background(out_root: Path) -> None:
    """把当前命令（去掉 --background）作为独立后台进程重新启动，日志写入文件。"""
    pid = read_pid(out_root)
    if pid and is_running(pid):
        sys.exit(f"已有后台任务在运行 (PID {pid})，请先 --stop 或等待其完成")
    out_root.mkdir(parents=True, exist_ok=True)
    log_path = out_root / f"split_shots_{datetime.now():%Y%m%d_%H%M%S}.log"
    argv = [a for a in sys.argv[1:] if a != "--background"]
    cmd = [sys.executable, "-u", str(Path(__file__).resolve())] + argv
    with open(log_path, "ab") as lf:
        if os.name == "nt":
            flags = (subprocess.DETACHED_PROCESS |
                     subprocess.CREATE_NEW_PROCESS_GROUP |
                     subprocess.CREATE_NO_WINDOW)
            proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=flags)
        else:
            proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
    pid_file_path(out_root).write_text(str(proc.pid))
    print(f"已转入后台运行 (PID {proc.pid})")
    print(f"日志: {log_path}")
    print(f"查看进度: python {Path(__file__).name} <输入目录> {out_root} --status")
    print(f"停止任务: python {Path(__file__).name} <输入目录> {out_root} --stop")


def show_status(out_root: Path) -> None:
    pid = read_pid(out_root)
    if pid and is_running(pid):
        print(f"后台任务运行中 (PID {pid})")
    else:
        print("没有正在运行的后台任务")
    logs = sorted(out_root.glob("split_shots_*.log"))
    if logs:
        print(f"\n最新日志 {logs[-1].name} 末尾:")
        lines = logs[-1].read_text(errors="replace").splitlines()
        for line in lines[-15:]:
            print(f"  {line}")


def stop_background(out_root: Path) -> None:
    pid = read_pid(out_root)
    if not pid or not is_running(pid):
        print("没有正在运行的后台任务")
        return
    os.kill(pid, signal.SIGTERM)
    print(f"已停止后台任务 (PID {pid})。用 --skip-existing 重跑可从断点继续")
    pid_file_path(out_root).unlink(missing_ok=True)


# ---------- 视频处理 ----------

def video_duration(path: Path) -> float:
    """用 ffprobe 读取视频时长（秒）。"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def measure_motion(video_path: Path, start: float, end: float, samples: int = 6) -> float:
    """估算镜头内的运动量：等间隔抽帧，计算相邻抽样帧的平均像素差（0-255 尺度）。
    logo/片头/片尾画面通常接近静止，运动量趋近 0；正常内容镜头明显更高。"""
    cap = cv2.VideoCapture(str(video_path))
    times = np.linspace(start, end, samples + 2)[1:-1]  # 取镜头内部的采样点
    prev, diffs = None, []
    for t in times:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ret, frame = cap.read()
        if not ret:
            continue
        gray = cv2.cvtColor(cv2.resize(frame, (96, 96)), cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            diffs.append(float(np.abs(gray - prev).mean()))
        prev = gray
    cap.release()
    return float(np.mean(diffs)) if diffs else 0.0


def auto_trim_static(video_path: Path, shots, kept_idx, motion_thr: float, window_sec: float):
    """去掉开头/结尾窗口内接近静止的镜头（残留的 logo、片名、结尾定帧等）。
    从头部逐个检查：镜头起点在窗口内且运动量低于阈值则丢弃，遇到动态镜头即停；尾部同理。
    操作的是场景索引列表，返回 (剩余索引, 头部去掉数, 尾部去掉数)。"""
    head_dropped = 0
    origin = shots[kept_idx[0]][0] if kept_idx else 0.0
    while len(kept_idx) > 1 and shots[kept_idx[0]][0] - origin < window_sec and \
            measure_motion(video_path, *shots[kept_idx[0]]) < motion_thr:
        kept_idx.pop(0)
        head_dropped += 1
    tail_dropped = 0
    tail_end = shots[kept_idx[-1]][1] if kept_idx else 0.0
    while len(kept_idx) > 1 and tail_end - shots[kept_idx[-1]][1] < window_sec and \
            measure_motion(video_path, *shots[kept_idx[-1]]) < motion_thr:
        kept_idx.pop()
        tail_dropped += 1
    return kept_idx, head_dropped, tail_dropped


def write_scene_json(out_dir: Path, video: Path, shots, outputs, args) -> None:
    """在视频的输出文件夹下写 scene.json，记录全部检测场景与已导出片段的路径。
    outputs: {scene_index: 输出文件路径}，未导出的场景 output_path 为 null。"""
    try:
        source_size = video.stat().st_size
    except OSError:
        source_size = None
    data = {
        "source_video": str(video),
        "source_size": source_size,
        "source_fingerprint": file_fingerprint(video),
        "detector": "content+adaptive+histogram",
        "aggressive": args.aggressive,
        "threshold": args.threshold,
        "min_scene_len": args.min_scene_len,
        "min_scene_seconds": args.min_clip,
        "split_mode": "copy" if args.copy else "encode",
        "fps": args.fps,
        "crf": args.crf,
        "preset": args.preset,
        "skip_head": args.skip_head,
        "skip_tail": args.skip_tail,
        "detected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scene_count": len(shots),
        "scenes": [
            {
                "scene_index": i,
                "start_time": round(start, 3),
                "end_time": round(end, 3),
                "duration": round(end - start, 3),
                "output_path": str(outputs[i]) if i in outputs else None,
                "thumbnail": None,
            }
            for i, (start, end) in enumerate(shots)
        ],
    }
    (out_dir / "scene.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_processed_index(dirs) -> dict:
    """扫描一个或多个结果目录中的 scene.json，建立"已处理视频"索引。
    scene.json 只在片段导出完成后写入，因此它的存在即代表该视频已处理完毕。"""
    index = {"paths": set(), "fingerprints": set(), "name_size": set(),
             "names": set(), "count": 0}
    for d in dirs:
        root = Path(d).expanduser().resolve()
        if not root.is_dir():
            print(f"  [警告] 结果目录不存在，已忽略: {root}")
            continue
        found = 0
        for sj in root.rglob("scene.json"):
            try:
                data = json.loads(sj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                print(f"  [警告] 无法解析，已忽略: {sj}")
                continue
            src = data.get("source_video")
            if not src:
                continue
            name = Path(src).name
            index["paths"].add(str(Path(src)))
            index["names"].add(name)
            if data.get("source_fingerprint"):
                index["fingerprints"].add(data["source_fingerprint"])
            if data.get("source_size"):
                index["name_size"].add((name, int(data["source_size"])))
            index["count"] += 1
            found += 1
        print(f"  {root}: {found} 条已处理记录")
    return index


def match_processed(video: Path, index: dict, match_by_name: bool):
    """判断视频是否已被处理过，返回匹配依据（未匹配则返回 None）。
    按代价从低到高依次尝试，指纹匹配放最后（需要读取文件内容）。"""
    if str(video) in index["paths"]:
        return "路径匹配"
    if index["name_size"]:
        try:
            if (video.name, video.stat().st_size) in index["name_size"]:
                return "文件名+大小匹配"
        except OSError:
            pass
    if match_by_name and video.name in index["names"]:
        return "文件名匹配"
    if index["fingerprints"] and file_fingerprint(video) in index["fingerprints"]:
        return "内容指纹匹配"
    return None


def filter_processed(videos, in_dir: Path, scan_dirs, match_by_name: bool, verbose: bool):
    """从待处理列表中剔除已处理过的视频，返回 (剩余视频, 已处理视频及其匹配依据)。"""
    print("正在扫描已处理结果 ...")
    index = load_processed_index(scan_dirs)
    print(f"共载入 {index['count']} 条已处理记录")
    if not index["count"]:
        return videos, []
    remaining, done = [], []
    for v in videos:
        reason = match_processed(v, index, match_by_name)
        if reason:
            done.append((v, reason))
            if verbose:
                print(f"  [已处理] {v.relative_to(in_dir)}  ({reason})")
        else:
            remaining.append(v)
    return remaining, done


def detect_shots(video_path: Path, threshold: float, min_scene_len_sec: float,
                 aggressive: bool = False):
    """用 PySceneDetect 检测镜头边界，返回 [(start_sec, end_sec), ...]。

    三个检测器并用，切点取并集（防止不同镜头被合并成一个）：
      - ContentDetector: 抓画面内容突变（硬切）
      - AdaptiveDetector: 对比相邻帧的相对变化，抓运动/摇镜头中的切换
      - HistogramDetector: 抓亮度直方图变化，对溶解/叠化等渐变过渡敏感
    aggressive=True 时各检测器灵敏度大幅调高（切得更碎，但几乎不漏切）。
    """
    video = open_video(str(video_path))
    fps = video.frame_rate
    min_len = max(1, int(min_scene_len_sec * fps))
    if aggressive:
        adaptive_kwargs = dict(adaptive_threshold=1.5, min_content_val=6.0)
        hist_threshold = 0.05
    else:
        adaptive_kwargs = dict(adaptive_threshold=2.5, min_content_val=12.0)
        hist_threshold = 0.08
    scene_manager = SceneManager()
    scene_manager.add_detector(
        ContentDetector(threshold=threshold, min_scene_len=min_len)
    )
    scene_manager.add_detector(
        AdaptiveDetector(min_scene_len=min_len, **adaptive_kwargs)
    )
    scene_manager.add_detector(
        HistogramDetector(threshold=hist_threshold, min_scene_len=min_len)
    )
    scene_manager.detect_scenes(video, show_progress=sys.stderr.isatty())
    scene_list = scene_manager.get_scene_list()
    shots = [(start.get_seconds(), end.get_seconds()) for start, end in scene_list]
    return merge_short_scenes(shots, min_scene_len_sec)


def cut_segment(src: Path, dst: Path, start: float, end: float, args) -> bool:
    """用 ffmpeg 切出 [start, end) 片段。返回是否成功。"""
    duration = end - start
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}"]
    if args.copy:
        # 流复制：速度快，不改帧率，切点会对齐到关键帧，可能有少许偏差
        cmd += ["-c", "copy"]
    else:
        # 重编码：切点精确到帧，统一输出帧率，CRF 控制质量
        cmd += [
            "-r", str(args.fps),
            "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
        ]
    cmd += ["-avoid_negative_ts", "make_zero", str(dst)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [错误] ffmpeg 切割失败: {dst.name}\n{result.stderr.strip()}")
        return False
    return True


def convert_full(src: Path, dst: Path, args) -> bool:
    """不切割，整段按目标质量/帧率转码（--copy 模式下为直接复制流）。"""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if args.copy:
        cmd += ["-c", "copy"]
    else:
        cmd += [
            "-r", str(args.fps),
            "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
        ]
    cmd.append(str(dst))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [错误] ffmpeg 转码失败: {dst.name}\n{result.stderr.strip()}")
        return False
    return True


def process_video(video: Path, in_dir: Path, out_root: Path, args) -> None:
    rel = video.relative_to(in_dir)
    print(f"=== 处理: {rel} ===")
    shots = detect_shots(video, args.threshold, args.min_scene_len, args.aggressive)
    total = len(shots)
    print(f"  检测到 {total} 个镜头")

    ext = video.suffix if args.copy else ".mp4"

    # 只有一个镜头（或未检测到边界）：不拆分、不去头尾，整段转成目标质量/帧率
    if total <= 1:
        duration = video_duration(video)
        out_dir = out_root / rel.parent / video.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        if duration < args.min_clip:
            print(f"  只有一个镜头且时长 {duration:.1f}s 不足 {args.min_clip:g}s，跳过")
            write_scene_json(out_dir, video, [(0.0, duration)], {}, args)
            return
        dst = out_dir / f"{video.stem}_scene_0000{ext}"
        whole = [(0.0, duration)]
        if args.skip_existing and dst.exists():
            print("  只有一个镜头，输出已存在，跳过")
            write_scene_json(out_dir, video, whole, {0: dst}, args)
            return
        print("  只有一个镜头，整段转码输出")
        if convert_full(video, dst, args):
            print(f"  完成: 已保存到 {dst}")
            write_scene_json(out_dir, video, whole, {0: dst}, args)
        else:
            write_scene_json(out_dir, video, whole, {}, args)
        return

    # 维持输入文件夹的路径结构，再按原视频名建立输出文件夹
    out_dir = out_root / rel.parent / video.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # 去掉片头（封面）和片尾片段（基于场景索引操作，便于在 scene.json 中溯源）
    kept_idx = list(range(total))[args.skip_head: total - args.skip_tail if args.skip_tail else None]
    if not kept_idx:
        print(f"  去掉片头 {args.skip_head} 个、片尾 {args.skip_tail} 个后没有剩余片段，跳过")
        write_scene_json(out_dir, video, shots, {}, args)
        return
    print(f"  去掉片头 {args.skip_head} 个、片尾 {args.skip_tail} 个", end="")

    # 自动去除残留的静态 logo/片名/定帧镜头
    if not args.no_auto_trim:
        kept_idx, head_n, tail_n = auto_trim_static(
            video, shots, kept_idx, args.motion_threshold, args.trim_window)
        if head_n or tail_n:
            print(f"，再自动去除静态镜头（片头 {head_n} 个、片尾 {tail_n} 个）", end="")

    # 丢弃过短的片段
    long_enough = [i for i in kept_idx if shots[i][1] - shots[i][0] >= args.min_clip]
    if len(long_enough) < len(kept_idx):
        print(f"，丢弃短于 {args.min_clip:g}s 的片段 {len(kept_idx) - len(long_enough)} 个", end="")
    kept_idx = long_enough
    print(f"，保留 {len(kept_idx)} 个片段")
    if not kept_idx:
        write_scene_json(out_dir, video, shots, {}, args)
        return

    ok = 0
    outputs = {}
    for n, idx in enumerate(kept_idx, 1):
        start, end = shots[idx]
        dst = out_dir / f"{video.stem}_scene_{idx:04d}{ext}"
        if args.skip_existing and dst.exists():
            outputs[idx] = dst
            ok += 1
            continue
        if cut_segment(video, dst, start, end, args):
            outputs[idx] = dst
            ok += 1
            print(f"    [{n}/{len(kept_idx)}] {dst.name}  ({start:.2f}s - {end:.2f}s)")
    write_scene_json(out_dir, video, shots, outputs, args)
    print(f"  完成: {ok}/{len(kept_idx)} 个片段已保存到 {out_dir}，场景记录已写入 scene.json")


def main():
    parser = argparse.ArgumentParser(
        description="扫描文件夹中的视频，按镜头拆分并去掉片头/片尾片段",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", help="包含视频的输入文件夹")
    parser.add_argument("output", help="输出根目录（必填，拆分的片段按输入目录结构存放于此）")
    parser.add_argument("--skip-head", type=int, default=1,
                        help="去掉开头的镜头数（封面/片头）")
    parser.add_argument("--skip-tail", type=int, default=1,
                        help="去掉结尾的镜头数（片尾）")
    parser.add_argument("--threshold", type=float, default=24.0,
                        help="ContentDetector 阈值，越小越敏感（切得越碎）；PySceneDetect 常规默认为 27")
    parser.add_argument("--min-scene-len", type=float, default=0.6,
                        help="最短镜头时长（秒），短于此的段会并入相邻镜头")
    parser.add_argument("--aggressive", action="store_true",
                        help="激进检测模式：各检测器灵敏度大幅调高，几乎不漏切但切得更碎")
    parser.add_argument("--min-clip", type=float, default=15.0,
                        help="输出片段的最短时长（秒），短于此的片段直接丢弃；"
                             "单镜头视频总时长不足时也会跳过")
    parser.add_argument("--motion-threshold", type=float, default=1.0,
                        help="静态镜头判定的运动量阈值（0-255 像素差尺度），低于此视为静态 logo/定帧")
    parser.add_argument("--trim-window", type=float, default=15.0,
                        help="自动静态修剪只作用于开头/结尾各这么多秒内的镜头")
    parser.add_argument("--no-auto-trim", action="store_true",
                        help="关闭对残留静态 logo/片名/定帧镜头的自动修剪")
    parser.add_argument("--background", action="store_true",
                        help="转入后台运行，日志写入输出目录下的 split_shots_*.log")
    parser.add_argument("--status", action="store_true",
                        help="查看后台任务状态和最新日志")
    parser.add_argument("--stop", action="store_true",
                        help="停止正在运行的后台任务")
    parser.add_argument("--fps", type=float, default=25,
                        help="输出帧率")
    parser.add_argument("--crf", type=int, default=16,
                        help="编码质量 (x264 CRF)，越小质量越高；16≈视觉无损，18 很高，23 中等")
    parser.add_argument("--preset", default="slow",
                        choices=["ultrafast", "superfast", "veryfast", "faster", "fast",
                                 "medium", "slow", "slower", "veryslow"],
                        help="x264 编码速度预设，越慢压缩效率越高（同 CRF 下文件更小）")
    parser.add_argument("--copy", action="store_true",
                        help="流复制模式：不重编码、不改帧率，速度极快但切点对齐关键帧")
    parser.add_argument("--skip-processed", action="store_true",
                        help="跳过已处理过的视频（扫描输出目录中的 scene.json 判断）")
    parser.add_argument("--processed-dir", nargs="+", metavar="DIR",
                        help="额外扫描这些历史结果目录（可指定多个），隐含开启 --skip-processed")
    parser.add_argument("--match-by-name", action="store_true",
                        help="已处理判定放宽到仅文件名匹配（默认用路径、文件名+大小、内容指纹匹配）")
    parser.add_argument("--scan-only", action="store_true",
                        help="只扫描并报告哪些视频已处理/待处理，不做任何转码")
    parser.add_argument("--no-dedup", action="store_true",
                        help="不做重复视频检测（默认会按内容指纹去重，重复的只处理一次）")
    parser.add_argument("--skip-existing", action="store_true",
                        help="跳过已存在的输出片段（断点续跑）")
    args = parser.parse_args()

    out_root = Path(args.output).expanduser().resolve()
    if args.status:
        show_status(out_root)
        return
    if args.stop:
        stop_background(out_root)
        return

    in_dir = Path(args.input).expanduser().resolve()
    if not in_dir.is_dir():
        sys.exit(f"输入文件夹不存在: {in_dir}")

    if args.background:
        launch_background(out_root)
        return

    videos = find_videos(in_dir)
    # 避免把输出目录里的片段再当作输入
    videos = [v for v in videos if out_root not in v.parents]
    if not videos:
        sys.exit(f"在 {in_dir} 中没有找到视频文件（支持: {', '.join(sorted(VIDEO_EXTS))}）")

    print(f"找到 {len(videos)} 个视频")

    # 扫描历史结果，跳过已处理过的视频
    done = []
    if args.skip_processed or args.processed_dir or args.scan_only:
        # 输出目录首次运行时不存在属正常情况，不计入扫描（避免误报警告）
        scan_dirs = ([str(out_root)] if out_root.is_dir() else []) + list(args.processed_dir or [])
        videos, done = filter_processed(
            videos, in_dir, scan_dirs, args.match_by_name, args.scan_only)
        print(f"已处理 {len(done)} 个（跳过），剩余 {len(videos)} 个待处理")

    if args.scan_only:
        if videos:
            print("\n待处理视频:")
            for v in videos:
                print(f"  {v.relative_to(in_dir)}")
        return

    if not videos:
        print("没有需要处理的视频。")
        return

    if not args.no_dedup:
        print("正在扫描重复视频 ...")
        videos = dedupe_videos(videos, in_dir)
    print(f"待处理 {len(videos)} 个视频，输出目录: {out_root}")

    out_root.mkdir(parents=True, exist_ok=True)
    pid_file_path(out_root).write_text(str(os.getpid()))
    try:
        for i, v in enumerate(videos, 1):
            try:
                print(f"\n[{i}/{len(videos)}] ", end="")
                process_video(v, in_dir, out_root, args)
            except Exception as e:
                print(f"  [错误] 处理 {v.name} 失败: {e}")
        print("\n全部完成。")
    finally:
        pid_file_path(out_root).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
