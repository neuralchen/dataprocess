# k3s 视频处理集群

把局域网内多台机器组成 k3s 集群，通过 master 上的 Web 界面统一管理节点、下发分布式视频处理任务，
任务按分片自动调度到各节点并行执行。

## 集群构成

| 角色 | SSH 端口 | 内网 IP | CPU | 内存 | GPU |
| --- | --- | --- | --- | --- | --- |
| master | 10094 | 192.168.1.218 | 48 | 188 GB | 2×4090 + 2×5090 |
| worker | 10095 | 192.168.1.221 | 48 | 125 GB | 4×5090 |
| worker | 10082 | 192.168.1.206 | 16 | 62 GB | 2×4090 |
| worker | 10052 | 192.168.1.219 | 16 | 62 GB | 2×4090 |

合计 **128 核、12 张 GPU**，千兆内网互联。

## 管理界面

浏览器打开 **http://192.168.1.218:8080**（局域网内访问）。

界面提供三部分：

- **节点**：各节点的就绪状态、CPU / 内存实时占用、GPU 型号数量、调度污点
- **提交任务**：填写输入输出目录、分片数与处理参数，一键下发到整个集群
- **任务**：每个任务的完成进度、各分片落在哪台机器、实时日志、删除任务

界面以 systemd 服务常驻，开机自启：

```bash
sudo systemctl status cluster-ui     # 查看状态
sudo systemctl restart cluster-ui    # 重启
journalctl -u cluster-ui -f          # 看服务日志
```

## 共享存储

master 的 9.1T 大盘通过 NFS 导出，四台机器都以 **`/mnt/cluster_data`** 挂载，路径完全一致。
容器内固定映射为 `/data`，所以界面里填的路径应当是：

| 界面填写 | 实际位置 |
| --- | --- |
| `/data/input` | `/mnt/cluster_data/input` |
| `/data/output` | `/mnt/cluster_data/output` |

把待处理素材放进 `/mnt/cluster_data/input`（在任意一台机器上放都行，NFS 共享），
产出会写回 `/mnt/cluster_data/output`，属主是 `ubuntu`，无需 sudo 即可管理。

## 任务是怎么分配的

任务以 Kubernetes **Indexed Job** 形式下发：分成 N 片就创建 N 个 Pod，
每个 Pod 从环境变量拿到自己的分片号，执行

```bash
python3 /project/split_shots.py <输入> <输出> --shard i/N <其它参数>
```

分片依据是**视频路径的哈希取模**——各节点不需要任何通信就能算出同一份划分，天然不重不漏。
Pod 之间设置了反亲和，调度器会优先把分片打散到不同节点。

失败的分片会自动重试（最多 2 次）。由于处理本身是幂等的（已完成的视频会写 `scene.json`，
配合 `--skip-processed` 会被跳过），重跑不会重复劳动。

## 典型用法

```bash
# 1. 把素材放到共享目录
cp -r /path/to/videos/* /mnt/cluster_data/input/

# 2. 浏览器打开 http://192.168.1.218:8080，填写：
#    输入 /data/input   输出 /data/output   分片数 4
#    参数 --encoder nvenc --cq 23 --jobs 8 --min-clip 15 --skip-processed

# 3. 在界面上看进度，或命令行查看
kubectl get pods -n video-pipeline -o wide
kubectl logs -n video-pipeline job/<任务名> --all-containers
```

分片数建议设为节点数或其整数倍。单节点内部的并行度由 `--jobs` 控制，
NVENC 的并发编码会话上限是**整机 8 路**（多卡也不叠加），所以 `--jobs` 不要超过 8。

## Worker 镜像

镜像 `video-pipeline:latest` 只包含运行环境（ffmpeg + PySceneDetect），
**项目代码通过 hostPath 从宿主机的 `/mnt/hd/Project/dataprocess` 挂载进容器**。
这意味着改完代码 `git pull` 即可生效，不必重建镜像。

需要重建时（比如换了依赖）：

```bash
cd /mnt/hd/Project/dataprocess/cluster
docker build -t video-pipeline:latest .
docker save video-pipeline:latest -o /mnt/cluster_data/video-pipeline.tar
sudo k3s ctr images import /mnt/cluster_data/video-pipeline.tar        # master
# 其它节点各执行一次同样的 import
```

## 运维要点

**磁盘布局**：所有 k3s 数据都通过 bind mount 落在各机的 `/mnt/hd`，不占根分区。
master 根分区只剩 16G（`/home` 占了 580G 用户数据），因此把 kubelet 的驱逐阈值调到了 1%，
否则节点会被打上 `disk-pressure` 污点而拒绝调度。

**新增节点**：

```bash
# 在新机器上（先确保有 /mnt/hd 大盘、nvidia 驱动、ffmpeg）
sudo mkdir -p /mnt/hd/k3s /var/lib/rancher
sudo mount --bind /mnt/hd/k3s /var/lib/rancher
curl -sfL https://get.k3s.io | sudo env \
  K3S_URL=https://192.168.1.218:6443 \
  K3S_TOKEN=<master 的 /var/lib/rancher/k3s/server/node-token> \
  INSTALL_K3S_EXEC='agent --node-ip <本机内网IP>' sh -
sudo mount -t nfs 192.168.1.218:/mnt/hd/Project/cluster_data /mnt/cluster_data
```

加入后在界面上会自动出现。记得给节点打标签以便界面显示 GPU 信息：

```bash
kubectl label node <节点名> gpu-count=2 gpu-model=4090x2 role=worker --overwrite
```

**常见问题**：

| 现象 | 原因与处理 |
| --- | --- |
| Pod 一直 Pending | 节点有污点（多为 `disk-pressure`），清理磁盘或调整驱逐阈值 |
| 容器内找不到 GPU | 检查 `runtimeClassName: nvidia` 与 `NVIDIA_VISIBLE_DEVICES` 环境变量 |
| 分片全挤在一个节点 | 反亲和是"优先"而非强制，节点资源差异大时可能集中，可增加分片数 |
| 产出文件属主是 root | 检查 Job 的 `securityContext` 是否设置了 `runAsUser: 1000` |
