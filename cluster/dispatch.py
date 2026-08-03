#!/usr/bin/env python3
"""集群数据分发与导出工具（就近处理模式）。

设计原则：处理阶段不走网络。素材按分片预先分发到各节点本地盘，
节点只读写自己的本地目录；只有导出时才集中收集。

分片规则与 split_shots.py 的 --shard 完全一致（视频相对路径的 SHA-256 取模），
因此节点 i 上的素材，正好就是 `--shard i/N` 会处理的那一份。

用法:
    # 查看各节点的本地数据与容量
    python3 dispatch.py status

    # 把素材按分片分发到各节点本地盘
    python3 dispatch.py scatter /path/to/videos

    # 把各节点的产出收集到一处（U 盘、另一台服务器、NAS）
    python3 dispatch.py gather /mnt/usb/export
    python3 dispatch.py gather user@host:/data/collected --remote

    # 清空各节点的本地输入（导出确认无误后回收空间）
    python3 dispatch.py clean --input
"""

import argparse
import hashlib
import shlex
import subprocess
import sys
from pathlib import Path

# 集群节点：名称 -> (SSH 端口, 内网 IP)。SSH 从 master 直连内网地址。
NODES = [
    ("10094", "192.168.1.218"),
    ("10095", "192.168.1.221"),
    ("10082", "192.168.1.206"),
    ("10052", "192.168.1.219"),
]
SSH_USER = "ubuntu"
# 各节点本地数据根目录（不是 NFS，处理时零网络 IO）
LOCAL_ROOT = "/mnt/hd/Project/local_data"
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm",
              ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".rmvb", ".rm", ".vob"}

SSH_OPTS = ["-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes"]


def shard_of(rel_path: str, total: int) -> int:
    """与 split_shots.py 的 apply_shard 保持一致的分片算法。"""
    return int(hashlib.sha256(str(rel_path).encode("utf-8")).hexdigest(), 16) % total


def run(cmd, capture=True, timeout=None):
    try:
        r = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def ssh(ip, remote_cmd, timeout=120):
    return run(["ssh"] + SSH_OPTS + [f"{SSH_USER}@{ip}", remote_cmd], timeout=timeout)


def is_local(ip):
    """master 上跑本工具时，对自己走本地命令，省一次 SSH。"""
    ok, out = run(["hostname", "-I"])
    return ok and ip in out.split()


def node_exec(ip, cmd, timeout=120):
    if is_local(ip):
        return run(["bash", "-c", cmd], timeout=timeout)
    return ssh(ip, cmd, timeout=timeout)


def cmd_status(args):
    """各节点的本地数据量与剩余容量。"""
    print(f"{'节点':<10} {'输入视频':>8} {'已处理':>8} {'产出片段':>9} "
          f"{'输入占用':>9} {'产出占用':>9} {'盘剩余':>8}")
    print("-" * 70)
    for name, ip in NODES:
        c = (f"IN={LOCAL_ROOT}/input; OUT={LOCAL_ROOT}/output; "
             f"echo $(find $IN -type f 2>/dev/null | wc -l) "
             f"$(find $OUT -name scene.json 2>/dev/null | wc -l) "
             f"$(find $OUT -name '*.mp4' 2>/dev/null | wc -l) "
             f"$(du -sh $IN 2>/dev/null | cut -f1) "
             f"$(du -sh $OUT 2>/dev/null | cut -f1) "
             f"$(df -h {LOCAL_ROOT} 2>/dev/null | tail -1 | awk '{{print $4}}')")
        ok, out = node_exec(ip, c)
        if not ok:
            print(f"{name:<10} {'连接失败':>8}  {out[:40]}")
            continue
        f = out.split()
        while len(f) < 6:
            f.append("-")
        print(f"{name:<10} {f[0]:>8} {f[1]:>8} {f[2]:>9} {f[3]:>9} {f[4]:>9} {f[5]:>8}")


def collect_sources(src: Path):
    vids = sorted(p for p in src.rglob("*")
                  if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
    return vids


def cmd_scatter(args):
    """按分片把素材分发到各节点本地盘。"""
    src = Path(args.source).expanduser().resolve()
    if not src.is_dir():
        sys.exit(f"素材目录不存在: {src}")
    vids = collect_sources(src)
    if not vids:
        sys.exit(f"{src} 中没有找到视频")
    total = len(NODES)

    # 按分片归组
    groups = {i: [] for i in range(total)}
    for v in vids:
        groups[shard_of(str(v.relative_to(src)), total)].append(v)

    print(f"素材 {len(vids)} 个，分为 {total} 片：")
    for i, (name, _) in enumerate(NODES):
        size = sum(p.stat().st_size for p in groups[i]) / 1024**3
        print(f"  分片 {i} -> {name}: {len(groups[i]):>5} 个, {size:.1f} GB")
    if args.dry_run:
        print("\n（--dry-run，未实际传输）")
        return

    for i, (name, ip) in enumerate(NODES):
        files = groups[i]
        if not files:
            continue
        print(f"\n=== 分发到 {name} ({len(files)} 个) ===")
        node_exec(ip, f"mkdir -p {LOCAL_ROOT}/input {LOCAL_ROOT}/output")
        # 用 rsync 增量传输，--files-from 保持源目录结构
        listing = "\n".join(str(p.relative_to(src)) for p in files)
        lst = Path("/tmp") / f"scatter_{i}.txt"
        lst.write_text(listing, encoding="utf-8")
        dest = (f"{LOCAL_ROOT}/input/" if is_local(ip)
                else f"{SSH_USER}@{ip}:{LOCAL_ROOT}/input/")
        cmd = ["rsync", "-a", "--info=progress2", "--files-from", str(lst),
               str(src) + "/", dest]
        if not is_local(ip):
            cmd[1:1] = ["-e", "ssh " + " ".join(SSH_OPTS)]
        ok, out = run(cmd, capture=False, timeout=None)
        print(f"  {'完成' if ok else '失败: ' + out[:200]}")


def cmd_gather(args):
    """把各节点的产出收集到目标位置（U 盘 / 本地目录 / 远程主机）。"""
    dest = args.dest
    remote = args.remote or ":" in dest.split("/")[0]
    if not remote:
        d = Path(dest).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        # 目标容量预检，避免拷到一半空间不足
        need = 0
        for name, ip in NODES:
            ok, out = node_exec(ip, f"du -sb {LOCAL_ROOT}/output 2>/dev/null | cut -f1")
            if ok and out.strip().isdigit():
                need += int(out.strip())
        import shutil as _sh
        free = _sh.disk_usage(d).free
        print(f"待导出 {need/1024**3:.1f} GB，目标可用 {free/1024**3:.1f} GB")
        if need > free:
            sys.exit("目标空间不足，请更换目标或分批导出")
        dest = str(d)

    for name, ip in NODES:
        print(f"\n=== 从 {name} 收集 ===")
        ok, cnt = node_exec(ip, f"find {LOCAL_ROOT}/output -name scene.json 2>/dev/null | wc -l")
        if not ok:
            print(f"  跳过（连接失败）")
            continue
        if cnt.strip() == "0":
            print("  无产出")
            continue
        print(f"  {cnt.strip()} 个视频的产出")
        src = (f"{LOCAL_ROOT}/output/" if is_local(ip)
               else f"{SSH_USER}@{ip}:{LOCAL_ROOT}/output/")
        cmd = ["rsync", "-a", "--info=progress2"]
        if not is_local(ip):
            cmd += ["-e", "ssh " + " ".join(SSH_OPTS)]
        if args.move:
            cmd.append("--remove-source-files")
        cmd += [src, dest if dest.endswith("/") else dest + "/"]
        ok, out = run(cmd, capture=False, timeout=None)
        print(f"  {'完成' if ok else '失败: ' + out[:200]}")
    print(f"\n导出目标: {dest}")


def cmd_clean(args):
    """清理各节点的本地数据（导出确认后回收空间）。"""
    targets = []
    if args.input:
        targets.append("input")
    if args.output:
        targets.append("output")
    if not targets:
        sys.exit("请指定 --input 或 --output")
    print(f"将清空各节点的 {', '.join(targets)} 目录")
    if not args.yes:
        if input("确认？(yes/N) ").strip().lower() != "yes":
            sys.exit("已取消")
    for name, ip in NODES:
        for t in targets:
            ok, _ = node_exec(ip, f"rm -rf {LOCAL_ROOT}/{t}/* 2>/dev/null; echo done")
            print(f"  {name} {t}: {'已清空' if ok else '失败'}")


def main():
    ap = argparse.ArgumentParser(
        description="集群数据分发与导出（就近处理模式）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="查看各节点本地数据与容量")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("scatter", help="按分片把素材分发到各节点本地盘")
    s.add_argument("source", help="素材目录")
    s.add_argument("--dry-run", action="store_true", help="只显示分配方案，不传输")
    s.set_defaults(func=cmd_scatter)

    s = sub.add_parser("gather", help="收集各节点产出到目标位置")
    s.add_argument("dest", help="目标目录，或 user@host:/path")
    s.add_argument("--remote", action="store_true", help="目标是远程主机")
    s.add_argument("--move", action="store_true", help="收集后删除节点上的源文件")
    s.set_defaults(func=cmd_gather)

    s = sub.add_parser("clean", help="清空各节点的本地数据")
    s.add_argument("--input", action="store_true")
    s.add_argument("--output", action="store_true")
    s.add_argument("--yes", action="store_true", help="跳过确认")
    s.set_defaults(func=cmd_clean)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
