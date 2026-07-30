#!/usr/bin/env python3
"""
扫描文件夹中的视频（支持多种格式），检测镜头(shot)边界并拆分成片段，
去掉片头（封面）和片尾片段，按原视频文件名建立文件夹保存拆分结果。

依赖:
    pip install scenedetect[opencv]
    系统需安装 ffmpeg / ffprobe

用法:
    python split_shots.py <输入文件夹> [-o 输出文件夹] [选项]

示例:
    python split_shots.py ./videos                     # 默认：重编码 25fps, CRF 16（接近原质量）
    python split_shots.py ./videos --crf 18 --fps 30   # 调整质量与帧率
    python split_shots.py ./videos --copy              # 流复制，不改帧率，速度最快
    python split_shots.py ./videos -o ./output --skip-head 1 --skip-tail 1
"""

import argparse
import hashlib
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

try:
    from scenedetect import open_video, SceneManager
    from scenedetect.detectors import ContentDetector
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


def detect_shots(video_path: Path, threshold: float, min_scene_len_sec: float):
    """用 PySceneDetect 检测镜头边界，返回 [(start_sec, end_sec), ...]。"""
    video = open_video(str(video_path))
    fps = video.frame_rate
    scene_manager = SceneManager()
    scene_manager.add_detector(
        ContentDetector(
            threshold=threshold,
            min_scene_len=max(1, int(min_scene_len_sec * fps)),
        )
    )
    scene_manager.detect_scenes(video, show_progress=True)
    scene_list = scene_manager.get_scene_list()
    return [(start.get_seconds(), end.get_seconds()) for start, end in scene_list]


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


def process_video(video: Path, in_dir: Path, out_root: Path, args) -> None:
    rel = video.relative_to(in_dir)
    print(f"\n=== 处理: {rel} ===")
    shots = detect_shots(video, args.threshold, args.min_scene_len)
    total = len(shots)
    print(f"  检测到 {total} 个镜头")

    if total == 0:
        print("  未检测到镜头边界，跳过")
        return

    # 去掉片头（封面）和片尾片段
    kept = shots[args.skip_head: total - args.skip_tail if args.skip_tail else None]
    if not kept:
        print(f"  去掉片头 {args.skip_head} 个、片尾 {args.skip_tail} 个后没有剩余片段，跳过")
        return
    print(f"  去掉片头 {args.skip_head} 个、片尾 {args.skip_tail} 个，保留 {len(kept)} 个片段")

    # 维持输入文件夹的路径结构，再按原视频名建立输出文件夹
    out_dir = out_root / rel.parent / video.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    ext = video.suffix if args.copy else ".mp4"
    ok = 0
    for i, (start, end) in enumerate(kept, 1):
        dst = out_dir / f"{video.stem}_shot_{i:03d}{ext}"
        if args.skip_existing and dst.exists():
            ok += 1
            continue
        if cut_segment(video, dst, start, end, args):
            ok += 1
            print(f"    [{i}/{len(kept)}] {dst.name}  ({start:.2f}s - {end:.2f}s)")
    print(f"  完成: {ok}/{len(kept)} 个片段已保存到 {out_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="扫描文件夹中的视频，按镜头拆分并去掉片头/片尾片段",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", help="包含视频的输入文件夹")
    parser.add_argument("-o", "--output", default=None,
                        help="输出根目录（默认为输入文件夹下的 shots_output）")
    parser.add_argument("--skip-head", type=int, default=1,
                        help="去掉开头的镜头数（封面/片头）")
    parser.add_argument("--skip-tail", type=int, default=1,
                        help="去掉结尾的镜头数（片尾）")
    parser.add_argument("--threshold", type=float, default=27.0,
                        help="场景切换检测阈值，越小越敏感（切得越碎）")
    parser.add_argument("--min-scene-len", type=float, default=0.6,
                        help="最短镜头时长（秒），短于此的不会单独成段")
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
    parser.add_argument("--no-dedup", action="store_true",
                        help="不做重复视频检测（默认会按内容指纹去重，重复的只处理一次）")
    parser.add_argument("--skip-existing", action="store_true",
                        help="跳过已存在的输出片段（断点续跑）")
    args = parser.parse_args()

    in_dir = Path(args.input).expanduser().resolve()
    if not in_dir.is_dir():
        sys.exit(f"输入文件夹不存在: {in_dir}")
    out_root = Path(args.output).expanduser().resolve() if args.output else in_dir / "shots_output"

    videos = find_videos(in_dir)
    # 避免把输出目录里的片段再当作输入
    videos = [v for v in videos if out_root not in v.parents]
    if not videos:
        sys.exit(f"在 {in_dir} 中没有找到视频文件（支持: {', '.join(sorted(VIDEO_EXTS))}）")

    print(f"找到 {len(videos)} 个视频")
    if not args.no_dedup:
        print("正在扫描重复视频 ...")
        videos = dedupe_videos(videos, in_dir)
    print(f"待处理 {len(videos)} 个视频，输出目录: {out_root}")
    for v in videos:
        try:
            process_video(v, in_dir, out_root, args)
        except Exception as e:
            print(f"  [错误] 处理 {v.name} 失败: {e}")

    print("\n全部完成。")


if __name__ == "__main__":
    main()
