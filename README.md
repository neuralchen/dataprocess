# split_shots — 视频镜头批量拆分工具

递归扫描文件夹中的各种格式视频，按镜头（shot）拆分成片段，自动去掉片头封面、logo、片尾，
按原视频名建立文件夹保存，并为每个视频生成 `scene.json` 记录完整的分段信息。

主要用于视频数据集的批量预处理。

## 目录

- [功能](#功能)
- [安装](#安装)
- [快速开始](#快速开始)
- [典型使用流程](#典型使用流程)
- [输出结构](#输出结构)
- [参数说明](#参数说明)
- [后台运行](#后台运行)
- [性能与资源占用](#性能与资源占用)
- [调参建议](#调参建议)
- [常见问题](#常见问题)

## 功能

- **递归扫描**：遍历所有子目录，识别 mp4 / mkv / avi / mov / flv / wmv / webm / ts / rmvb 等 20 种常见格式
- **重复视频检测**：按内容指纹去重，文件名不同但内容相同的副本只处理一次（不删除源文件）
- **已处理结果扫描**：可指定多个历史结果目录，自动识别哪些视频处理过并跳过，支持增量续跑
- **镜头检测**：三个检测器并用（内容突变 + 自适应 + 直方图），硬切、快速运镜、溶解叠化都能识别
- **片头片尾去除**：固定跳过首尾镜头，再自动识别并去掉残留的静态 logo、片名、结尾定帧画面
- **最短片段过滤**：短于指定时长的片段直接丢弃，避免产生大量碎片
- **质量与帧率控制**：统一输出帧率，CRF 控制画质；也支持无损流复制
- **并行处理**：多视频并发处理，自动按 CPU 核心数分配任务数与编码线程
- **GPU 硬件编码**：支持 NVIDIA NVENC（CUDA）和 macOS VideoToolbox，大幅降低 CPU 占用
- **目录结构保留**：输出完整复刻输入的目录层级
- **分段记录**：每个视频输出 `scene.json`，记录所有检测到的场景及导出路径
- **后台运行**：一键转后台执行，支持查看进度与停止，Windows / Linux / macOS 通用

## 安装

需要 Python 3.9+ 和 [ffmpeg](https://ffmpeg.org/)。

**Ubuntu / macOS**

```bash
bash setup_venv.sh
source .venv/bin/activate
```

**Windows**

```bat
setup_venv.bat
.venv\Scripts\activate
```

脚本会创建虚拟环境 `.venv`、安装依赖，并检查 ffmpeg 是否可用（缺失时给出对应平台的安装命令）。

ffmpeg 手动安装：

| 平台 | 命令 |
| --- | --- |
| Ubuntu | `sudo apt install ffmpeg` |
| macOS | `brew install ffmpeg` |
| Windows | `winget install Gyan.FFmpeg` |

## 快速开始

```bash
python split_shots.py <输入文件夹> <输出文件夹> [选项]
```

两个位置参数都是必填的。最简单的一次完整运行：

```bash
python split_shots.py ./videos /data/output --preset veryfast --crf 20
```

它会递归扫描 `./videos` 下的所有视频，逐个检测镜头、去掉片头片尾、拆成片段，
按原目录结构写入 `/data/output`，并为每个视频生成 `scene.json`。

> 建议加上 `--preset veryfast --crf 20`：默认的 `slow`/CRF 16 是画质优先的重负载配置，
> 批量处理时会占满 CPU 且输出体积很大，详见[性能与资源占用](#性能与资源占用)。

常用命令速查：

| 目的 | 命令 |
| --- | --- |
| 标准批量处理 | `python split_shots.py ./videos /data/out --preset veryfast --crf 20` |
| 只看会处理哪些视频 | `python split_shots.py ./videos /data/out --scan-only` |
| 处理新增素材（跳过旧的） | `python split_shots.py ./videos /data/out --skip-processed` |
| 快速粗切（不重编码） | `python split_shots.py ./videos /data/out --copy` |
| 后台跑 + 查看进度 | `--background` / `--status` / `--stop` |

## 典型使用流程

### 首次批量处理

先用 `--scan-only` 确认扫描到的视频数量符合预期，再正式跑：

```bash
# 1. 先看看会处理多少视频
python split_shots.py ./videos /data/output --scan-only

# 2. 拿一小批素材试参数，确认切分效果和画质
python split_shots.py ./sample /data/test --preset veryfast --crf 20

# 3. 确认无误后转后台跑全量
python split_shots.py ./videos /data/output --preset veryfast --crf 20 --background

# 4. 随时查看进度
python split_shots.py ./videos /data/output --status
```

第 2 步很重要：不同来源的素材，合适的检测阈值和片段时长差别很大，
先用小批量确认再跑全量能省下大量返工时间。参考[调参建议](#调参建议)。

### 素材陆续新增（增量处理）

素材库不断补充新视频时，加 `--skip-processed` 即可只处理新增部分，
已处理过的视频连镜头检测都不会做：

```bash
python split_shots.py ./videos /data/output --skip-processed --preset veryfast --crf 20
```

如果历史结果分散在多个批次目录里，用 `--processed-dir` 一并纳入判断：

```bash
python split_shots.py ./videos /data/output_v3 \
    --processed-dir /data/output_v1 /data/output_v2 \
    --preset veryfast --crf 20
```

### 任务中断后继续

任务被中断（手动停止、断电、报错）后，加 `--skip-existing` 重跑，已切好的片段不会重做：

```bash
python split_shots.py ./videos /data/output --skip-existing --preset veryfast --crf 20
```

已经完整处理完的视频，配合 `--skip-processed` 可以整个跳过，比 `--skip-existing` 更省时间：

```bash
python split_shots.py ./videos /data/output --skip-processed --skip-existing
```

### 只要粗切，不在乎精确切点

`--copy` 直接复制视频流，不重编码，速度快几十倍且完全无损，
代价是切点会对齐到关键帧（零点几秒偏差），且不会统一帧率：

```bash
python split_shots.py ./videos /data/output --copy
```

## 输出结构

输出目录复刻输入目录的层级，每个视频对应一个以其文件名命名的文件夹：

```
输入:                          输出:
videos/                        output/
├── a.mp4                      ├── a/
└── 剧集/                      │   ├── a_scene_0002.mp4
    └── b.mkv                  │   ├── a_scene_0005.mp4
                               │   └── scene.json
                               └── 剧集/
                                   └── b/
                                       ├── b_scene_0001.mp4
                                       └── scene.json
```

片段文件名中的编号是**场景索引**，与 `scene.json` 里的 `scene_index` 一一对应。编号不连续是正常的
（被去掉的片头、logo、过短片段占用了中间的索引）。

### scene.json

记录该视频**全部**检测到的场景，包括被丢弃的。被丢弃场景的 `output_path` 为 `null`，
下游筛选 `output_path != null` 即可拿到所有实际导出的片段。

```json
{
  "source_video": "/data/videos/example.mp4",
  "source_size": 104857600,
  "source_fingerprint": "3f2a...c81d",
  "detector": "content+adaptive+histogram",
  "aggressive": false,
  "threshold": 24.0,
  "min_scene_len": 0.6,
  "min_scene_seconds": 15.0,
  "split_mode": "encode",
  "fps": 25,
  "crf": 16,
  "preset": "slow",
  "skip_head": 1,
  "skip_tail": 1,
  "detected_at": "2026-08-01T03:21:32.843130Z",
  "scene_count": 6,
  "scenes": [
    {
      "scene_index": 0,
      "start_time": 0.0,
      "end_time": 1.52,
      "duration": 1.52,
      "output_path": null,
      "thumbnail": null
    },
    {
      "scene_index": 2,
      "start_time": 3.04,
      "end_time": 25.04,
      "duration": 22.0,
      "output_path": "/data/output/example/example_scene_0002.mp4",
      "thumbnail": null
    }
  ]
}
```

## 参数说明

### 镜头检测

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--threshold` | 24.0 | 内容检测阈值，越小越敏感、切得越碎（PySceneDetect 常规默认 27） |
| `--min-scene-len` | 0.6 | 最短镜头时长（秒），短于此的段会并入相邻镜头 |
| `--aggressive` | 关闭 | 激进模式，各检测器灵敏度大幅调高，几乎不漏切但切得更碎 |

### 片头片尾去除

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--skip-head` | 1 | 固定去掉开头的镜头数 |
| `--skip-tail` | 1 | 固定去掉结尾的镜头数 |
| `--motion-threshold` | 1.0 | 静态镜头判定阈值，运动量低于此值视为 logo / 定帧 |
| `--trim-window` | 15.0 | 自动静态修剪只作用于首尾各这么多秒内 |
| `--no-auto-trim` | 关闭 | 关闭静态镜头自动修剪 |

固定跳过之后，脚本会继续检查首尾窗口内的镜头：几乎静止的画面（厂牌 logo、片名、结尾定帧）
会被继续丢弃，直到遇到有实际运动的内容镜头为止。

### 片段筛选

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--min-clip` | 15.0 | 输出片段的最短时长（秒），短于此的直接丢弃；单镜头视频总时长不足时整个跳过 |

### 并行与硬件加速

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-j` / `--jobs` | 0（自动） | 并行处理的视频数；自动值为 `核心数-2`，上限 16 |
| `--threads` | 0（自动） | 单个 ffmpeg 的编码线程数；自动值为 `核心数 / jobs`，最小 2 |
| `--encoder` | cpu | `cpu` / `nvenc` / `nvenc-hevc` / `videotoolbox` / `auto` |
| `--cq` | 同 `--crf` | 硬件编码的质量值，越小质量越高 |

**并行策略**：多进程少线程明显优于单进程多线程。x264 在片段较短时线程扩展性很差，
而每个任务都有进程启动、seek、镜头检测等非满载阶段，因此 `jobs × threads` 略微超过
核心数反而利用率更高。自动值已按实测调优，一般无需手动指定。

**GPU 加速**：`--encoder nvenc` 使用 NVIDIA 显卡编码（需要 CUDA 驱动和带 NVENC 的 ffmpeg），
同时启用 CUDA 硬件解码。`--encoder auto` 会自动检测可用的硬件编码器，检测方式是实际试跑一次编码，
因此不会出现"编译了但驱动跑不通"的误判。

```bash
# 自动选择最快的可用编码器
python split_shots.py ./videos /data/out --encoder auto

# 显式使用 NVIDIA GPU，质量值 20
python split_shots.py ./videos /data/out --encoder nvenc --cq 20

# 手动指定并行度（长视频较少时可调低）
python split_shots.py ./videos /data/out --jobs 4
```

> GPU 编码的画质与同数值的 x264 CRF 不完全等价，体积通常略大。
> **同一批数据集建议固定用同一种编码器**，避免不同批次画质特征不一致。

### 编码质量

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--fps` | 25 | 输出帧率 |
| `--crf` | 16 | x264 质量，越小越好；16 约为视觉无损，18 很高，23 中等 |
| `--preset` | slow | x264 速度预设，越慢同画质下文件越小 |
| `--copy` | 关闭 | 流复制模式：不重编码、不改帧率，速度极快，但切点对齐关键帧会有零点几秒偏差 |

CRF 是按内容动态分配码率的，所以"维持原画质"指的是视觉质量。如果源视频本身码率很低，
CRF 16 的输出可能比源文件更大，这是正常的。

> ⚠️ **默认的 `--preset slow --crf 16` 是画质优先的重负载配置**，会占满 CPU、产生较大的输出文件。
> 批量处理时建议改用 `--preset veryfast --crf 20`，详见下方[性能与资源占用](#性能与资源占用)。

### 已处理结果扫描

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--skip-processed` | 关闭 | 跳过已处理过的视频（扫描输出目录中的 `scene.json` 判断） |
| `--processed-dir` | 无 | 额外扫描的历史结果目录，可指定多个；隐含开启 `--skip-processed` |
| `--match-by-name` | 关闭 | 判定放宽到仅文件名匹配 |
| `--scan-only` | 关闭 | 只扫描报告哪些已处理 / 待处理，不做任何转码 |

用于增量处理：素材陆续新增时，只处理新素材；或者结果分散在多个批次目录中，跨目录识别。

```bash
# 只跳过本次输出目录里已处理的
python split_shots.py ./videos ./output --skip-processed

# 跨多个历史结果目录识别（结果分散在不同批次时）
python split_shots.py ./videos ./output_new --processed-dir ./output_v1 ./output_v2

# 先看看哪些需要处理，不实际转码
python split_shots.py ./videos ./output --scan-only --processed-dir ./output_v1
```

判定依据按代价从低到高依次尝试，命中任意一条即视为已处理：

| 依据 | 适用场景 |
| --- | --- |
| 路径匹配 | 素材位置没变 |
| 文件名 + 大小匹配 | 素材被移动过，或换了机器（路径不同） |
| 内容指纹匹配 | 素材被改名或复制过 |
| 文件名匹配 | 仅在 `--match-by-name` 下启用；素材被重新压制过（大小和内容都变了） |

`scene.json` 只在视频分析完成后写入，因此它的存在即代表该视频已处理完毕。
**分析后没有产出任何合格片段的视频也会写 `scene.json`**（`scenes` 里全部 `output_path` 为 `null`），
这样下次扫描时不会重复分析。

### 与 `--skip-existing` 的区别

两者粒度不同，可以配合使用：

| 参数 | 粒度 | 作用 |
| --- | --- | --- |
| `--skip-processed` | 视频级 | 已处理过的视频整个跳过，**连镜头检测都不做**，最省时间 |
| `--skip-existing` | 片段级 | 仍会重新检测镜头，只跳过已存在的片段文件；用于任务中断后续跑 |

### 其它

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--no-dedup` | 关闭 | 跳过重复视频检测 |
| `--skip-existing` | 关闭 | 跳过已存在的输出片段，用于断点续跑 |

## 后台运行

```bash
# 转入后台，日志写到输出目录下的 split_shots_<时间戳>.log
python split_shots.py ./videos ./output --background

# 查看运行状态和最新日志
python split_shots.py ./videos ./output --status

# 停止后台任务
python split_shots.py ./videos ./output --stop
```

后台进程与终端分离，关闭终端不影响运行。PID 记录在输出目录的 `.split_shots.pid`，
任务结束后自动清理。中途停止后，用 `--skip-existing` 重新运行即可从断点继续。

> `--stop` 会连同全部工作进程和 ffmpeg 子进程一起结束（Windows 用 `taskkill /T`，
> Linux / macOS 杀整个进程组），不会留下占满 CPU 的孤儿进程。
>
> 另外重复运行的保护是**按输出目录**判断的，换一个输出目录再启动不会被拦截。Windows 后台任务不显示窗口，
> 启动新任务前建议先用 `--status` 确认上一批已经结束。

## 性能与资源占用

视频转码本身是重负载操作，批量处理前请了解实际开销。以下为 1080p 素材、10 核 CPU 上的实测值：

| 环节 | 资源占用 |
| --- | --- |
| 镜头检测 | 约 157 MB 内存，速度很快，不是瓶颈 |
| ffmpeg 编码（默认参数） | **占满约 8 个核心**，单进程峰值内存约 900 MB（4K 素材会达到 3~4 GB） |
| 输出体积（CRF 16） | 与源文件相当甚至更大 |

### 实测加速效果

12 个 720p 视频、共 36 个片段、10 核 CPU、`--preset medium --crf 18`：

| 配置 | 耗时 | CPU 占用 | 相对串行 |
| --- | --- | --- | --- |
| CPU 串行（`--jobs 1`） | 31.3 s | 6.6 核 | 1.00× |
| CPU 并行（自动） | 26.0 s | 8.7 核 | **1.20×** |
| GPU 硬件编码 | 16.7 s | **2.3 核** | **1.87×** |

CPU 并行的收益有限，因为 x264 本身已经能吃满多核；核心数越多的服务器收益越明显。
**GPU 编码才是数量级的差异**——不仅快近一倍，CPU 占用还从 8.7 核降到 2.3 核，
机器仍可正常做别的事。有 NVIDIA 显卡的话强烈建议加 `--encoder nvenc`。

内存方面注意：并行任务数越多内存占用越高。处理 4K 素材时单个 ffmpeg 就要 3~4 GB，
`--jobs 16` 可能需要 50 GB 以上，此时应手动调低 `--jobs`。

由此带来的三个实际风险：

- **系统卡顿**：x264 默认使用全部逻辑核心，且脚本未限制线程数。批量任务会让 CPU 长时间满载，
  Windows 下界面可能失去响应
- **磁盘写满**：CRF 16 输出体积大，整批处理容易占满磁盘，**务必把输出目录指定到非系统盘**
- **内存压力**：处理 4K 素材时单个 ffmpeg 就要 3~4 GB，低内存机器需要留意

**批量处理的推荐配置**：

```bash
python split_shots.py ./videos /data/output --preset veryfast --crf 20
```

`veryfast` 比 `slow` 快 5~10 倍，CRF 20 画质依然很高但体积明显更小。
如果只是粗切、不需要统一帧率，用 `--copy` 完全跳过重编码，速度最快且无损。

## 调参建议

遇到问题时按下表调整：

| 现象 | 处理方式 |
| --- | --- |
| 切得太碎 | 调大 `--threshold`（如 27）或 `--min-scene-len`（如 1.0） |
| 不同镜头被合并成一段 | 开启 `--aggressive`，或调小 `--threshold` |
| 片头 logo 没去干净 | 调大 `--motion-threshold`（如 2~3），或调大 `--skip-head` |
| 正片开头的静态空镜被误删 | 使用 `--no-auto-trim`，或调小 `--motion-threshold` |
| 有效片段被丢弃太多 | 调小 `--min-clip` |
| 处理速度太慢 | 优先用 `--encoder nvenc`（有 N 卡时）；其次 `--preset veryfast`，或 `--copy` 跳过重编码 |
| 电脑卡顿、界面无响应 | 用 `--encoder nvenc` 把编码交给 GPU；或调低 `--jobs` |
| 内存不够 / OOM | 调低 `--jobs`（4K 素材建议 4 以下） |
| 磁盘被写满 | 输出目录指定到非系统盘；调大 `--crf`（如 20~23）减小体积 |

## 处理流程

1. 递归扫描输入目录，收集所有支持格式的视频
2. 扫描历史结果目录，跳过已处理过的视频（需开启 `--skip-processed` 或指定 `--processed-dir`）
3. 按内容指纹去重，重复视频只处理一次
4. 逐个视频检测镜头边界，合并过短的碎片段
5. 只检测到单个镜头时，不拆分，整段按目标质量和帧率转码
6. 去掉固定数量的首尾镜头，再自动去除残留的静态 logo / 定帧画面
7. 丢弃短于 `--min-clip` 的片段
8. 用 ffmpeg 导出片段，写入 `scene.json`

## 常见问题

**为什么某个视频的输出文件夹是空的，只有一个 `scene.json`？**

说明该视频分析过了，但没有片段满足条件——通常是所有镜头都短于 `--min-clip`（默认 15 秒），
或者去掉片头片尾后没有剩余镜头。打开 `scene.json` 看 `scenes` 里各段的 `duration` 即可确认。
需要保留更短的片段就调小 `--min-clip`。

**片段编号为什么不连续？**

编号是**场景索引**，对应 `scene.json` 里的 `scene_index`。被去掉的片头、logo、过短片段
占用了中间的索引，所以编号会跳号，这是正常的。

**处理到一半报错了，重跑会重复做吗？**

加 `--skip-existing` 就不会，已存在的片段文件会被跳过。已完整处理完的视频可以再加
`--skip-processed` 整个跳过，连镜头检测都省了。

**输出文件比原视频还大？**

CRF 是按内容动态分配码率的，源视频码率很低时（比如网上下载的压缩视频），
CRF 16 的输出确实可能更大。调大 `--crf`（如 20~23）即可。

**能识别重新压制过的重复视频吗？**

不能。去重和已处理判定都基于内容指纹，只能识别字节级相同的副本（复制、改名、移动过的文件）。
同一视频被重新编码后指纹就变了。文件名没变的话可以用 `--match-by-name` 放宽判定。

**Windows 上跑着跑着电脑卡死？**

默认的 `--preset slow --crf 16` 会占满 CPU。有 NVIDIA 显卡就加 `--encoder nvenc` 把编码交给 GPU，
CPU 占用可降到 1/4；没有的话改用 `--preset veryfast --crf 20` 并调低 `--jobs`。
输出目录记得指定到非系统盘。详见[性能与资源占用](#性能与资源占用)。

## 依赖

- [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) — 镜头边界检测
- [OpenCV](https://opencv.org/) — 帧读取与运动量计算
- [ffmpeg](https://ffmpeg.org/) — 视频切割与转码
