# split_shots — 视频镜头批量拆分工具

递归扫描文件夹中的各种格式视频，按镜头（shot）拆分成片段，自动去掉片头封面、logo、片尾，
按原视频名建立文件夹保存，并为每个视频生成 `scene.json` 记录完整的分段信息。

主要用于视频数据集的批量预处理。

## 功能

- **递归扫描**：遍历所有子目录，识别 mp4 / mkv / avi / mov / flv / wmv / webm / ts / rmvb 等 20 种常见格式
- **重复视频检测**：按内容指纹去重，文件名不同但内容相同的副本只处理一次（不删除源文件）
- **镜头检测**：三个检测器并用（内容突变 + 自适应 + 直方图），硬切、快速运镜、溶解叠化都能识别
- **片头片尾去除**：固定跳过首尾镜头，再自动识别并去掉残留的静态 logo、片名、结尾定帧画面
- **最短片段过滤**：短于指定时长的片段直接丢弃，避免产生大量碎片
- **质量与帧率控制**：统一输出帧率，CRF 控制画质；也支持无损流复制
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

## 使用

```bash
python split_shots.py <输入文件夹> <输出文件夹> [选项]
```

常用示例：

```bash
# 默认配置：25fps、CRF 16、最短片段 15 秒
python split_shots.py ./videos ./output

# 调整画质与帧率
python split_shots.py ./videos ./output --crf 18 --fps 30

# 保留更短的片段，并放宽镜头检测（切得更粗）
python split_shots.py ./videos ./output --min-clip 8 --threshold 27

# 后台运行，之后查看进度
python split_shots.py ./videos ./output --background
python split_shots.py ./videos ./output --status
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

### 编码质量

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--fps` | 25 | 输出帧率 |
| `--crf` | 16 | x264 质量，越小越好；16 约为视觉无损，18 很高，23 中等 |
| `--preset` | slow | x264 速度预设，越慢同画质下文件越小 |
| `--copy` | 关闭 | 流复制模式：不重编码、不改帧率，速度极快，但切点对齐关键帧会有零点几秒偏差 |

CRF 是按内容动态分配码率的，所以"维持原画质"指的是视觉质量。如果源视频本身码率很低，
CRF 16 的输出可能比源文件更大，这是正常的。

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

## 调参建议

遇到问题时按下表调整：

| 现象 | 处理方式 |
| --- | --- |
| 切得太碎 | 调大 `--threshold`（如 27）或 `--min-scene-len`（如 1.0） |
| 不同镜头被合并成一段 | 开启 `--aggressive`，或调小 `--threshold` |
| 片头 logo 没去干净 | 调大 `--motion-threshold`（如 2~3），或调大 `--skip-head` |
| 正片开头的静态空镜被误删 | 使用 `--no-auto-trim`，或调小 `--motion-threshold` |
| 有效片段被丢弃太多 | 调小 `--min-clip` |
| 处理速度太慢 | 使用 `--preset fast`，或用 `--copy` 跳过重编码 |

## 处理流程

1. 递归扫描输入目录，收集所有支持格式的视频
2. 按内容指纹去重，重复视频只处理一次
3. 逐个视频检测镜头边界，合并过短的碎片段
4. 只检测到单个镜头时，不拆分，整段按目标质量和帧率转码
5. 去掉固定数量的首尾镜头，再自动去除残留的静态 logo / 定帧画面
6. 丢弃短于 `--min-clip` 的片段
7. 用 ffmpeg 导出片段，写入 `scene.json`

## 依赖

- [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) — 镜头边界检测
- [OpenCV](https://opencv.org/) — 帧读取与运动量计算
- [ffmpeg](https://ffmpeg.org/) — 视频切割与转码
