#!/usr/bin/env python3
"""集群管理服务：查看节点状态、提交与监控分布式视频处理任务。

部署在 master 节点上，通过 kubectl 与 k3s 交互。
任务以 Kubernetes Job 形式下发，按节点算力自动分片。

启动:
    python3 server.py [--port 8080]
"""

import base64
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
NAMESPACE = "video-pipeline"
JOB_PREFIX = "vsplit"
# worker 容器以此身份运行，保证产出文件归宿主机普通用户所有
RUN_UID = int(os.environ.get("PIPELINE_UID", "1000"))
RUN_GID = int(os.environ.get("PIPELINE_GID", "1000"))

# 访问认证：设置了 PIPELINE_USER/PIPELINE_PASS 才启用（暴露公网时必须设置）
AUTH_USER = os.environ.get("PIPELINE_USER", "")
AUTH_PASS = os.environ.get("PIPELINE_PASS", "")

# 任务参数白名单。界面提交的参数会被拼进容器内执行的命令，
# 因此只放行已知选项，并禁止任何 shell 元字符，防止命令注入。
ALLOWED_FLAGS = {
    "--encoder", "--cq", "--crf", "--preset", "--fps", "--copy",
    "--jobs", "--threads", "--gpu", "--detector", "--aggressive",
    "--threshold", "--min-scene-len", "--min-clip",
    "--skip-head", "--skip-tail", "--motion-threshold", "--trim-window",
    "--no-auto-trim", "--no-dedup", "--skip-existing", "--skip-processed",
    "--match-by-name",
}
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._%-]+$")
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/-]*$")


def validate_options(text):
    """校验处理参数：只允许白名单选项，值不得含 shell 元字符。
    返回 (是否合法, 规范化后的参数串 或 错误说明)。"""
    if not text:
        return True, ""
    if re.search(r"[;&|`$><\n\r'\"\\]", text):
        return False, "参数含有非法字符（shell 元字符）"
    parts = text.split()
    out = []
    i = 0
    while i < len(parts):
        tok = parts[i]
        if not tok.startswith("--"):
            return False, f"无法识别的参数: {tok}"
        if tok not in ALLOWED_FLAGS:
            return False, f"不允许的参数: {tok}"
        out.append(tok)
        # 后面若跟着取值，一并校验
        if i + 1 < len(parts) and not parts[i + 1].startswith("--"):
            val = parts[i + 1]
            if not SAFE_VALUE.match(val):
                return False, f"参数 {tok} 的取值非法: {val}"
            out.append(val)
            i += 2
        else:
            i += 1
    return True, " ".join(out)


def validate_path(p):
    """输出/输入路径必须是绝对路径且不含可越权字符。"""
    if not p or not SAFE_PATH.match(p) or ".." in p:
        return False
    return True
# 各节点的处理能力权重（按 CPU 核数），用于分片时按算力分配
CACHE_TTL = 5.0

_cache = {}
_cache_lock = threading.Lock()


def kubectl(*args, timeout=30):
    """执行 kubectl 命令，返回 (成功, 输出)。"""
    cmd = ["kubectl"] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout if r.returncode == 0 else r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def cached(key, fn, ttl=CACHE_TTL):
    """短期缓存，避免界面刷新时频繁调用 kubectl。"""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def parse_quantity(q):
    """把 k8s 的资源量（如 197460848Ki、48、62Gi）转成数值。"""
    if q is None:
        return 0
    s = str(q)
    m = re.match(r"^([0-9.]+)([A-Za-z]*)$", s)
    if not m:
        return 0
    val, unit = float(m.group(1)), m.group(2)
    mult = {"": 1, "m": 0.001, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3,
            "Ti": 1024**4, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
    return val * mult.get(unit, 1)


def get_nodes():
    """收集节点清单及其容量、状态、标签。"""
    ok, out = kubectl("get", "nodes", "-o", "json")
    if not ok:
        return {"error": out, "nodes": []}
    try:
        data = json.loads(out)
    except ValueError as e:
        return {"error": f"解析失败: {e}", "nodes": []}

    # 实时用量（metrics-server 提供，可能尚未就绪）
    usage = {}
    ok_m, out_m = kubectl("top", "nodes", "--no-headers")
    if ok_m:
        for line in out_m.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                usage[parts[0]] = {"cpu_pct": parts[2].rstrip("%"),
                                   "mem_pct": parts[4].rstrip("%")}

    nodes = []
    for item in data.get("items", []):
        meta, status = item.get("metadata", {}), item.get("status", {})
        name = meta.get("name", "?")
        labels = meta.get("labels", {})
        conds = {c["type"]: c["status"] for c in status.get("conditions", [])}
        cap = status.get("capacity", {})
        addrs = {a["type"]: a["address"] for a in status.get("addresses", [])}
        taints = [t.get("key") for t in item.get("spec", {}).get("taints", [])]
        nodes.append({
            "name": name,
            "ready": conds.get("Ready") == "True",
            "ip": addrs.get("InternalIP", ""),
            "cpu": int(parse_quantity(cap.get("cpu"))),
            "mem_gb": round(parse_quantity(cap.get("memory")) / 1024**3, 1),
            "gpu_count": labels.get("gpu-count", "?"),
            "gpu_model": labels.get("gpu-model", ""),
            "role": labels.get("role", "worker"),
            "taints": taints,
            "schedulable": not any("disk-pressure" in t or "unschedulable" in t
                                   for t in taints),
            "usage": usage.get(name, {}),
        })
    nodes.sort(key=lambda n: (n["role"] != "master", n["name"]))
    return {"nodes": nodes}


def get_jobs():
    """列出本系统提交的任务及其进度。"""
    ok, out = kubectl("get", "jobs", "-n", NAMESPACE, "-o", "json")
    if not ok:
        return {"jobs": []}
    try:
        data = json.loads(out)
    except ValueError:
        return {"jobs": []}

    # Pod 与节点的对应关系，用于展示每个分片跑在哪台机器
    pods_by_job = {}
    ok_p, out_p = kubectl("get", "pods", "-n", NAMESPACE, "-o", "json")
    if ok_p:
        try:
            for p in json.loads(out_p).get("items", []):
                jn = p.get("metadata", {}).get("labels", {}).get("job-name")
                if not jn:
                    continue
                pods_by_job.setdefault(jn, []).append({
                    "name": p["metadata"]["name"],
                    "node": p.get("spec", {}).get("nodeName", "-"),
                    "phase": p.get("status", {}).get("phase", "?"),
                })
        except ValueError:
            pass

    jobs = []
    for item in data.get("items", []):
        meta, spec, st = item["metadata"], item.get("spec", {}), item.get("status", {})
        name = meta["name"]
        ann = meta.get("annotations", {})
        jobs.append({
            "name": name,
            "created": meta.get("creationTimestamp", ""),
            "parallelism": spec.get("parallelism", 1),
            "completions": spec.get("completions", 1),
            "active": st.get("active", 0),
            "succeeded": st.get("succeeded", 0),
            "failed": st.get("failed", 0),
            "input": ann.get("pipeline/input", ""),
            "output": ann.get("pipeline/output", ""),
            "options": ann.get("pipeline/options", ""),
            "pods": sorted(pods_by_job.get(name, []), key=lambda p: p["name"]),
        })
    jobs.sort(key=lambda j: j["created"], reverse=True)
    return {"jobs": jobs}


def build_job_yaml(cfg, shard_total, nodes):
    """生成 Indexed Job：每个分片一个 Pod，用 JOB_COMPLETION_INDEX 决定处理哪一片。"""
    stamp = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
    name = f"{JOB_PREFIX}-{stamp}"
    opts = cfg.get("options", "").strip()
    image = cfg.get("image", "video-pipeline:latest")
    inp, outp = cfg["input"], cfg["output"]

    # 工作脚本用 YAML 块标量嵌入，避免脚本里的引号破坏 YAML 结构
    script_lines = [
        "set -e",
        "IDX=${JOB_COMPLETION_INDEX:-0}",
        f'echo "分片 $IDX/{shard_total} 在 $(hostname) 启动"',
        f"exec python3 /project/split_shots.py '{inp}' '{outp}' "
        f"--shard $IDX/{shard_total} {opts}",
    ]
    script = "\n".join(" " * 12 + ln for ln in script_lines)
    return name, f"""apiVersion: batch/v1
kind: Job
metadata:
  name: {name}
  namespace: {NAMESPACE}
  annotations:
    pipeline/input: "{inp}"
    pipeline/output: "{outp}"
    pipeline/options: "{opts}"
spec:
  completionMode: Indexed
  completions: {shard_total}
  parallelism: {shard_total}
  backoffLimit: 2
  template:
    metadata:
      labels:
        app: video-pipeline
    spec:
      restartPolicy: Never
      runtimeClassName: nvidia
      # 以宿主机的 ubuntu 用户身份运行，产出文件才归普通用户所有，
      # 否则输出全是 root 属主，日常管理都要 sudo
      securityContext:
        runAsUser: {RUN_UID}
        runAsGroup: {RUN_GID}
        fsGroup: {RUN_GID}
      # 优先把各分片分散到不同节点，充分利用整个集群的算力
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            podAffinityTerm:
              topologyKey: kubernetes.io/hostname
              labelSelector:
                matchLabels:
                  job-name: {name}
      containers:
      - name: worker
        image: {image}
        imagePullPolicy: IfNotPresent
        command: ["bash", "-c"]
        args:
          - |
{script}
        env:
        - name: NVIDIA_VISIBLE_DEVICES
          value: "all"
        - name: NVIDIA_DRIVER_CAPABILITIES
          value: "compute,utility,video"
        volumeMounts:
        - name: project
          mountPath: /project
        - name: data
          mountPath: /data
      volumes:
      - name: project
        hostPath:
          path: /mnt/hd/Project/dataprocess
          type: Directory
      - name: data
        hostPath:
          path: /mnt/cluster_data
          type: DirectoryOrCreate
"""


def submit_job(cfg):
    """校验参数并提交任务。"""
    for k in ("input", "output"):
        if not cfg.get(k):
            return False, f"缺少参数: {k}"
        if not validate_path(cfg[k]):
            return False, f"{k} 路径非法（需为绝对路径且不含特殊字符）"
    ok_opt, opts_or_err = validate_options(cfg.get("options", "").strip())
    if not ok_opt:
        return False, opts_or_err
    cfg = dict(cfg, options=opts_or_err)
    info = get_nodes()
    ready = [n for n in info["nodes"] if n["ready"] and n["schedulable"]]
    if not ready:
        return False, "没有可调度的节点"
    try:
        shards = int(cfg.get("shards") or len(ready))
    except ValueError:
        return False, "分片数必须是整数"
    shards = max(1, min(shards, 64))

    kubectl("create", "namespace", NAMESPACE)
    name, yaml = build_job_yaml(cfg, shards, ready)
    tmp = Path("/tmp") / f"{name}.yaml"
    tmp.write_text(yaml, encoding="utf-8")
    ok, out = kubectl("apply", "-f", str(tmp))
    if not ok:
        return False, out
    return True, f"任务 {name} 已提交，分为 {shards} 片"


def delete_job(name):
    if not re.fullmatch(r"[a-z0-9-]+", name or ""):
        return False, "任务名非法"
    ok, out = kubectl("delete", "job", name, "-n", NAMESPACE)
    return ok, out


def job_logs(name, tail=200):
    if not re.fullmatch(r"[a-z0-9-]+", name or ""):
        return "任务名非法"
    ok, out = kubectl("logs", f"job/{name}", "-n", NAMESPACE,
                      f"--tail={int(tail)}", "--all-containers", timeout=25)
    return out if ok else f"（暂无日志）{out}"


def cluster_storage():
    """共享存储用量。"""
    path = "/mnt/hd/Project/cluster_data"
    try:
        u = shutil.disk_usage(path)
        return {"path": path, "total_gb": round(u.total / 1024**3),
                "free_gb": round(u.free / 1024**3),
                "used_pct": round(100 * u.used / u.total)}
    except OSError as e:
        return {"path": path, "error": str(e)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静默访问日志

    def authorized(self):
        """未配置账号密码时不启用认证；配置了则要求 HTTP Basic。"""
        if not AUTH_USER:
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(hdr[6:]).decode("utf-8")
            user, _, pwd = raw.partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        # 用固定时间比较，避免通过响应时间猜测密码
        return (hmac.compare_digest(user, AUTH_USER)
                and hmac.compare_digest(pwd, AUTH_PASS))

    def require_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Video Cluster"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.authorized():
            return self.require_auth()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            html = (HERE / "index.html").read_text(encoding="utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/api/nodes":
            return self._send(200, json.dumps(cached("nodes", get_nodes),
                                              ensure_ascii=False))
        if u.path == "/api/jobs":
            return self._send(200, json.dumps(cached("jobs", get_jobs, 3),
                                              ensure_ascii=False))
        if u.path == "/api/storage":
            return self._send(200, json.dumps(cluster_storage(), ensure_ascii=False))
        if u.path == "/api/logs":
            name = (q.get("job") or [""])[0]
            return self._send(200, json.dumps({"logs": job_logs(name)},
                                              ensure_ascii=False))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if not self.authorized():
            return self.require_auth()
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, json.dumps({"ok": False, "msg": "请求体不是合法 JSON"}))

        if u.path == "/api/submit":
            ok, msg = submit_job(payload)
        elif u.path == "/api/delete":
            ok, msg = delete_job(payload.get("job"))
        else:
            return self._send(404, json.dumps({"ok": False, "msg": "not found"}))
        with _cache_lock:
            _cache.pop("jobs", None)
        return self._send(200, json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False))


def main():
    import argparse
    ap = argparse.ArgumentParser(description="k3s 视频处理集群管理界面")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"集群管理界面已启动: http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
