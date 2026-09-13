# Figcopy Collage Pipeline 操作指南

本文说明如何在 Windows PowerShell 中运行“参考拼贴图 → 可复用模板 → 更换客户素材 → 导出 PNG”流水线，并给出本次 `Image #1` 的可复现命令。

## 先用这一条主流程

完成下面“环境准备”后，普通使用只需启动本机工作台：

```powershell
cd D:\codes\figcopy
python -m collage studio --data-dir D:\datas\figcopy
```

程序默认打开 `http://127.0.0.1:8787/`。在页面内按顺序完成：

1. 新建项目并上传参考拼贴图；
2. 检查 Draft 框位、图层和背景来源；固定底板需复核删除蒙版，客户满版照片背景无需背景清版；没有反馈时勾选整体确认即可保存，有反馈时先提交 VLM 纠正；
3. 按页面生成的命名槽位上传客户图片或填写文字；
4. 查看本地合成预览，人工确认后批准发布。

耗时的 VLM、图片生成、抠图与渲染作为后台任务执行，页面会轮询项目状态。浏览器标签可关闭；如果终端进程也被关闭，重新运行 `studio`，项目会从持久化阶段继续。工作台仍然保留 Draft 与最终预览两个明确的人工门禁。

安装过 editable package 后，等价命令更短：

```powershell
figcopy studio --data-dir D:\datas\figcopy
```

默认会自动打开浏览器；`--no-open` 禁止自动打开，`--port 8899` 可更换端口。服务固定只接受本机回环访问；写操作需要页面启动令牌，并拒绝非本机 Host/Origin。单次上传总量限制为 128 MiB。

启动 Yibu 审计代理后，点击页面右上角“Provider 设置”即可在密码框输入 API Key，并配置 VLM、图片编辑模型和本地 BiRefNet。Key 只保存在当前 `studio` 进程内存中，不进入项目文件、日志或浏览器存储；关闭该进程后会清除。流程若因缺少配置而阻塞，设置页可在保存后直接重试当前项目。

没有 VLM 或图片模型时，在“新建项目 → 高级设置”上传人工 Draft JSON 与清版背景图。需要 `cutout` 的普通不透明照片时，安装 BiRefNet 可选依赖，或在高级设置中指定其他抠图 Provider。

命令行编排仍可用于自动化和无界面环境：

```powershell
figcopy run --project my-collage --reference "D:\pictures\reference.jpg" --reviewer YOUR_NAME
figcopy status --project my-collage
figcopy resume --project my-collage --bindings "D:\customer\bindings.json"
figcopy resume --project my-collage --approve --approval-notes "已完成视觉检查"
```

程序关闭、模型失败或依赖缺失时，工作台会显示稳定错误码与重试表单；CLI 用户可用 `status` 查看 `stage`、`last_error` 和 `next_action`，修复后执行 `resume`。后续章节的 `analyze / review-ui / build / guide / render / approve` 是高级调试入口。

### 背景来源与迁移项目

审核页普通区域提供“背景来源”选择，不需要修改 JSON 或手动删除背景层：

- “固定底板：保留或清版”：适用于纸纹、相册底板或需要保留的固定场景。原来的局部保护、整张重建、已有背景候选及 mask 校验保持有效。
- “客户照片铺满背景”：选择一个必填、铺满画布、无旋转/开洞/羽化的普通照片槽。系统直接以该槽作为最底层，不清版背景；文字和装饰仍按原策略制作。上传后还会检查实际裁切与偏移是否漏底。

VLM 已明确输出照片背景时页面会选中它；旧稿即使存在满版槽也只提示可切换，不自动改变来源。切换会取消整体确认勾选，检查后需要重新勾选；不会因此要求填写反馈或逐字核对未展开的默认文字。

对于已经确认、正在报背景制作错误的迁移项目：

1. 回到项目页，在“项目文件”区域点击“修改背景来源，另存版本”。不必先构建出模板。
2. 新版本打开审核页后，选择客户照片背景及对应槽。S02 街景图的已有槽为 `slot_bg_photo`，不新增照片槽。
3. 核对其他槽位和装饰，勾选整体确认并开始制作。背景节点应显示 `BACKGROUND_PROVIDED_BY_SLOT`，装饰继续执行。
4. 按命名槽上传素材，检查本地成图并重新验收；原项目、确认记录和结果保留。

另存本身只做本地复制，不调用模型。复制未完成会以 `BACKGROUND_REVISION_INCOMPLETE` 停止，需从源项目重新另存；不能把半成品直接继续制作。装饰的待定请求和重试次数也保留，避免同一未决请求重复计费。

跳过背景不等于整条制作流程不需要 Provider。若装饰仍需参考生成，`HTTP 401 / PROVIDER_AUTH_FAILED` 仍要解决凭据问题。更新代码后需重启 `studio` 并刷新浏览器；内存中输入的 Key 需要重新填写。

## 1. 先理解两种运行阶段

模板制作阶段偶尔运行一次：分析参考图、确认槽位、准备清版背景与透明装饰、构建模板、试拼并人工批准。这个阶段可以导入人工素材，也可以接入外部 VLM、图片编辑或抠图 provider。

客户渲染阶段可以重复运行：只读取已经发布的模板和客户图片，完成等比裁切、图层合成和 PNG 输出。这个阶段完全本地运行，渲染审计中的 `network_calls` 固定为 `0`。

## 2. 环境准备

项目要求 Python 3.11+、Pillow 11/12 和 PyAV 18.1。在项目目录执行：

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

建议固定一个源码仓库之外的数据目录：

```powershell
$env:FIGCOPY_DATA_DIR = "D:\datas\figcopy"
```

`studio`、`run`、`resume`、`status` 和 `demo` 会在其中使用 `projects/<项目 ID>`；底层 CLI 的显式 `--out`、`--work` 路径也应指向该目录。

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

Web 工作台用户直接点击右上角“Provider 设置”：在 Yibu API Key 密码框输入 Key，或填写已有 `shared.py` 路径；模型字段会显示当前进程使用的值。VLM 与图片编辑共享同一份 Yibu 凭据和审计代理。设置只影响当前工作台进程，且不会回传或持久化 Key。

命令行、自动化或希望启动时预先配置的用户，可以回到项目目录运行仓库内的配置脚本：

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
$env:YIBU_VLM_REASONING_EFFORT = "high"
$env:YIBU_IMAGE_MODEL = "doubao-seedream-5-0-260128"
$env:YIBU_IMAGE_SIZE = "2K"
$env:YIBU_TIMEOUT_SECONDS = "900"
```

`kimi-k3` 会默认启用 `high` 推理强度，并以 `16384` 作为思考与 Draft 共用的输出硬上限。达到上限时程序会停止并报告 `PROVIDER_OUTPUT_TRUNCATED`，不会把残缺 JSON 当成成功结果。若切回 Opus，可把 `YIBU_VLM_MODEL` 设为 `opus-4.8`；provider 会将它映射为 Yibu 的实际模型 ID `claude-opus-4-8`。

已有环境变量或工作台中显式设置的 `max` 仍会保留；如需使用 `high`，请修改该设置或重新加载配置脚本。修改代码默认值后，已启动的工作台需要重启才能加载新默认值。

如果以后不想读取旧项目文件，可以改设当前进程的 `$env:YIBU_API_KEY`；它的优先级高于 `YIBU_SHARED_PATH`。不要把真实 Key 写进仓库、命令示例、日志或模板 JSON。

可调配置：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `YIBU_AUDIT_BASE_URL` | `http://127.0.0.1:17860` | 只允许本机回环审计代理 |
| `YIBU_VLM_MODEL` | `kimi-k3` | `opus-4.8` 会自动映射到 `claude-opus-4-8` |
| `YIBU_VLM_MAX_TOKENS` | Kimi 为 `16384`，其他为 `8192` | VLM 思考与 Draft 共用的最大输出预算 |
| `YIBU_VLM_REASONING_EFFORT` | Kimi 为 `high`，其他不传 | 可选 `low`、`high`、`max`；留空则由模型决定 |
| `YIBU_IMAGE_MODEL` | `doubao-seedream-5-0-260128` | Seedream 图生图使用 `/v1/images/generations` 的 `image` 字段；Gemini 模型仍自动使用原生 `generateContent` |
| `YIBU_IMAGE_SIZE` | `2K` | 可选 `1K`、`2K`、`4K`；当前 Seedream Lite 最低为 2K，配置 1K 时会明确记录警告并使用 2K |
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

确认页的问答和“其他错误或补充说明”均为选填。全部留空时，勾选整体确认即可保存；未展开的固定装饰文字沿用识别结果，并记录为接受默认文字。展开固定装饰或修改文字后，需要勾选对应文字确认，收起不会跳过已展开项的检查。有非空反馈时，先提交 VLM 纠正，可以只回答部分问题；模型返回后重新确认当前版本。

确认页会按 `target_rect` 短边的 8% 为 `photo_feather` 自动设置羽化宽度，最大不超过 128 px；修改槽位尺寸时，尚未手调的值会同步更新。`photo` 表示硬边，`photo_feather` 使用正数羽化宽度。一般用户不需要了解或填写 `clip_mask`，已有高级覆盖配置仍然兼容。

确认页会把所有槽位和独立装饰的 `source_rect` 合并，并按区域大小加少量边距，自动生成白色清版蒙版；一般不再需要手涂，明显不准确时才补画或擦除，也可一键恢复自动结果。显式传入 `--mask` 时仍以该文件为准。

文字槽会优先使用 Draft 的 `default_text`；若它为空但槽位名称中已经带有识别文字（例如 `文字「hello」`），确认页会直接回填该文字。未指定字体时自动批准本地通用字体，不要求用户寻找字体路径。复杂固定装饰默认按参考图生成完整素材，生成结果仍按获准使用的完整文字检查。原始识别问题保留在 Draft 与确认记录中，接受当前结果不会冒充 VLM 已纠正或已逐字核对。只有自动蒙版确实为空时才需明确确认无需清版。

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
python -m collage demo --data-dir D:\datas\figcopy --project m1-demo
python -m collage validate `
  --template D:\datas\figcopy\projects\m1-demo\template `
  --allow-unreviewed
```

主要输出：

- `D:\datas\figcopy\projects\m1-demo\renders\result.png`：替换素材后的试拼图；
- `D:\datas\figcopy\projects\m1-demo\template\template.json`：状态为 `needs_review` 的模板；
- `D:\datas\figcopy\projects\m1-demo\workspace\inspection.html`：制作检查报告。

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
| `CHROMA_DEPENDENCY_MISSING` | 无法加载本地 PyAV；运行 `python -m pip install -e "."` 更新核心依赖，并重启工作台 |
| `CHROMA_FILTER_UNAVAILABLE` | PyAV 缺少去底滤镜；安装项目声明的官方 PyAV 二进制包 |
| `CHROMA_PROCESSING_FAILED` | 本地色键处理失败；检查单件原始输出与日志，不会自动回退旧算法 |
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

### 背景接缝、贴纸裁断与人物显小

确认页提供“背景制作方式”：

- **局部保护**（默认）：按删除区和融合边带拼回候选背景，区外保留参考图。扩张与羽化都为 0 时是硬边拼接；羽化能柔化边界，但不能纠正模型重画后的纹理错位。
- **整张重建**：直接采用映射到画布的完整清版候选，适合大面积移除照片、只要求近似纸纹的拼贴。已有局部 `allowed_mask` 时不能使用此模式。

`review` 和 `review-ui` 也支持 `--background-composition full_candidate`；ReviewedSpec 对应字段为 `background.composition_mode`。可配合 `--background-candidate` 复用已满意的完整背景，避免重复生成。

装饰素材采用等比补边保留完整模型输出，生成后的内容不能按参考图输入的补边窗口再次裁切。提示词要求先补全轮廓，超画布效果在最终排版时处理。旧版本缓存已丢失的上下内容无法通过改缩放恢复，修改后需重新构建相应装饰。

参考效果是大幅照片柔边融合时，槽位选 `photo_feather + cover` 并绑定原照片。`cutout` 会去掉照片环境，不适合替代这类处理；抠图模式按可见 alpha 范围缩放，透明留白不再把人物压小。半身照用于特写槽时仍需调 `scale` 与 `offset_px`，槽位铺满不代表脸部大小已匹配。渲染过程只使用本地图片，不会生成或修改人脸。

## 10. 最短操作清单

日常给已发布模板换图时，只需：

1. 复制一份 Bindings；
2. 为每个命名槽位填写客户图片路径；
3. 必要时调 `scale` 和 `offset_px`；
4. 运行 `python -m collage render ...`；
5. 查看 PNG 和同名 `.render.json`。

## 制作后调整独立装饰

在素材上传页、待验收结果页或已发布结果页点击“调整独立装饰图层”。

- 在画面或列表中选择一件装饰，拖动位置；输入宽高和旋转角度，使用“上移一层／下移一层”调整遮挡关系。
- “本地精确预览”调用正式 Renderer；未绑定照片时使用明确标记的占位照片，不代表真实换图效果。
- “保存为新版本”创建新的布局修订项目，复制固定素材并保留哈希，原项目继续保留。已有可用客户素材时直接在本地生成新预览，否则等待补充素材。
- 新版本需要重新验收。位置调整不会重新生成固定装饰。
- 结构复核的问答记录在项目 analysis/feedback 下，最终确认绑定当前 Draft 哈希；生成失败的单件记录在 workspace/overlay_attempts 下。请依据业务 code 检查，未知网络结果不会自动重复计费。

本轮验证入口见 [实现记录](artifacts/pipeline_interactive_verified/README.md)。

### 固定装饰的 PyAV 色键处理

生成装饰和导入色键素材均使用同一 PyAV 路径。保留现有 `chroma_key`、`chroma_tolerance` 字段；默认容差 40 对应 `similarity=0.12`，默认软边对应 `blend=0.16`。程序从外围 8% 区域采样与指定色键色相相近的可见像素；背景变化较大时会相应扩大去底半径。

绿色、蓝色背景附加对应的颜色污染清理；紫红、青、黄等其他色键执行去底而不套用绿色去污染。去底依据颜色，因此被线条围住的同色色键区域也会变透明；请使用与主体颜色分离的色键。细线不再自动内缩，输入已有的透明度会保留。

升级本地处理器不会清空已发生的生成请求或重新开始两次生成额度。旧原图可以重新去底并接受新的完整性检查；已发送但结果未知的检查仍会停止自动重放。PyAV 处理后的部分边缘可能呈灰色，仍需按实际合成效果验收。
