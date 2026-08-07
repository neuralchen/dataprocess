#!/usr/bin/env python3
"""从文本清单中提取 ed2k 链接并批量下载。

依赖 aMule 提供 ed2k 协议支持：脚本负责解析清单、投递任务、跟踪进度、
整理产出，实际传输由 amuled 守护进程完成。

仅用于下载你拥有相应权利的内容。

准备工作（Ubuntu）:
    sudo apt install -y amule-daemon amule-utils
    amuled --ec-config          # 首次运行会生成配置并提示设置远程口令
    # 编辑 ~/.aMule/amule.conf，确认以下项：
    #   [ExternalConnect]
    #   AcceptExternalConnections=1
    #   ECPassword=<你设置口令的 MD5>
    systemctl --user start amule-daemon    # 或 amuled -f

用法:
    python3 ed2k_downloader.py links.txt -o /mnt/hd2/downloads
    python3 ed2k_downloader.py links.txt -o /data/videos --password 口令
    python3 ed2k_downloader.py links.txt --list-only      # 只解析不下载
"""

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote

# ed2k://|file|<文件名>|<字节数>|<MD4 哈希>|/
ED2K_RE = re.compile(
    r"ed2k://\|file\|(?P<name>[^|]+)\|(?P<size>\d+)\|(?P<hash>[0-9A-Fa-f]{32})\|",
    re.IGNORECASE)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def parse_links(path: Path):
    """从任意文本中提取 ed2k 链接，按哈希去重。

    清单里常混有分组标题、大小标注等杂项，直接正则扫全文比逐行解析更稳。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    seen, items = set(), []
    for m in ED2K_RE.finditer(text):
        h = m.group("hash").upper()
        if h in seen:
            continue
        seen.add(h)
        items.append({
            "link": m.group(0).rstrip("|/") + "|/",
            "name": unquote(m.group("name")),
            "size": int(m.group("size")),
            "hash": h,
        })
    return items


def ec_available():
    return shutil.which("amulecmd") is not None


def ec_cmd(args, cmd, timeout=60):
    """通过 amulecmd 与 amuled 通信。"""
    base = ["amulecmd", "-h", args.host, "-p", str(args.port)]
    if args.password:
        base += ["-P", args.password]
    try:
        r = subprocess.run(base + ["-c", cmd], capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def ensure_daemon(args):
    ok, out = ec_cmd(args, "status")
    if ok:
        return True, out
    return False, out


def add_links(args, items):
    """把链接投递给 amuled。已在队列中的会被它自行忽略。"""
    added, failed = 0, []
    for it in items:
        ok, out = ec_cmd(args, f'add {it["link"]}')
        if ok:
            added += 1
        else:
            failed.append((it["name"], out[:80]))
    return added, failed


# amulecmd 的每个任务占两行，第二行形如：
#    > \t [12.3%]  123456/999999   - Downloading - 007.part.met - Auto [Hi]
# 注意状态词没有方括号包裹，早期版本按 [Waiting] 匹配是错的，导致计数恒为 0。
TASK_LINE_RE = re.compile(
    r"\[\s*(?P<pct>[\d.]+)%\]\s+(?P<got>\d+)\s*/\s*(?P<total>\d+)\s+-\s+(?P<status>[^-]+?)\s+-")


def parse_queue(out):
    """解析 show DL 输出，按状态分类统计。

    返回 {状态: 个数}、总体字节进度，以及任务明细列表。
    """
    stats, tasks = {}, []
    for m in TASK_LINE_RE.finditer(out):
        st = m.group("status").strip()
        stats[st] = stats.get(st, 0) + 1
        tasks.append({"pct": float(m.group("pct")),
                      "got": int(m.group("got")),
                      "total": int(m.group("total")),
                      "status": st})
    got = sum(t["got"] for t in tasks)
    total = sum(t["total"] for t in tasks)
    return stats, got, total, tasks


def progress(args):
    """查询下载队列状态。返回 (状态统计, 已下字节, 总字节, 任务列表)。"""
    ok, out = ec_cmd(args, "show DL")
    if not ok:
        return None
    return parse_queue(out)


def collect(args, items):
    """把 aMule 的产出按原始文件名整理到目标目录。"""
    incoming = Path(args.incoming).expanduser()
    dest = Path(args.output).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    if not incoming.is_dir():
        return 0, f"aMule 完成目录不存在: {incoming}"
    moved = 0
    for f in incoming.iterdir():
        if not f.is_file():
            continue
        target = dest / f.name
        if target.exists() and target.stat().st_size == f.stat().st_size:
            continue
        try:
            shutil.move(str(f), str(target))
            moved += 1
        except OSError as e:
            print(f"  移动失败 {f.name}: {e}")
    return moved, None


def verify(dest: Path, items):
    """按清单里的字节数核对产出，大小不符的多半是没下完。"""
    by_name = {it["name"]: it for it in items}
    ok = bad = missing = 0
    for it in items:
        p = dest / it["name"]
        if not p.exists():
            missing += 1
        elif p.stat().st_size == it["size"]:
            ok += 1
        else:
            bad += 1
            print(f"  大小不符: {it['name'][:50]} "
                  f"({human(p.stat().st_size)} / 应为 {human(it['size'])})")
    return ok, bad, missing


def main():
    ap = argparse.ArgumentParser(
        description="从文本清单批量下载 ed2k 链接（需 aMule 守护进程）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("listfile", nargs="?", help="含 ed2k 链接的文本文件（--status 时可省略）")
    ap.add_argument("-o", "--output", default="./downloads", help="下载完成后的存放目录")
    ap.add_argument("--incoming", default="~/.aMule/Incoming",
                    help="aMule 的完成目录，脚本从这里取走文件")
    ap.add_argument("--host", default="127.0.0.1", help="amuled 地址")
    ap.add_argument("--port", type=int, default=4712, help="amuled 外部连接端口")
    ap.add_argument("-P", "--password", default="", help="amuled 远程口令")
    ap.add_argument("--list-only", action="store_true", help="只解析清单，不下载")
    ap.add_argument("--status", action="store_true",
                    help="只查询当前队列状态后退出，不投递新任务")
    ap.add_argument("--no-collect", action="store_true", help="不自动整理产出")
    ap.add_argument("--interval", type=int, default=60, help="进度轮询间隔（秒）")
    args = ap.parse_args()

    if args.status:
        if not ec_available():
            sys.exit("找不到 amulecmd，请先安装：sudo apt install amule-daemon amule-utils")
        st = progress(args)
        if st is None:
            sys.exit("无法连接 amuled，请确认守护进程已启动")
        stats, got, total, tasks = st
        print(f"队列任务 {len(tasks)} 个" +
              ("，状态: " + "、".join(f"{k} {v}" for k, v in sorted(stats.items()))
               if stats else ""))
        if total:
            print(f"整体进度: {human(got)} / {human(total)} ({100.0*got/total:.2f}%)")
        act = [t for t in tasks if t["got"] > 0]
        print(f"有字节进度的任务: {len(act)} / {len(tasks)}")
        if tasks and not act:
            print("所有任务均无进度，通常意味着连不到源"
                  "（LowID 会严重限制可连对端，需转发 TCP 4662 / UDP 4672）")
        return

    if not args.listfile:
        sys.exit("请指定清单文件，或用 --status 查询当前队列")
    src = Path(args.listfile).expanduser()
    if not src.is_file():
        sys.exit(f"清单文件不存在: {src}")

    items = parse_links(src)
    if not items:
        sys.exit("清单中没有找到 ed2k 链接")
    total = sum(it["size"] for it in items)
    print(f"解析到 {len(items)} 个链接（去重后），合计 {human(total)}")

    if args.list_only:
        for i, it in enumerate(items, 1):
            print(f"  {i:>3}. {human(it['size']):>9}  {it['name'][:70]}")
        return

    dest = Path(args.output).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest).free
    print(f"目标目录 {dest}，可用 {human(free)}")
    if total > free:
        sys.exit(f"空间不足：需要 {human(total)}，仅剩 {human(free)}")

    if not ec_available():
        sys.exit("找不到 amulecmd，请先安装：sudo apt install amule-daemon amule-utils")
    ok, out = ensure_daemon(args)
    if not ok:
        sys.exit(f"无法连接 amuled：{out[:200]}\n"
                 f"请确认守护进程已启动，且 amule.conf 里 AcceptExternalConnections=1")

    added, failed = add_links(args, items)
    print(f"已投递 {added}/{len(items)} 个任务" +
          (f"，失败 {len(failed)} 个" if failed else ""))
    for name, err in failed[:5]:
        print(f"  失败: {name[:50]} -> {err}")

    print("\n开始下载，Ctrl+C 可随时退出（任务在 amuled 中继续）\n")
    try:
        while True:
            time.sleep(args.interval)
            st = progress(args)
            if st is None:
                print("  查询进度失败，稍后重试")
                continue
            stats, got, total_bytes, tasks = st
            if not args.no_collect:
                moved, err = collect(args, items)
                if moved:
                    print(f"  已整理 {moved} 个文件到 {dest}")
            okc, bad, miss = verify(dest, items)
            # 按状态明细展示，而不是笼统的"队列中 N"——
            # 全是 Waiting 说明连不到源，和正在下载是完全不同的情况
            detail = "、".join(f"{k} {v}" for k, v in sorted(stats.items())) or "队列为空"
            pct = (100.0 * got / total_bytes) if total_bytes else 0.0
            print(f"  [{time.strftime('%H:%M:%S')}] 已完成 {okc}/{len(items)}"
                  f" | 队列: {detail} | 已下载 {human(got)}"
                  + (f" ({pct:.1f}%)" if total_bytes else "")
                  + (f" | 大小异常 {bad}" if bad else ""))
            if stats and set(stats) == {"Waiting"} and got == 0:
                print("     提示：全部处于 Waiting 且无字节进度，通常是连不到源。"
                      "若为 LowID，需转发 TCP 4662 / UDP 4672 以获得 HighID")
            if okc == len(items):
                print("\n全部下载完成")
                break
    except KeyboardInterrupt:
        print("\n已退出监控，下载任务仍在 amuled 中运行")

    okc, bad, miss = verify(dest, items)
    print(f"\n最终：完整 {okc}，大小异常 {bad}，缺失 {miss}，共 {len(items)}")


if __name__ == "__main__":
    main()
