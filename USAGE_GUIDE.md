# Figcopy Collage Pipeline 操作指南

本文说明如何在 Windows PowerShell 中运行“参考拼贴图 → 可复用模板 → 更换客户素材 → 导出 PNG”流水线，并给出本次 `Image #1` 的可复现命令。

## 1. 先理解两种运行阶段

模板制作阶段偶尔运行一次：分析参考图、确认槽位、准备清版背景与透明装饰、构建模板、试拼并人工批准。这个阶段可以导入人工素材，也可以接入外部 VLM、图片编辑或抠图 provider。

客户渲染阶段可以重复运行：只读取已经发布的模板和客户图片，完成等比裁切、图层合成和 PNG 输出。这个阶段完全本地运行，渲染审计中的 `network_calls` 固定为 `0`。

## 2. 环境准备

项目要求 Python 3.11+ 和 Pillow 11/12。在项目目录执行：

```powershell
cd D:\codes\figcopy
python --version
python -m pip install -e .
python -m collage --help
```

需要运行测试时安装开发依赖：

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
```

如果当前环境已经能运行 `python -m collage --help`，无需重复安装。

需要把普通照片用于 `cutout` 槽时，再安装本地 BiRefNet 可选依赖：

```powershell
python -m pip install -e ".[birefnet]"
```

这组依赖包含 PyTorch，安装体积会明显大于项目核心依赖；`photo` 和 `photo_feather` 槽不需要安装它。
当前电脑不使用 NVIDIA CUDA 时，可先明确安装 CPU 版，避免拉取不需要的 CUDA 运行时：

```powershell
python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
python -m pip install -e ".[birefnet]"
```

## 3. 配置 YibuAPI（强制走审计）

本项目提供两个可直接加载的 provider：

- `collage.providers.yibu:YibuVisionProvider`：VLM 分析；
- `collage.providers.yibu:YibuImageProvider`：背景 mask 编辑和 overlay 参考生成。

它们只接受本机 HTTP 回环地址，并在首次调用前检查代理上游确实是 `yibuapi.com`。把 `YIBU_AUDIT_BASE_URL` 配成公网地址会直接报 `YIBU_AUDIT_PROXY_REQUIRED`，因此不会绕过审计误调公网接口。

### 3.1 启动并检查审计代理

先在一个单独的 PowerShell 窗口启动代理并保持窗口运行：

```powershell
cd D:\codes\yibu-audit-proxy
python run.py serve
```

另一个窗口检查状态：

```powershell
python D:\codes\yibu-audit-proxy\run.py status
```

应看到 `"status": "ok"` 和 `"upstream": "https://yibuapi.com"`。健康检查地址固定为 `http://127.0.0.1:17860/_yibu_audit/health`。

### 3.2 配置模型与凭据来源

回到项目目录，运行仓库内的配置脚本：

```powershell
cd D:\codes\figcopy
Set-ExecutionPolicy -Scope Process Bypass
. .\examples\configure_yibu.ps1
```

`Scope Process` 只对当前 PowerShell 窗口生效，关闭窗口后恢复原策略。如果不希望调整当前进程的脚本策略，直接使用下面的手动环境变量命令即可。

脚本只调用现有 `shared.py` 的 `get_api_key()`，不会复制或打印 Key。等价的手动配置如下：

```powershell
$env:YIBU_SHARED_PATH = "D:\codes\creative-video-editor\shared.py"
$env:YIBU_AUDIT_BASE_URL = "http://127.0.0.1:17860"
$env:YIBU_VLM_MODEL = "kimi-k3"
$env:YIBU_VLM_MAX_TOKENS = "16384"
$env:YIBU_VLM_REASONING_EFFORT = "max"
$env:YIBU_IMAGE_MODEL = "gemini-3-pro-image-preview"
$env:YIBU_IMAGE_SIZE = "1K"
$env:YIBU_TIMEOUT_SECONDS = "900"
```

`kimi-k3` 会默认启用 `max` 推理强度，并以 `16384` 作为思考与 Draft 共用的输出硬上限。达到上限时程序会停止并报告 `PROVIDER_OUTPUT_TRUNCATED`，不会把残缺 JSON 当成成功结果。若切回 Opus，可把 `YIBU_VLM_MODEL` 设为 `opus-4.8`；provider 会将它映射为 Yibu 的实际模型 ID `claude-opus-4-8`。

如果以后不想读取旧项目文件，可以改设当前进程的 `$env:YIBU_API_KEY`；它的优先级高于 `YIBU_SHARED_PATH`。不要把真实 Key 写进仓库、命令示例、日志或模板 JSON。

可调配置：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `YIBU_AUDIT_BASE_URL` | `http://127.0.0.1:17860` | 只允许本机回环审计代理 |
| `YIBU_VLM_MODEL` | `claude-opus-4-8` | `opus-4.8` 会自动映射 |
| `YIBU_VLM_MAX_TOKENS` | Kimi 为 `16384`，其他为 `8192` | VLM 思考与 Draft 共用的最大输出预算 |
| `YIBU_VLM_REASONING_EFFORT` | Kimi 为 `max`，其他不传 | 可选 `low`、`high`、`max`；留空则由模型决定 |
| `YIBU_IMAGE_MODEL` | `gemini-3-pro-image-preview` | Gemini 原生 `generateContent` 路由 |
| `YIBU_IMAGE_SIZE` | `1K` | 可选 `1K`、`2K`、`4K` |
| `YIBU_TIMEOUT_SECONDS` | Kimi 为 `900`，其他为 `600` | 单次上游请求超时秒数 |

### 3.3 实际运行 VLM 与图片模型

先分析参考图：

```powershell
python -m collage analyze `
  --reference "D:\datas\图片排版样图\20260908-192946.jpg" `
  --out work\new_template `
  --provider collage.providers.yibu:YibuVisionProvider
```

VLM 输出仍然只是 `draft`。查看 `work\new_template\draft_preview.png`，再用 `review-ui` 检查框选和自动生成的白色删除区：

```powershell
python -m collage review-ui `
  --draft work\new_template\draft.json `
  --out work\new_template\reviewed.json `
  --reviewer YOUR_NAME
```

确认页会按 `target_rect` 短边的 8% 为 `photo_feather` 自动设置羽化宽度，最大不超过 128 px；修改槽位尺寸时，尚未手调的值会同步更新。`photo` 表示硬边，`photo_feather` 使用正数羽化宽度。一般用户不需要了解或填写 `clip_mask`，已有高级覆盖配置仍然兼容。

确认页会把所有槽位和独立装饰的 `source_rect` 合并，并按区域大小加少量边距，自动生成白色清版蒙版；一般不再需要手涂，明显不准确时才补画或擦除，也可一键恢复自动结果。显式传入 `--mask` 时仍以该文件为准。

文字槽会优先使用 Draft 的 `default_text`；若它为空但槽位名称中已经带有识别文字（例如 `文字「hello」`），确认页会直接回填该文字。未指定字体时自动批准本地通用字体，不要求用户寻找字体路径。缺少透明素材的固定装饰默认按参考图近似制作，不再询问素材路径；Draft 待确认问题按当前页面设置处理并写入审核记录，不要求输入长答案。只有自动蒙版确实为空时才需明确确认无需清版。

确认稿没有导入 `background.candidate_path`、且确实需要模型生成背景时，显式传图片 provider：

```powershell
python -m collage build `
  --spec work\new_template\reviewed.json `
  --out templates\new_template `
  --provider collage.providers.yibu:YibuImageProvider
```

构建器会保存模型原始候选、回裁变换和最终保护合成图；mask 黑区最终由本地程序从原图逐像素恢复。模型输出不会因为 HTTP 成功就自动成为 `ready`，仍需试拼和人工批准。

### 3.4 冒烟测试与审计报告

下面的脚本会创建一张合成参考图，真实调用一次图片模型，并通过正常 `build` 路径产出 `needs_review` 模板：

```powershell
python -m examples.yibu_live_smoke --out artifacts\yibu_smoke
```

检查审计状态和导出月报：

```powershell
python D:\codes\yibu-audit-proxy\run.py status
python D:\codes\yibu-audit-proxy\run.py report `
  --month 2026-09 `
  --timezone Asia/Shanghai `
  --csv artifacts\yibu-usage-2026-09.csv
```

审计库不保存提示词、回答、图片或完整 API Key。若 Gemini 返回 `IMAGE_RECITATION`，provider 会报 `PROVIDER_IMAGE_RECITATION`；这是模型的复现/版权相似性限制，应改写为更原创的制作要求，而不是原样盲目重试。

## 4. 零配置验证流水线

先运行不依赖网络和图片模型的内置演示：

```powershell
python -m collage demo --out artifacts/m1_demo
python -m collage validate `
  --template artifacts/m1_demo/template `
  --allow-unreviewed
```

主要输出：

- `artifacts/m1_demo/result.png`：替换素材后的试拼图；
- `artifacts/m1_demo/template/template.json`：状态为 `needs_review` 的模板；
- `artifacts/m1_demo/work/inspection.html`：制作检查报告。

## 5. 复现本次 Image #1 试拼

本次参考图被确认成 4 个客户照片槽：一个全画布主照片，以及左上、右上、左下三个叠加照片。英文、相框虚线、回形针、放射涂鸦和右下角字样作为固定前景。

已保存的生成输入位于：

- `artifacts/reference_20260908_trial/generated_assets/clean_background_generated.png`；
- `artifacts/reference_20260908_trial/generated_assets/test_photo_sheet_generated.png`。

重新执行完整试拼：

```powershell
python -m examples.reference_image_trial `
  --reference "D:\datas\图片排版样图\20260908-192946.jpg" `
  --clean-background "artifacts\reference_20260908_trial\generated_assets\clean_background_generated.png" `
  --photo-sheet "artifacts\reference_20260908_trial\generated_assets\test_photo_sheet_generated.png" `
  --out "artifacts\reference_20260908_trial"
```

该示例脚本只服务于这张 `1320 x 1767` 参考图，其坐标写在 `examples/reference_image_trial.py`。它会按阶段输出日志，并生成：

- `comparison.png`：参考图与试拼图并排对照；
- `trial_render.png`：完整分辨率试拼结果；
- `reviewed.json`：人工确认后的制作规格；
- `bindings.json`：本次四张测试照片的绑定；
- `template/`：可复用但尚未批准的模板包；
- `work/inspection.html`：删除蒙版、清版背景和透明前景检查页。

## 6. 给 Image #1 模板换成自己的照片

先生成槽位说明和 Bindings 起始文件：

```powershell
python -m collage guide `
  --template artifacts/reference_20260908_trial/template `
  --allow-unreviewed `
  --out artifacts/reference_20260908_trial/upload_guide.html `
  --bindings-out artifacts/reference_20260908_trial/my_bindings.json
```

编辑 `my_bindings.json`，将路径换成自己的图片。推荐在 JSON 中使用 `/`，避免 Windows 反斜杠转义：

```json
{
  "version": "collage-bindings/1",
  "slots": {
    "photo_background": {
      "path": "D:/photos/main.jpg",
      "scale": 1.0,
      "offset_px": [0, 0]
    },
    "photo_small_left": {
      "path": "D:/photos/small.jpg",
      "scale": 1.0,
      "offset_px": [0, 0]
    },
    "photo_tall_right": {
      "path": "D:/photos/tall.jpg",
      "scale": 1.0,
      "offset_px": [0, 0]
    },
    "photo_large_left": {
      "path": "D:/photos/large.jpg",
      "scale": 1.0,
      "offset_px": [0, 0]
    }
  }
}
```

生成预览：

```powershell
python -m collage render `
  --template artifacts/reference_20260908_trial/template `
  --bindings artifacts/reference_20260908_trial/my_bindings.json `
  --out artifacts/reference_20260908_trial/my_result.png `
  --allow-unreviewed
```

`scale` 是客户图片在槽内的额外缩放；`cover` 槽要求它不小于 `1.0`。`offset_px` 是槽位局部坐标中的 `[横向, 纵向]` 微调。例：`"scale": 1.12, "offset_px": [-20, 8]` 表示放大 12%，向左 20 像素、向下 8 像素。

### 6.1 为 cutout 槽自动抠图

只有模板中 `mode` 为 `cutout` 的槽需要此步骤。普通 JPG 或不透明 PNG 可直接交给内置的 `BiRefNet_lite-matting`：

```powershell
python -m collage --verbose cutout `
  --input "D:\photos\person.jpg" `
  --out "work\customer\person_cutout.png"
```

输出包括：

- `person_cutout.png`：与输入尺寸一致、带软 Alpha 的本地抠图结果；
- `person_cutout.png.audit.json`：输入/输出哈希、实际模型和耗时，不包含客户图片内容。

然后把 Bindings 中相应槽位改为：

```json
{
  "slots": {
    "person_main": {
      "path": "customer/person_cutout.png"
    }
  }
}
```

已有有效 Alpha 的透明 PNG 会直接通过，不加载模型。模型默认固定为官方 `ZhengPeng7/BiRefNet_lite-matting` 的已审核提交，第一次使用会从 Hugging Face 下载约 89 MB 权重；客户图片始终在本机处理。

可选配置：

```powershell
# 默认 auto：检测到 CUDA 就用 GPU，否则用 CPU。
$env:COLLAGE_BIREFNET_DEVICE = "auto"

# 指定 Hugging Face 下载缓存位置。
$env:COLLAGE_BIREFNET_CACHE_DIR = "D:\models\hf-cache"

# 已手工下载完整模型仓库时，直接使用本地目录并禁止联网取模型。
$env:COLLAGE_BIREFNET_MODEL_PATH = "D:\models\BiRefNet_lite-matting"

# 不设本地目录、但要求只读取已有 Hugging Face 缓存。
$env:COLLAGE_BIREFNET_LOCAL_FILES_ONLY = "1"
```

若要恢复默认设置，在当前 PowerShell 执行：

```powershell
Remove-Item Env:COLLAGE_BIREFNET_DEVICE -ErrorAction SilentlyContinue
Remove-Item Env:COLLAGE_BIREFNET_CACHE_DIR -ErrorAction SilentlyContinue
Remove-Item Env:COLLAGE_BIREFNET_MODEL_PATH -ErrorAction SilentlyContinue
Remove-Item Env:COLLAGE_BIREFNET_LOCAL_FILES_ONLY -ErrorAction SilentlyContinue
```

## 7. 人工确认并发布模板

模板构建后故意保持 `needs_review`。请检查：旧照片是否残留、边框是否断裂、文字是否可读、裁切主体是否合适、图层遮挡是否正确。

确认试拼结果后发布：

```powershell
python -m collage approve `
  --template artifacts/reference_20260908_trial/template `
  --evidence artifacts/reference_20260908_trial/my_result.png `
  --reviewer YOUR_NAME `
  --notes "已检查四个槽位、固定涂鸦和边缘"

python -m collage validate `
  --template artifacts/reference_20260908_trial/template
```

批准后的常规渲染不再需要 `--allow-unreviewed`：

```powershell
python -m collage render `
  --template artifacts/reference_20260908_trial/template `
  --bindings artifacts/reference_20260908_trial/my_bindings.json `
  --out artifacts/reference_20260908_trial/final.png
```

不要仅因为命令成功就批准模板；`approve` 表示真人已经看过新客户素材的最终效果。

## 8. 制作另一张参考图的通用流程

没有 VLM provider 时，先人工填写 Draft；有 provider 时用 `module:object` 显式接入：

```powershell
# 人工 Draft
python -m collage analyze `
  --reference path/to/reference.jpg `
  --manual-draft path/to/manual_draft.json `
  --out work/new_template

# 或真实 VLM provider
python -m collage analyze `
  --reference path/to/reference.jpg `
  --provider your_package:vision_provider `
  --out work/new_template
```

然后打开本机确认页，修正槽位、模式、层序和移除蒙版：

```powershell
python -m collage review-ui `
  --draft work/new_template/draft.json `
  --out work/new_template/reviewed.json `
  --reviewer YOUR_NAME `
  --background-candidate work/new_template/clean_background.png
```

确认后构建、生成上传指南、试拼和批准：

```powershell
python -m collage build `
  --spec work/new_template/reviewed.json `
  --out templates/new_template

python -m collage guide `
  --template templates/new_template `
  --allow-unreviewed `
  --out work/new_template/upload_guide.html `
  --bindings-out work/new_template/bindings.json

python -m collage render `
  --template templates/new_template `
  --bindings work/new_template/bindings.json `
  --out work/new_template/result.png `
  --allow-unreviewed

python -m collage approve `
  --template templates/new_template `
  --evidence work/new_template/result.png `
  --reviewer YOUR_NAME
```

当前仓库不会默认启用在线 provider。可按第 3 节显式配置 Yibu；若没有传 `--provider`，且 `reviewed.json` 没有导入 `background.candidate_path` 或 overlay 的 `prepared_asset`，`build` 会明确报 `IMAGE_PROVIDER_UNAVAILABLE`，不会拿旧参考图或空白图伪装成功。

## 9. 日志与排错

全局 `--verbose` 写在子命令前面：

```powershell
python -m collage --verbose render `
  --template templates/new_template `
  --bindings work/new_template/bindings.json `
  --out work/new_template/debug.png `
  --allow-unreviewed
```

优先检查这些文件：

- `<template>.work/state.json` 或显式 `work/state.json`：当前节点状态与错误码；
- `work/inspection.html`：背景保护、透明 overlay 和 provider 审计；
- `result.png.render.json`：模板/绑定/输出哈希、耗时和网络调用数；
- `template/template.json`：槽位、图层和发布状态。

常见错误：

| 错误码 | 含义与处理 |
|---|---|
| `VISION_PROVIDER_UNAVAILABLE` | 未提供 VLM；改用 `--manual-draft` 或配置真实 provider |
| `IMAGE_PROVIDER_UNAVAILABLE` | 缺清版/装饰素材；导入候选 PNG 或配置图片 provider |
| `CUTOUT_PROVIDER_UNAVAILABLE` | `cutout` 槽收到普通不透明图片；提供透明 PNG、alpha 或抠图 provider |
| `BIREFNET_DEPENDENCY_MISSING` | 未安装或无法加载本地模型依赖；运行 `python -m pip install -e ".[birefnet]"` |
| `BIREFNET_MODEL_LOAD_FAILED` | 首次下载失败、缓存不完整或模型目录不正确；查看 `--verbose` 日志并检查模型配置 |
| `BIREFNET_DEVICE_UNAVAILABLE` | 显式配置了不可用的 CUDA；改成 `COLLAGE_BIREFNET_DEVICE=auto` 或 `cpu` |
| `MISSING_BINDING` | 必填槽位没有素材；检查 Bindings 中的 slot id |
| `TEMPLATE_NOT_READY` | 模板尚未批准；制作预览时加 `--allow-unreviewed`，正式使用前执行 `approve` |
| `SPEC_VALIDATION_FAILED` | JSON 字段、枚举或坐标不符合 schema；根据日志中的字段路径修正 |
| `YIBU_AUDIT_PROXY_UNAVAILABLE` | 本机审计代理未启动；先运行 `D:\codes\yibu-audit-proxy\run.py serve` |
| `YIBU_AUDIT_PROXY_REQUIRED` | Base URL 不是本机回环审计地址；改回 `http://127.0.0.1:17860` |
| `YIBU_CREDENTIAL_MISSING` | 没有 `YIBU_API_KEY`，也没有可用的 `YIBU_SHARED_PATH` |
| `PROVIDER_OUTPUT_TRUNCATED` | VLM 达到输出上限；提高 `YIBU_VLM_MAX_TOKENS` 或降低推理强度后重新分析 |
| `PROVIDER_DRAFT_INCOMPLETE` | VLM 没有返回完整 Draft；检查模型响应或换用更稳定的结构化输出模型 |
| `PROVIDER_IMAGE_RECITATION` | Gemini 因复现相似性限制拒绝生成；把提示改得更原创后再发起新请求 |
| `PROVIDER_CONTENT_BLOCKED` | Gemini 内容策略阻止生成；检查参考图和制作要求，不要自动重试 |

## 10. 最短操作清单

日常给已发布模板换图时，只需：

1. 复制一份 Bindings；
2. 为每个命名槽位填写客户图片路径；
3. 必要时调 `scale` 和 `offset_px`；
4. 运行 `python -m collage render ...`；
5. 查看 PNG 和同名 `.render.json`。
