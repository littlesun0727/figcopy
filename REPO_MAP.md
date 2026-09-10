# Repository Map

本文件记录实施 `COLLAGE_MVP_PLAN.md` 后的真实仓库职责、运行入口和能力缺口。

## 实施前盘点

实施开始时仓库只有 `COLLAGE_MVP_PLAN.md`，没有 `AGENTS.md`、README、依赖清单、运行入口、测试、VLM、图片编辑、抠图、文件存储、工作流或渲染接口。因此没有可复用代码，也没有可运行的基线测试；本实现采用计划建议的 Python + Pillow，不引入工作流框架、数据库或前端依赖。

## 文件职责

| 职责 | 真实文件 |
|---|---|
| CLI 与命令路由 | `collage/cli.py`, `collage/__main__.py` |
| Draft / ReviewedSpec / TemplateSpec / Bindings 严格校验 | `collage/schema.py` |
| EXIF、crop、mask、alpha、颜色键与背景保护合成 | `collage/prepare.py` |
| 等比 provider 补边、回裁和坐标逆变换 | `collage/geometry.py` |
| VLM 输入、人工 Draft 导入与候选框预览 | `collage/analyze.py` |
| CLI 人工确认与一页式本地确认界面 | `collage/review.py`, `collage/review_ui.py` |
| provider 协议、能力声明和显式插件加载 | `collage/providers/base.py` |
| 明确标记的确定性测试 provider | `collage/providers/fixture.py` |
| 强制经本机审计代理的 Yibu VLM / Gemini 图片 provider | `collage/providers/yibu.py` |
| 本地 BiRefNet_lite-matting 客户图片抠图 provider | `collage/providers/birefnet.py` |
| 背景/overlay 构建、审计、视觉报告与发布 | `collage/build.py` |
| 内容寻址缓存和节点状态 | `collage/cache.py` |
| 客户图片/文字准备与确定性 Renderer | `collage/render.py` |
| 独立 cutout 准备及云上传授权门禁 | `collage/cutout.py` |
| 模板包文件、路径、透明度和发布校验 | `collage/validate.py` |
| 命名上传指南与 Bindings 起始文件 | `collage/guide.py` |
| 无网络 M1 演示生成 | `collage/demo.py` |
| Image #1 真实参考图试拼示例 | `examples/reference_image_trial.py` |
| Yibu 配置与真实图片冒烟测试 | `examples/configure_yibu.ps1`, `examples/yibu_live_smoke.py` |
| 中文操作指南 | `USAGE_GUIDE.md` |
| 原子写入、安全路径、哈希、图片解码 | `collage/io_utils.py` |
| 自动化回归 | `tests/` |

## 运行入口

```powershell
python -m collage --help
python -m collage demo --out artifacts/m1_demo
python -m collage validate --template artifacts/m1_demo/template --allow-unreviewed
python -m pytest -q
```

安装项目后也可使用 `collage ...` console script。依赖与 Python 版本约定在 `pyproject.toml`。

## 当前 provider 与能力

| 能力 | 当前实现 | 状态 |
|---|---|---|
| VLM 候选 Draft | Yibu 审计 provider + 严格协议 + 人工草稿 fallback | `claude-opus-4-8` 已用 Image #1 实测 |
| 背景 mask 编辑 | Yibu Gemini provider、mask 极性、保护合成、缓存 | `gemini-3-pro-image-preview` 已用合成 mask 实测 |
| Overlay 参考生成 | Yibu Gemini provider、alpha/颜色键适配、边缘预览、缓存 | 协议已自动测试，仍需真实模板视觉验收 |
| Cutout | 透明 PNG 直通 + 本地 BiRefNet_lite-matting + 可插拔 provider + 云上传授权 | 自动测试及 Python 3.11 CPU 真实模型冒烟通过；视觉验收待模板作者确认 |
| 本地渲染 | Pillow，无 provider 参数、审计固定为零网络调用 | 已实现并自动测试 |

Yibu provider 拒绝公网 Base URL，必须先通过 `127.0.0.1` 审计健康检查；API Key 只从环境变量或明确指定的外部 `shared.py` 动态读取。fixture provider 只用于调用、缓存和失败恢复测试，审计中明确为 `fixture=true`。导入人工制作素材记录为 `imported-*`，也不宣称它来自真实模型。

## 验证范围

自动测试覆盖 EXIF、source rect、provider 补边/回裁、mask 极性、保护像素、缓存失效、层序、cover/contain、旋转、画布裁切、半透明合成、photo feather、cutout 门禁、路径穿越、损坏图片、透明通道、中文路径和发布证据。

额外真实验证已覆盖：Yibu `claude-opus-4-8` 对 Image #1 的候选 Draft，`gemini-3-pro-image-preview` 对合成图的 mask 编辑、尺寸回裁、保护区合成和审计落盘，以及 `BiRefNet_lite-matting` 对 1080×1440 人物照片的 CPU 推理、离线缓存与本地目录加载。尚缺的真实验收不是代码测试可以替代的：完整真实参考图的模型清版与复杂装饰生成、模板作者对抠图边缘与保留对象的确认，以及两类真实拼贴模板 × 每类三组客户素材的人工视觉回归。
