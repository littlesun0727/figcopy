# Figcopy Collage MVP

本仓库实现“参考拼贴图 → 可复用模板 → 客户素材合成”的首版工作流。模板制作期可以接入外部 VLM、图片编辑或抠图 provider；模板发布后的渲染只读取本地 JSON/PNG/字体，不调用 VLM 或生图服务。

第一次使用请直接阅读 [USAGE_GUIDE.md](USAGE_GUIDE.md)，其中包含零配置演示、`Image #1` 实际试拼、换成自己照片、人工批准以及排错命令。

运行产物不再默认写入源码仓库。可通过 `--data-dir` 或 `FIGCOPY_DATA_DIR` 指定外部数据目录；未指定时使用系统用户数据目录。原有 `work/`、`templates/`、`artifacts/` 已归档到 `D:\datas\figcopy\archive\legacy-20260910-pre-cleanup`。

实现遵循 [COLLAGE_MVP_PLAN.md](COLLAGE_MVP_PLAN.md)。仓库内置了强制经过本机审计代理的 Yibu VLM/图片 provider，以及完全本地运行的 BiRefNet_lite-matting 抠图 provider；二者都不会在仓库保存密钥或客户原图。无凭据时仍可完整运行本地合成、导入人工素材、人工审核和发布链路。

## 快速验证

环境要求为 Python 3.11+、Pillow 11/12。核心流程已用 Python 3.14、Pillow 12.3 和 pytest 9 验证；BiRefNet 可选 provider 已在 `env_py11` 的 Python 3.11 CPU 环境实测。

```powershell
python -m pytest -q
$env:FIGCOPY_DATA_DIR = "D:\datas\figcopy"
python -m collage demo --project m1-demo
python -m collage validate `
  --template "$env:FIGCOPY_DATA_DIR\projects\m1-demo\template" `
  --allow-unreviewed
```

Yibu 接入可直接加载现有 `D:\codes\creative-video-editor\shared.py`；仓库配置脚本使用 `kimi-k3` 和 `gemini-3-pro-image-preview`。配置、审计代理启动、真实冒烟测试与完整命令见 [USAGE_GUIDE.md](USAGE_GUIDE.md#3-配置-yibuapi强制走审计)。

`demo` 会程序构造参考图、底图、透明贴纸和两张客户测试图，然后构建 `needs_review` 模板并用新素材生成 `D:\datas\figcopy\projects\m1-demo\renders\result.png`。它不会自动冒充真人批准；查看结果后需显式运行 `approve`。整个演示不访问网络，也不把程序构造素材描述成真实模型效果。

## 推荐：本机 Web 工作台

```powershell
cd D:\codes\figcopy
python -m collage studio --data-dir D:\datas\figcopy
```

命令会打开只监听 `127.0.0.1` 的浏览器工作台。普通使用不再需要编辑 JSON 或逐条执行 Python：

- 在“新建项目”里上传参考图；
- 在可视化审核页确认内容框和删除蒙版；
- 按页面列出的命名槽位上传客户图片、填写文字；
- 直接查看合成 PNG，并经过第二个人工门禁后发布。

VLM、图片生成和抠图在后台任务中运行，页面会自动刷新阶段。关闭浏览器标签不会丢进度；终端进程被关闭后，重新运行同一条 `studio` 命令即可读取 `project.json` 并从已保存阶段继续。工作台不会跳过 Draft 审核和最终结果批准两个真人门禁。

安装过 editable package 后也可写成：

```powershell
figcopy studio --data-dir D:\datas\figcopy
```

默认会自动打开浏览器；用 `--no-open` 可只启动服务，默认地址为 `http://127.0.0.1:8787/`。客户图会被规范化后复制进外部项目数据目录，不会写进源码仓库。页面采用单次启动令牌保护写操作，并拒绝非回环 Host/Origin。

`run / resume / status` 仍保留给自动化、无界面服务器和排错；底层单步命令继续保留给局部调试。没有模型凭据时，可在新建项目的高级设置中上传人工 Draft 与清版背景图。

## 高级单步工作流

以下命令保留给调试、局部重做和自定义集成；普通使用不需要逐条执行。

### 1. 分析参考图

没有 VLM 时使用人工 JSON 草稿：

```powershell
python -m collage analyze `
  --reference path/to/reference.jpg `
  --manual-draft path/to/manual_draft.json `
  --out work/demo
```

程序会应用 EXIF 方向、保存 `work/demo/reference.png`，再写入包含程序所有元数据的 `draft.json` 和框选预览。人工草稿只需提供 `slots`、`overlays`、`background`、`layer_order` 和 `questions`。运行 `demo` 后可参考其 `reviewed.json`；严格字段约定按对象类型放在 `collage/schemas/`。

配置真实 VLM 时，显式传入 Python 插件：

```powershell
python -m collage analyze --reference ref.png --out work/demo --provider your_package:vision_provider
```

接口收到的是规范化 PNG 字节与媒体类型，而非模型不可访问的本地路径。模型返回值只会成为候选 Draft，不能直接发布。

### 2. 人工确认

可直接编辑 JSON/mask，也可启动只监听 `127.0.0.1` 的单页确认工具：

```powershell
python -m collage review-ui `
  --draft work/demo/draft.json `
  --out work/demo/reviewed.json `
  --reviewer YOUR_NAME `
  --background-candidate work/demo/imported_background.png
```

页面会根据 Draft 中旧照片、旧文字和独立装饰的 `source_rect` 自动涂好白=删除的 `remove_mask`，通常只需检查内容框后直接保存；明显误差仍可补画、擦除或一键恢复自动涂层。`photo_feather` 会按框大小自动设置柔和边缘；文字会复用 Draft 已识别出的原文并自动批准本地通用字体；缺少透明素材的固定装饰按近似制作；Draft 待确认问题本轮按当前设置处理，不再要求逐题输入长答案。位置、文字样式、背景参数和层序收在折叠的“高级”区域。图片模式仍为 `unknown`，或确实没有任何可清除区域时，保存才需要额外确认。页面不会自动打开外部浏览器，也不会监听公网地址。

纯 CLI 确认方式：

```powershell
python -m collage review `
  --draft work/demo/draft.json `
  --out work/demo/reviewed.json `
  --remove-mask work/demo/remove_mask.png `
  --background-candidate work/demo/imported_background.png `
  --expand-px 6 --feather-px 10 `
  --reviewer YOUR_NAME
```

`--slot-overrides` 和 `--overlay-overrides` 仍接受 `id -> object` JSON，可为确认页预载字体、clip mask、预制 overlay、basic shape 等高级字段。普通页面流程不要求填写这些路径；若高级覆盖显式重新启用 `requires_exact_content=true`，仍必须同时提供 `prepared_asset`。

### 3. 构建与检查模板素材

```powershell
python -m collage build --spec work/demo/reviewed.json --out templates/demo
```

若 `candidate_path` / `prepared_asset` 已在确认稿中给出，构建会导入这些素材。否则必须显式配置实现了协议的图片 provider：

```powershell
python -m collage build --spec work/demo/reviewed.json --out templates/demo --provider your_package:image_provider
```

`--fixture-provider` 只用于离线链路测试，产物会永久记录 `fixture_used=true`，默认无法被批准为正式模板。

默认工作目录是模板目录旁的 `<name>.work`，其中保存：

- 原参考图截取的 overlay crop；
- `remove_mask.png`、`blend_mask.png`；
- provider 原始背景候选和受保护合成后的背景；
- provider 输入补边/回裁变换；
- overlay 的浅底/深底边缘预览；
- 节点状态、缓存、调用审计和 `inspection.html`。

模板包本身只包含运行所需的 `template.json`、`assets/`、可选 `masks/` 和批准后的 `preview.png`，不会携带 prompt、原参考图、凭据或制作端绝对路径。

### 4. 用新客户素材试拼并批准

先从 slot 生成命名上传指南与 Bindings 起始文件：

```powershell
python -m collage guide --template templates/demo --allow-unreviewed `
  --out work/upload_guide.html --bindings-out work/bindings.json
```

编辑 Bindings 后生成待审预览：

```powershell
python -m collage render --template templates/demo --bindings work/bindings.json `
  --out work/result.png --allow-unreviewed
```

确认旧素材残留、接缝、绿边、遮挡和层序均可接受后，显式记录验收：

```powershell
python -m collage approve --template templates/demo --evidence work/result.png --reviewer YOUR_NAME
```

批准后，普通 `render` 只接受 `ready` 模板。每次渲染还会写一个不含客户路径或图片数据的 `.render.json`，记录输入/输出哈希、耗时和 `network_calls=0`。

## Bindings

图片绑定支持缩放与槽位局部像素偏移：

```json
{
  "version": "collage-bindings/1",
  "slots": {
    "photo_left": {
      "path": "customer/photo.jpg",
      "scale": 1.1,
      "offset_px": [-12, 4]
    },
    "person_main": {
      "path": "customer/person.png"
    },
    "caption": {
      "text": "今天很开心"
    }
  }
}
```

`cutout` 接受有效透明 PNG，或接受与规范化客户图尺寸一致的 `subject_alpha`。Renderer 本身永远不调用 provider。普通不透明图片先安装本地抠图可选依赖：

```powershell
python -m pip install -e ".[birefnet]"
python -m collage cutout --input customer.jpg --out customer_cutout.png
```

`collage cutout` 默认使用官方 `ZhengPeng7/BiRefNet_lite-matting`，在本机输出带软 Alpha 的 PNG；首次运行会下载固定版本的约 89 MB 模型权重，但不会上传客户图片。同一进程内会复用已加载模型。随后把 Bindings 中该槽的 `path` 指向 `customer_cutout.png`。

默认设备为 `auto`（有 CUDA 时用 CUDA，否则用 CPU）。常用配置如下：

```powershell
$env:COLLAGE_BIREFNET_DEVICE = "cpu"                    # 或 auto / cuda / cuda:0
$env:COLLAGE_BIREFNET_CACHE_DIR = "D:\models\hf-cache" # 下载缓存目录
$env:COLLAGE_BIREFNET_MODEL_PATH = "D:\models\BiRefNet_lite-matting" # 完全离线目录
```

设置 `COLLAGE_BIREFNET_MODEL_PATH` 后只读取该本地目录，不访问 Hugging Face。高级用户仍可用 `--provider your_package:cutout_provider` 替换默认实现；只有云端 provider 才必须显式传 `--allow-cloud-upload`。

## Provider 接口

接口定义在 `collage/providers/base.py`：

- `VisionProvider.analyze(...)` 返回候选 Draft 和 `ProviderAudit`；
- `ImageProvider` 通过 `ImageCapabilities` 声明参考图、mask、透明输出、mask 极性和尺寸限制；
- `CutoutProvider.cutout(...)` 返回客户图坐标空间的单通道主体 alpha。

provider 对象必须能以 `module:object` 加载；`object` 可以是无参类、无参工厂或实例。不要在代码或配置中硬编码密钥。图片调用只对明确的限流/临时网络错误做有限重试；超时、权限、拒绝和参数错误不会盲目重发。

## 状态与发布规则

制作状态写在 work 的 `state.json`：`reviewed → building → needs_review`，错误为 `blocked` 或 `failed`。只有以下条件同时满足时才能变为 `ready`：

- schema、引用、图片解码、哈希、尺寸和安全路径全部通过；
- 复杂 overlay 具有真实透明像素；
- 使用新客户素材生成了同画布尺寸的验收图；
- 模板作者显式运行 `approve` 并记录 reviewer 与证据哈希。

自动测试证明的是程序行为，不证明生成背景或装饰的视觉内容正确。真实 M2/M3/M4/M5 验收仍需要授权 provider、参考图、客户替换素材和人工视觉判断。
