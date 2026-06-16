# stroi-studio

[English](README.md) · **简体中文**

围绕 [stROI](https://github.com/Tianqi-Ma/stROI) ROI 工作流的本地网页 GUI，用于数字病理。
它把病理学家用 marker 笔标注好的全玻片图像（WSI）变成干净的、可用于训练的高分辨率
ROI —— 全程在浏览器里完成，跑在（通常无显示器的）服务器上，经 SSH 隧道访问。

```
 ┌─ HistoQC ─┐   ┌─ 审核 + 调整 ────────────────┐   ┌─ 映射回原图 ────────┐
 │ 对一批切片 │ → │ ROI = HistoQC 组织,          │ → │ level-0 GeoJSON     │
 │ 跑质控     │   │ 画笔只做修正:                │   │ + 低分 mask+json    │
 │           │   │ 绿=加回 · 红=排除 ·          │   │ + 高清 tiles        │
 │           │   │ 青=限定范围(可选)            │   │ (批量、后台)        │
 └───────────┘   └──────────────────────────────┘   └─────────────────────┘
```

原始切片**永不被修改**；所有产物都写到一个独立的 studio 输出目录。

## ROI 是怎么算出来的

ROI **以 HistoQC 的组织 mask 为基底** —— 你不用把质控已经识别好的区域重新描一遍。
三个画笔只用来在这个基底上做修正：

- **绿 — 加回（add back）**：HistoQC 误删的区域，并入组织；
- **红 — 排除（exclude）**：伪影 / 不想要的组织，从组织中减去；
- **青 — 限定范围（limit to area，可选）**：当你只想要其中某几块时，画一个圈，ROI 就被
  限制在圈内。

公式：`edited = (组织 ∪ 绿) \ 红`，若画了青圈则 `ROI = edited ∩ 青圈`，否则
`ROI = edited`。每个画笔都是**区域填充**的：画一个圈贡献的是它**圈内的整块区域**，而不只是
那条线。什么都不画，得到的就是完整的 HistoQC 组织。

## 两步审核流程

1. **标记（Mark）** —— 在缩略图上用三个画笔调整 ROI（实时显示每个画笔画了多少像素；
   撤销 / 清空；可调画笔大小；HistoQC 组织层显示开关）。点 **Compute ROI**。
2. **预览（Preview）** —— 算出的 ROI 会染色叠在切片上；可打开四联 QC 对比图；设置审核状态
   （`approved` / `skipped` / `flagged` / …）后进入下一张。

导出**不在单张页里做**：切片审核通过后，回**仪表盘批量导出**
（GeoJSON / 高清 tiles / level-0 mask，用勾选框选择，作为后台任务运行并显示进度条）。

## 安装

```bash
pip install -e /path/to/stROI          # stroi 库（如果还没装）
pip install -e /path/to/stroi-studio   # 本包（会一并装上 flask）
```

HistoQC 必须装在一个**独立的 Python 环境**里（它有自己的 openslide 构建，且只以子进程方式
启动）。把 studio 指向那个环境的解释器：

```bash
export STROI_STUDIO_HISTOQC_PYTHON=/path/to/histoqc-venv/bin/python
# 可选: export STROI_STUDIO_HISTOQC_CONFIG=v2.1
```

## 运行

```bash
stroi-studio \
  --results-dir /path/to/histoqc_output \   # 已有的 QC 输出（可选）
  --slide-dir   /path/to/slides \           # 原始 WSI
  --studio-out  /path/to/studio_output \
  --port 5005
```

然后在你自己的电脑上：

```bash
ssh -L 5005:localhost:5005 <server>
# 打开 http://localhost:5005
```

- `--results-dir` —— 已有的 HistoQC 输出目录（含 `results.tsv` 和每片子目录）。
  **省略它就是从零开始**：只传 `--slide-dir`，然后在仪表盘里点 Run HistoQC；results
  目录会自动建在 `--studio-out` 下。
- `--slide-dir` —— 原始切片文件夹；映射回原图、以及在 GUI 里跑 HistoQC 时必需。
- `--studio-out` —— studio 写状态和每片产物的位置。

## 产物（每张切片，位于 `<studio-out>/<batch>/<slide_file>/`）

| 文件 | 内容 |
|---|---|
| `<slide>_annotation.png` | 你压平后的 绿/红/青 笔迹（可重新打开续编辑） |
| `<slide>_roi.png` | 缩略图分辨率的二值 ROI mask |
| `<slide>_roi.json` | sidecar：ROI 统计 + level-0 尺寸 + 逐轴 downsample |
| `<slide>_roi.geojson` | **level-0 像素坐标**下的 ROI 多边形（QuPath / openslide 可用） |
| `<slide>_overlay.png` | 四联 QC 图（缩略图 / 组织 / ROI / 叠加） |
| `_tiles/` + `tiles_index.tsv` | 从 ROI 切出的高清 tiles（按需导出） |
| `<slide>_roi_level0.png` | 全分辨率二值 mask（按需导出；很大） |

项目状态存在 `<studio-out>/<batch>/studio.sqlite`。

## 适用范围与说明

- **首个版本针对 `.svs`。** 架构本身与格式无关（一律走 openslide），但目前端到端验证过的
  是 `.svs`。
- **Ventana `.bif` 暂不支持**：本机的 libopenslide（3.x/4.x）打开这类文件会报
  `Bad direction attribute "LEFT"`。把 TIFF 方向标签修正（LEFT→RIGHT）后的切片可以正常
  打开；把这个修正做成 ingest 前置步骤是后续工作。打不开的切片会被标记并跳过，绝不会阻塞
  整批其余切片。
- 每片的 downsample 是**逐轴**从切片 / `results.tsv` 读取的（例如 16.0000 × 15.9949）——
  绝不假设各向同性。

## 测试

```bash
python -m pytest tests -q          # GUI + 映射 + QC + 导出（全部 mock）
```

测试不使用任何真实切片数据；HistoQC 和 openslide 都被 mock 或喂合成数据。
