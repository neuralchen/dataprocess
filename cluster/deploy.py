#!/usr/bin/env python3
"""k3s 视频处理集群一键部署工具（可在 Windows / Linux / macOS 上运行）。

读取 cluster.json 里的节点清单（IP、端口、账号、密码），通过 SSH 自动完成：
安装 k3s、组网、部署项目与运行环境、配置共享存储、构建分发 worker 镜像、启动管理界面。

注意：master 必须是 Linux 机器（k3s 控制平面不支持 Windows），
但本工具本身可以在 Windows 上运行来远程操控。

用法:
    pip install paramiko
    cp cluster.example.json cluster.json     # Windows: copy
    # 编辑 cluster.json 填入实际的 IP / 账号 / 密码
    python deploy.py check      # 只检查各节点环境，不做改动
    python deploy.py deploy     # 执行完整部署
    python deploy.py status     # 查看集群状态
    python deploy.py teardown   # 卸载 k3s（不删数据）
"""

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit("缺少依赖 paramiko，请执行: pip install paramiko")

HERE = Path(__file__).resolve().parent
K3S_INSTALL_URL = "https://get.k3s.io"


def log(msg, indent=0):
    print("  " * indent + msg, flush=True)


class Node:
    """一台远程主机，封装 SSH 执行与文件上传。"""

    def __init__(self, cfg, opts, role):
        self.name = cfg.get("name") or cfg["host"]
        self.host = cfg["host"]
        self.port = int(cfg.get("ssh_port", 22))
        self.user = cfg["user"]
        self.password = cfg["password"]
        self.data_dir = cfg.get("data_dir", "/mnt/hd").rstrip("/")
        self.role = role
        self.opts = opts
        self._cli = None

    # ---------- 连接与执行 ----------

    @property
    def cli(self):
        if self._cli is None:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(self.host, port=self.port, username=self.user,
                      password=self.password, timeout=20, banner_timeout=30)
            self._cli = c
        return self._cli

    def run(self, cmd, timeout=600, check=False):
        """执行命令，返回 (退出码, 输出)。"""
        _, out, err = self.cli.exec_command(cmd, timeout=timeout)
        text = out.read().decode("utf-8", "replace")
        code = out.channel.recv_exit_status()
        if code != 0:
            text += err.read().decode("utf-8", "replace")
        if check and code != 0:
            raise RuntimeError(f"[{self.name}] 命令失败: {cmd}\n{text.strip()[:500]}")
        return code, text.strip()

    def sudo(self, cmd, **kw):
        """以 root 执行。密码通过 stdin 传给 sudo -S，
        因此命令本身不能再依赖 stdin（需要 stdin 的场景请先写临时文件）。"""
        safe = cmd.replace("'", "'\\''")
        return self.run(f"echo '{self.password}' | sudo -S -p '' bash -c '{safe}'", **kw)

    def put_text(self, content, remote_path, mode=0o644, as_root=False):
        """把文本写到远端文件。先传到临时路径再 sudo 移动，避开权限与 stdin 问题。"""
        tmp = f"/tmp/.deploy_{int(time.time()*1000)%100000}"
        sftp = self.cli.open_sftp()
        try:
            with sftp.file(tmp, "w") as f:
                f.write(content)
            sftp.chmod(tmp, mode)
        finally:
            sftp.close()
        if as_root:
            self.sudo(f"cp {tmp} {remote_path} && chmod {oct(mode)[2:]} {remote_path}", check=True)
        else:
            self.run(f"cp {tmp} {remote_path} && chmod {oct(mode)[2:]} {remote_path}", check=True)
        self.run(f"rm -f {tmp}")

    def close(self):
        if self._cli:
            self._cli.close()
            self._cli = None

    # ---------- 环境探测 ----------

    def probe(self):
        info = {"name": self.name, "host": self.host, "role": self.role}
        _, info["os"] = self.run("lsb_release -ds 2>/dev/null || cat /etc/os-release | "
                                 "grep PRETTY | cut -d'\"' -f2")
        _, info["cpu"] = self.run("nproc")
        _, info["mem"] = self.run("free -g | awk '/Mem:/{print $2}'")
        _, info["gpu"] = self.run("nvidia-smi --query-gpu=name --format=csv,noheader "
                                  "2>/dev/null | tr '\\n' ',' | sed 's/,$//'")
        _, info["ffmpeg"] = self.run("ffmpeg -version 2>/dev/null | head -1 | cut -c1-30")
        _, info["nvenc"] = self.run("ffmpeg -hide_banner -encoders 2>/dev/null | grep -c nvenc")
        _, info["docker"] = self.run("command -v docker >/dev/null && echo yes || echo no")
        _, info["k3s"] = self.run("command -v k3s >/dev/null && echo yes || echo no")
        _, info["disk"] = self.run(
            f"df -h {self.data_dir} 2>/dev/null | tail -1 | awk '{{print $4\" 可用 / \"$2}}'")
        _, info["ip"] = self.run("hostname -I | tr ' ' '\\n' | grep -E '^(192|10|172)' | head -1")
        rc, _ = self.sudo("true")
        info["sudo"] = "ok" if rc == 0 else "失败"
        return info


# ---------- 部署步骤 ----------

def ensure_prereqs(node: Node):
    """补齐 ffmpeg、python venv、nfs 客户端等基础依赖。"""
    need = []
    for pkg, probe in (("ffmpeg", "command -v ffmpeg"),
                       ("python3-venv", "python3 -m venv --help"),
                       ("python3-pip", "command -v pip3"),
                       ("nfs-common", "command -v mount.nfs"),
                       ("git", "command -v git")):
        rc, _ = node.run(f"{probe} >/dev/null 2>&1")
        if rc != 0:
            need.append(pkg)
    if need:
        log(f"安装缺失依赖: {', '.join(need)}", 2)
        node.sudo("DEBIAN_FRONTEND=noninteractive apt-get update -q", timeout=900)
        node.sudo(f"DEBIAN_FRONTEND=noninteractive apt-get install -y -q {' '.join(need)}",
                  timeout=1800)
    else:
        log("基础依赖齐备", 2)


def setup_data_dir(node: Node):
    """把 k3s 数据目录绑定到大盘，避免撑爆根分区。"""
    d = node.data_dir
    node.sudo(f"mkdir -p {d}/k3s /var/lib/rancher", check=True)
    rc, _ = node.run("mountpoint -q /var/lib/rancher")
    if rc != 0:
        node.sudo(f"mount --bind {d}/k3s /var/lib/rancher", check=True)
    # 写入 fstab 使其重启后仍生效
    rc, fstab = node.run("cat /etc/fstab")
    line = f"{d}/k3s /var/lib/rancher none bind 0 0"
    if line not in fstab:
        node.put_text(fstab.rstrip("\n") + "\n" + line + "\n", "/etc/fstab", as_root=True)
    _, where = node.run("df -h /var/lib/rancher | tail -1 | awk '{print $1\" \"$4}'")
    log(f"k3s 数据目录 -> {where}", 2)


def install_k3s_binary(node: Node, version):
    """预先下载 k3s 二进制，避免安装脚本因网络波动失败。"""
    rc, _ = node.run("test -x /usr/local/bin/k3s")
    if rc == 0:
        return
    url = (f"https://github.com/k3s-io/k3s/releases/download/"
           f"{version.replace('+', '%2B')}/k3s")
    log("下载 k3s 二进制 ...", 2)
    rc, out = node.run(f"curl -sfL --retry 3 --max-time 600 -o /tmp/k3s '{url}'", timeout=900)
    if rc != 0:
        raise RuntimeError(f"[{node.name}] k3s 下载失败: {out[:200]}")
    node.sudo("install -m 755 /tmp/k3s /usr/local/bin/k3s", check=True)


def install_master(master: Node, version):
    rc, _ = master.run("systemctl is-active k3s")
    if rc == 0:
        log("k3s server 已在运行", 2)
        return
    install_k3s_binary(master, version)
    master.run(f"curl -sfL {K3S_INSTALL_URL} -o /tmp/k3s_install.sh", timeout=300)
    ip = master.internal_ip
    # 注意：sudo 会剥离环境变量，必须用 sudo env 显式传递
    cmd = (f"env INSTALL_K3S_SKIP_DOWNLOAD=true "
           f"INSTALL_K3S_EXEC='server --node-ip {ip} --tls-san {ip} "
           f"--write-kubeconfig-mode 644 --disable servicelb "
           f"--kubelet-arg=eviction-hard=nodefs.available<1%,imagefs.available<1%' "
           f"sh /tmp/k3s_install.sh")
    log("安装 k3s server ...", 2)
    master.sudo(cmd, timeout=900, check=True)
    for _ in range(30):
        time.sleep(5)
        rc, out = master.run("kubectl get nodes --no-headers 2>/dev/null")
        if rc == 0 and "Ready" in out:
            log("master 就绪", 2)
            return
    raise RuntimeError("master 启动超时")


def join_worker(worker: Node, master_ip, token, version):
    rc, _ = worker.run("systemctl is-active k3s-agent")
    if rc == 0:
        log("已加入集群", 2)
        return
    install_k3s_binary(worker, version)
    worker.run(f"curl -sfL {K3S_INSTALL_URL} -o /tmp/k3s_install.sh", timeout=300)
    ip = worker.internal_ip
    # 令牌通过环境文件传递，避免出现在命令行里被其它用户看到
    worker.put_text(f"K3S_URL=https://{master_ip}:6443\nK3S_TOKEN={token}\n",
                    "/etc/systemd/system/k3s-agent.service.env", mode=0o600, as_root=True)
    cmd = (f"env INSTALL_K3S_SKIP_DOWNLOAD=true K3S_URL=https://{master_ip}:6443 "
           f"K3S_TOKEN='{token}' INSTALL_K3S_EXEC='agent --node-ip {ip}' "
           f"sh /tmp/k3s_install.sh")
    log("加入集群 ...", 2)
    worker.sudo(cmd, timeout=900, check=True)
    time.sleep(10)
    rc, st = worker.run("systemctl is-active k3s-agent")
    log(f"agent 状态: {st}", 2)


def deploy_project(node: Node, opts):
    """拉取代码并建立 Python 运行环境。"""
    proj = f"{node.data_dir}/{opts['project_dir']}"
    parent = str(Path(proj).parent).replace("\\", "/")
    node.run(f"mkdir -p {parent}")
    rc, _ = node.run(f"test -d {proj}/.git")
    if rc == 0:
        node.run(f"cd {proj} && git pull -q", timeout=300)
    else:
        rc, out = node.run(f"git clone -q {opts['repo']} {proj}", timeout=900)
        if rc != 0:
            raise RuntimeError(f"[{node.name}] 克隆失败: {out[:200]}")
    node.run(f"cd {proj} && test -d .venv || python3 -m venv .venv", timeout=300)
    node.run(f"cd {proj} && .venv/bin/python -m ensurepip --upgrade", timeout=300)
    node.run(f"cd {proj} && .venv/bin/python -m pip install -q --upgrade pip", timeout=600)
    rc, out = node.run(f"cd {proj} && .venv/bin/python -m pip install -q -r requirements.txt",
                       timeout=1800)
    rc2, ver = node.run(f"cd {proj} && .venv/bin/python -c "
                        f"'import scenedetect;print(scenedetect.__version__)'")
    log(f"项目就绪 (scenedetect {ver})" if rc2 == 0 else f"依赖安装异常: {out[:150]}", 2)


def setup_shared_storage(master: Node, workers, opts):
    """master 导出 NFS，各 worker 挂载到统一路径。"""
    share = f"{master.data_dir}/{opts['shared_dir']}"
    master.sudo("DEBIAN_FRONTEND=noninteractive apt-get install -y -q nfs-kernel-server",
                timeout=900)
    master.run(f"mkdir -p {share}/input {share}/output")
    rc, exports = master.run("cat /etc/exports 2>/dev/null")
    subnet = ".".join(master.internal_ip.split(".")[:3]) + ".0/24"
    line = f"{share} {subnet}(rw,sync,no_subtree_check,no_root_squash)"
    if line not in exports:
        master.put_text((exports.rstrip("\n") + "\n" + line + "\n").lstrip("\n"),
                        "/etc/exports", as_root=True)
    master.sudo("exportfs -ra && systemctl enable --now nfs-kernel-server", check=True)
    # master 自身也挂一份，保证四台机器路径一致
    master.sudo("mkdir -p /mnt/cluster_data")
    rc, _ = master.run("mountpoint -q /mnt/cluster_data")
    if rc != 0:
        master.sudo(f"mount --bind {share} /mnt/cluster_data")
    log(f"NFS 导出 {share} -> {subnet}", 2)

    for w in workers:
        w.sudo("mkdir -p /mnt/cluster_data")
        rc, _ = w.run("mountpoint -q /mnt/cluster_data")
        if rc != 0:
            rc, out = w.sudo(f"mount -t nfs {master.internal_ip}:{share} /mnt/cluster_data",
                             timeout=120)
            if rc != 0:
                log(f"[{w.name}] 挂载失败: {out[:120]}", 2)
                continue
        rc, fstab = w.run("cat /etc/fstab")
        ent = f"{master.internal_ip}:{share} /mnt/cluster_data nfs defaults,nofail,_netdev 0 0"
        if ent not in fstab:
            w.put_text(fstab.rstrip("\n") + "\n" + ent + "\n", "/etc/fstab", as_root=True)
        _, avail = w.run("df -h /mnt/cluster_data | tail -1 | awk '{print $4}'")
        log(f"[{w.name}] 已挂载，可用 {avail}", 2)


def label_nodes(master: Node, nodes):
    for n in nodes:
        _, gpus = n.run("nvidia-smi -L 2>/dev/null | wc -l")
        _, k8s = master.run(f"kubectl get nodes -o wide --no-headers | "
                            f"awk '$6==\"{n.internal_ip}\"{{print $1}}'")
        if not k8s.strip():
            continue
        master.run(f"kubectl label node {k8s.strip()} gpu-count={gpus.strip() or 0} "
                   f"role={n.role} --overwrite >/dev/null 2>&1")
    log("节点标签已设置", 2)


def write_nodes_json(master: Node, all_nodes, opts):
    """生成 nodes.json —— 分发数据与分配分片共用的权威映射表。"""
    entries = []
    for n in all_nodes:
        _, k8s = master.run(f"kubectl get nodes -o wide --no-headers | "
                            f"awk '$6==\"{n.internal_ip}\"{{print $1}}'")
        entries.append({"name": n.name, "k8s_name": k8s.strip() or n.name,
                        "ip": n.internal_ip, "ssh_port": n.port})
    cfg = {
        "_comment": "由 deploy.py 自动生成。分片号 = 本列表中的位置，"
                    "dispatch.py 与 server.py 共用，改动顺序需重新分发数据。",
        "local_root": f"{master.data_dir}/{opts['local_data_dir']}",
        "ssh_user": master.user,
        "nodes": entries,
    }
    proj = f"{master.data_dir}/{opts['project_dir']}"
    master.put_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                    f"{proj}/cluster/nodes.json")
    log(f"nodes.json 已生成（{len(entries)} 个节点）", 2)


def build_and_distribute_image(master: Node, workers, opts):
    """在 master 构建 worker 镜像并分发到各节点的 containerd。"""
    proj = f"{master.data_dir}/{opts['project_dir']}"
    rc, _ = master.run("command -v docker >/dev/null")
    if rc != 0:
        log("master 未安装 docker，跳过镜像构建（可后续手动执行）", 2)
        return
    log("构建 worker 镜像 ...", 2)
    rc, out = master.run(f"cd {proj}/cluster && docker build -q -t video-pipeline:latest .",
                         timeout=2400)
    if rc != 0:
        log(f"构建失败: {out[:200]}", 2)
        return
    tar = f"{master.data_dir}/{opts['shared_dir']}/video-pipeline.tar"
    master.run(f"docker save video-pipeline:latest -o {tar}", timeout=900)
    master.sudo(f"k3s ctr images import {tar}", timeout=900)
    for w in workers:
        rc, _ = w.sudo(f"k3s ctr images import /mnt/cluster_data/video-pipeline.tar",
                       timeout=900)
        log(f"[{w.name}] 镜像导入{'成功' if rc == 0 else '失败'}", 2)
    master.run(f"rm -f {tar}")


def setup_ui(master: Node, opts):
    """把管理界面装成开机自启的 systemd 服务。"""
    proj = f"{master.data_dir}/{opts['project_dir']}"
    env = ""
    if opts.get("ui_user"):
        env = (f"Environment=PIPELINE_USER={opts['ui_user']}\n"
               f"Environment=PIPELINE_PASS={opts.get('ui_password', '')}\n")
    unit = f"""[Unit]
Description=Video Pipeline Cluster UI
After=network.target k3s.service

[Service]
Type=simple
User={master.user}
WorkingDirectory={proj}/cluster
Environment=KUBECONFIG=/etc/rancher/k3s/k3s.yaml
{env}ExecStart=/usr/bin/python3 {proj}/cluster/server.py --port {opts.get('ui_port', 8080)}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    master.put_text(unit, "/etc/systemd/system/cluster-ui.service", mode=0o600, as_root=True)
    master.sudo("systemctl daemon-reload && systemctl enable --now cluster-ui", check=True)
    time.sleep(4)
    _, st = master.run("systemctl is-active cluster-ui")
    log(f"管理界面: {st} -> http://{master.internal_ip}:{opts.get('ui_port', 8080)}", 2)
    if opts.get("ui_user"):
        log(f"访问账号: {opts['ui_user']}", 2)


# ---------- 命令 ----------

def load_config(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"找不到配置文件 {p}\n请先复制 cluster.example.json 为 cluster.json 并填写")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    opts = {k: v for k, v in cfg.get("options", {}).items()}
    opts.setdefault("project_dir", "Project/dataprocess")
    opts.setdefault("repo", "https://github.com/neuralchen/dataprocess.git")
    opts.setdefault("shared_dir", "Project/cluster_data")
    opts.setdefault("local_data_dir", "Project/local_data")
    opts.setdefault("k3s_version", "v1.36.2+k3s1")
    master = Node(cfg["master"], opts, "master")
    workers = [Node(w, opts, "worker") for w in cfg.get("workers", [])]
    return master, workers, opts


def resolve_ips(nodes):
    """取各节点的内网地址（k3s 组网用），失败则退回配置里的 host。"""
    for n in nodes:
        _, ip = n.run("hostname -I | tr ' ' '\\n' | grep -E '^(192|10|172)' | head -1")
        n.internal_ip = ip.strip() or n.host


def ask(prompt, default=""):
    tip = f"{prompt} [{default}]: " if default else f"{prompt}: "
    v = input(tip).strip()
    return v or default


def probe_candidate(host, port, user, password, data_dir):
    """连上去看看这台机器什么情况，顺便验证账号密码对不对。"""
    cfg = {"name": host, "host": host, "ssh_port": port, "user": user,
           "password": password, "data_dir": data_dir}
    n = Node(cfg, {}, "?")
    try:
        info = n.probe()
        _, ip = n.run("hostname -I | tr ' ' '\\n' | grep -E '^(192|10|172)' | head -1")
        _, hn = n.run("hostname -s")
        info["internal_ip"] = ip.strip()
        info["hostname"] = hn.strip()
        return info, None
    except Exception as e:
        return None, str(e)
    finally:
        n.close()


def cmd_init(config_path):
    """交互式向导：逐台录入节点，验证连通性，选定 master，生成 cluster.json。"""
    print("集群配置向导\n")
    print("提示：master 必须是 Linux 机器（k3s 控制平面不支持 Windows）。")
    print("      本工具可以在 Windows 上运行，远程操控这些 Linux 节点。\n")

    default_user = ask("默认登录账号", "ubuntu")
    default_pass = ask("默认登录密码（各节点相同时只需填一次）", "")
    default_dir = ask("默认数据盘路径（存放 k3s 数据与项目，别用根目录）", "/mnt/hd")

    nodes = []
    print("\n开始录入节点，直接回车结束录入。\n")
    while True:
        host = ask(f"第 {len(nodes)+1} 台的 IP 或主机名（回车结束）")
        if not host:
            break
        port = int(ask("  SSH 端口", "22") or 22)
        user = ask("  账号", default_user)
        pwd = ask("  密码", default_pass)
        ddir = ask("  数据盘路径", default_dir)
        print("  连接中 ...", end="", flush=True)
        info, err = probe_candidate(host, port, user, pwd, ddir)
        if err:
            print(f"\r  连接失败：{err[:80]}\n  请重新录入这一台\n")
            continue
        disk_ok = bool(info["disk"])
        print(f"\r  ✓ {info['hostname']}  内网 {info['internal_ip']}")
        print(f"    {info['os'][:36]} | {info['cpu']} 核 | {info['mem']} GB | "
              f"GPU {info['gpu'][:40] or '无'}")
        print(f"    数据盘 {ddir}: {info['disk'] or '不存在（部署会失败）'} | sudo {info['sudo']}")
        if not disk_ok:
            if ask("  数据盘路径无效，仍要加入吗？(y/N)", "N").lower() != "y":
                print()
                continue
        nodes.append({"name": info["hostname"] or host, "host": host, "ssh_port": port,
                      "user": user, "password": pwd, "data_dir": ddir, "_info": info})
        print()

    if not nodes:
        sys.exit("没有录入任何节点")

    print("已录入的节点：")
    for i, n in enumerate(nodes, 1):
        d = n["_info"]
        free = (d["disk"] or "").split()[0] if d["disk"] else "?"
        print(f"  {i}. {n['name']:<14} {d['internal_ip']:<15} "
              f"{d['cpu']} 核 / {d['mem']} GB / 数据盘 {free} / "
              f"GPU {len([x for x in (d['gpu'] or '').split(',') if x])} 张")

    # 推荐磁盘余量最大的做 master——它要跑 NFS 服务端和镜像仓库
    def free_bytes(n):
        s = (n["_info"]["disk"] or "0").split()[0].rstrip("BKMGTP")
        unit = (n["_info"]["disk"] or "0 ").split()[0][-1:]
        try:
            return float(s) * {"T": 1e12, "G": 1e9, "M": 1e6}.get(unit, 1)
        except ValueError:
            return 0
    rec = max(range(len(nodes)), key=lambda i: free_bytes(nodes[i])) + 1
    print(f"\nmaster 会承担控制平面、NFS 共享存储和管理界面，建议选磁盘余量最大的一台。")
    pick = ask(f"选择 master（输入编号）", str(rec))
    try:
        mi = int(pick) - 1
        assert 0 <= mi < len(nodes)
    except (ValueError, AssertionError):
        sys.exit("编号无效")

    print()
    ui_port = ask("管理界面端口", "8080")
    ui_user = ask("界面登录账号（留空则不启用认证，暴露公网时务必设置）", "admin")
    ui_pass = ask("界面登录密码", "") if ui_user else ""

    for n in nodes:
        n.pop("_info", None)
    cfg = {
        "master": nodes[mi],
        "workers": [n for i, n in enumerate(nodes) if i != mi],
        "options": {
            "project_dir": "Project/dataprocess",
            "repo": "https://github.com/neuralchen/dataprocess.git",
            "shared_dir": "Project/cluster_data",
            "local_data_dir": "Project/local_data",
            "ui_port": int(ui_port or 8080),
            "ui_user": ui_user,
            "ui_password": ui_pass,
            "k3s_version": "v1.36.2+k3s1",
        },
    }
    p = Path(config_path)
    if p.exists() and ask(f"{p.name} 已存在，覆盖？(y/N)", "N").lower() != "y":
        sys.exit("已取消")
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)     # 里面有密码，收紧权限
    except OSError:
        pass
    print(f"\n配置已保存到 {p}")
    print(f"  master: {nodes[mi]['name']}")
    print(f"  worker: {', '.join(n['name'] for i, n in enumerate(nodes) if i != mi) or '无'}")
    print("\n接下来执行：python deploy.py deploy")
    return 0


def cmd_check(master, workers, opts):
    log("检查各节点环境（不做任何改动）\n")
    ok = True
    for n in [master] + workers:
        try:
            info = n.probe()
        except Exception as e:
            log(f"[{n.name}] 连接失败: {e}")
            ok = False
            continue
        log(f"[{info['name']}] {info['host']}  ({info['role']})")
        log(f"系统 {info['os'][:40]} | CPU {info['cpu']} 核 | 内存 {info['mem']} GB", 2)
        log(f"GPU {info['gpu'] or '无'}", 2)
        log(f"ffmpeg {info['ffmpeg'] or '未安装'} | NVENC {info['nvenc']} 个", 2)
        log(f"docker {info['docker']} | k3s {info['k3s']} | sudo {info['sudo']}", 2)
        log(f"数据盘 {n.data_dir}: {info['disk'] or '不存在'}", 2)
        if info["sudo"] != "ok":
            log("!! sudo 不可用，部署会失败", 2)
            ok = False
        if not info["disk"]:
            log(f"!! 数据盘 {n.data_dir} 不存在，请修改 data_dir", 2)
            ok = False
        print()
    log("检查通过，可以执行 deploy" if ok else "存在问题，请先处理上面标记 !! 的项")
    return 0 if ok else 1


def cmd_deploy(master, workers, opts):
    all_nodes = [master] + workers
    log("=== 1/8 连接并探测节点 ===")
    resolve_ips(all_nodes)
    for n in all_nodes:
        log(f"[{n.name}] {n.host}:{n.port} -> 内网 {n.internal_ip}", 1)

    log("\n=== 2/8 安装基础依赖 ===")
    for n in all_nodes:
        log(f"[{n.name}]", 1)
        ensure_prereqs(n)

    log("\n=== 3/8 准备数据目录 ===")
    for n in all_nodes:
        log(f"[{n.name}]", 1)
        setup_data_dir(n)

    log("\n=== 4/8 安装 k3s ===")
    log(f"[{master.name}] master", 1)
    install_master(master, opts["k3s_version"])
    rc, token = master.sudo("cat /var/lib/rancher/k3s/server/node-token")
    if rc != 0 or len(token) < 40:
        raise RuntimeError("无法获取集群令牌")
    for w in workers:
        log(f"[{w.name}] worker", 1)
        join_worker(w, master.internal_ip, token.strip(), opts["k3s_version"])

    log("\n=== 5/8 部署项目与运行环境 ===")
    for n in all_nodes:
        log(f"[{n.name}]", 1)
        deploy_project(n, opts)

    log("\n=== 6/8 配置共享存储 ===")
    setup_shared_storage(master, workers, opts)

    log("\n=== 7/8 生成集群配置与镜像 ===")
    time.sleep(10)
    label_nodes(master, all_nodes)
    write_nodes_json(master, all_nodes, opts)
    build_and_distribute_image(master, workers, opts)

    log("\n=== 8/8 启动管理界面 ===")
    setup_ui(master, opts)

    log("\n=== 部署完成 ===")
    _, nodes_out = master.run("kubectl get nodes -o wide --no-headers")
    for line in nodes_out.splitlines():
        f = line.split()
        log(f"{f[0]:<16} {f[1]:<8} {f[5] if len(f) > 5 else ''}", 1)
    log(f"\n管理界面: http://{master.internal_ip}:{opts.get('ui_port', 8080)}")
    return 0


def cmd_status(master, workers, opts):
    resolve_ips([master] + workers)
    rc, out = master.run("kubectl get nodes -o wide --no-headers 2>/dev/null")
    if rc != 0:
        log("集群未就绪或 kubectl 不可用")
        return 1
    log("集群节点:")
    for line in out.splitlines():
        f = line.split()
        log(f"{f[0]:<16} {f[1]:<10} {f[5] if len(f) > 5 else ''}", 1)
    rc, jobs = master.run("kubectl get jobs -n video-pipeline --no-headers 2>/dev/null")
    log(f"\n任务: {len(jobs.splitlines()) if rc == 0 and jobs else 0} 个")
    rc, ui = master.run("systemctl is-active cluster-ui")
    log(f"管理界面: {ui} -> http://{master.internal_ip}:{opts.get('ui_port', 8080)}")
    return 0


def cmd_teardown(master, workers, opts):
    ans = input("将卸载所有节点的 k3s（不删除数据与项目），确认？(yes/N) ").strip().lower()
    if ans != "yes":
        return 1
    for w in workers:
        w.sudo("/usr/local/bin/k3s-agent-uninstall.sh >/dev/null 2>&1 || true", timeout=300)
        log(f"[{w.name}] 已卸载", 1)
    master.sudo("systemctl stop cluster-ui; systemctl disable cluster-ui", timeout=120)
    master.sudo("/usr/local/bin/k3s-uninstall.sh >/dev/null 2>&1 || true", timeout=300)
    log(f"[{master.name}] 已卸载", 1)
    return 0


def main():
    ap = argparse.ArgumentParser(description="k3s 视频处理集群一键部署")
    ap.add_argument("action", choices=["init", "check", "deploy", "status", "teardown"],
                    help="init=交互式生成配置并选定 master")
    ap.add_argument("-c", "--config", default=str(HERE / "cluster.json"))
    args = ap.parse_args()

    if args.action == "init":
        sys.exit(cmd_init(args.config))

    master, workers, opts = load_config(args.config)
    fn = {"check": cmd_check, "deploy": cmd_deploy,
          "status": cmd_status, "teardown": cmd_teardown}[args.action]
    try:
        code = fn(master, workers, opts)
    except Exception as e:
        log(f"\n出错: {e}")
        code = 1
    finally:
        for n in [master] + workers:
            n.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
