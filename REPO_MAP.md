# 工程结构

这份文件只说明“代码放在哪里、运行数据放在哪里”。具体命令仍见
[USAGE_GUIDE.md](USAGE_GUIDE.md)。

## 两类目录

- `D:\codes\figcopy`：源码、测试、示例和文档。正常运行不应在这里产生项目文件。
- Figcopy 数据目录：项目输入、中间结果、模板、渲染结果、缓存和日志。由
  `--data-dir`、`FIGCOPY_DATA_DIR` 或系统用户数据目录决定；当前开发数据使用
  `D:\datas\figcopy`。

数据目录结构：

```text
<figcopy-data>/
├─ projects/
│  └─ <project-id>/
│     ├─ project.json
│     ├─ inputs/
│     ├─ analysis/
│     ├─ review/
│     ├─ template/
│     ├─ renders/
│     ├─ reports/
│     └─ workspace/
├─ cache/
├─ logs/
└─ archive/
```

`projects` 是用户项目的持久化目录；一个项目从参考图分析到最终渲染的文件都归在
同一个 `<project-id>` 下。它不是 Python 源码目录。

## Python 包分层

| 目录 | 职责 |
|---|---|
| `collage/cli/` | 命令行参数和命令路由 |
| `collage/core/` | 错误类型、日志、原子文件 IO、状态文件 |
| `collage/projects/` | 外部数据根目录、项目路径和 `project.json` |
| `collage/workflows/` | 可恢复状态机、阶段执行、客户素材导入和状态摘要 |
| `collage/schemas/` | Draft、ReviewedSpec、TemplateSpec、Bindings 校验 |
| `collage/imaging/` | 通用图片读写、mask、alpha、颜色键和几何变换 |
| `collage/providers/` | VLM、图片生成、抠图 provider 协议及实现 |
| `collage/template/` | 参考图分析、人工审核、模板构建、发布和校验 |
| `collage/rendering/` | Bindings 准备、图片层、文字层和确定性合成 |
| `collage/studio/` | 本机审核服务及独立 HTML/CSS/JS |
| `collage/devtools/` | 离线演示数据生成；不属于生产渲染路径 |

主要子模块：

- `template/build/`：背景、overlay、模板包、检查报告、批准和编排。
- `template/review/`：审核默认规则与 ReviewedSpec 固化。
- `providers/yibu/`：配置、审计 HTTP 客户端、VLM 分析和图片生成。
- `studio/templates/`、`studio/static/`：审核页前端资源。
- `rendering/`：Bindings、布局、图片层、文字层与顶层渲染服务。
- `workflows/`：`run / resume / status` 使用的统一端到端编排层。

依赖方向保持为：`cli → workflows/studio → template/rendering/projects → schemas/providers/imaging/core`。
底层模块不反向依赖 CLI 或页面层。

## 稳定入口

```powershell
python -m collage --help
figcopy --help
python -m pytest -q
```

安装后 `collage` 和 `figcopy` 两个命令指向同一入口。现有 provider 插件字符串保持
兼容，例如：

```text
collage.providers.yibu:YibuVisionProvider
collage.providers.yibu:YibuImageProvider
collage.providers.birefnet:BiRefNetLiteMattingProvider
```

## 仓库边界

- `tests/`：自动化回归。
- `examples/`：可选示例，不是运行时项目目录。
- `COLLAGE_MVP_PLAN.md`：原始产品/技术约束，不是操作手册。
- 根目录的 `work/`、`templates/`、`artifacts/` 仅作为旧版兼容忽略项；
  新流程应写入外部数据目录。
- 缓存、模型权重、客户图片、密钥和生成结果不提交到 Git。
