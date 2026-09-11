# 自动重建基线与 A1 窗口检查

本目录遵循仓库根目录的 FIGCOPY_AUTO_REBUILD_IMPROVEMENT_PLAN.md。

- `catalog.json`：十张原始样板的指纹、目标能力及评测状态。
- `policy.json`：替换单位、素材制作、坐标和近似边界的版本化规则。
- `annotations.json`：经授权查看十张缩略预览后登记的评测对象、粗区域和未决项。它不是精确 mask，也不是生产自动识别输入。
- 基线命令只输出元数据，不复制样板图片、密钥或制作端绝对路径。

## A1 的实际入口

```powershell
python -B -m collage window-demo --out artifacts/auto_rebuild_a1
python -B -m collage probe --template artifacts/auto_rebuild_a1/template --out artifacts/auto_rebuild_a1_recheck --expectations artifacts/auto_rebuild_a1/evaluation/expectations.json
```

每次使用新的输出目录。演示使用程序构造的四个照片窗口、不规则局部 mask、程序虚线框、不透明和半透明前景。主要输出：

- `template/template.json`：v2 模板与固定素材。
- `evaluation/expectations.json`：独立的离线评测要求。
- `probes/preview.png`：编号素材的合成图。
- `probes/visibility/*.png`：每个槽位在最终画布上的贡献，0 表示完全被遮住，255 表示完整显示。
- `probes/quality.json`、`probes/inspection.html`：分项检查和可视报告。

`probe` 将某个槽分别填黑、填白，保持其它输入一致，用输出差分测量这个槽经过后续图层后的实际贡献。已知允许的遮挡体现在离线评测 mask 中；新增遮挡、错误层序、空窗口或多露出的区域会失败。alpha 容差只处理 8-bit 运算舍入，不是未标定的几何宽容度。

无 `--expectations` 时输出诊断状态 `unverified`，完全不可见的窗口仍会失败。检查失败时 CLI 返回非零退出码并保留报告。探针的 `passed` 只代表这次窗口检查通过；报告始终标记 `production_acceptance=false`。旧照片残留、文字语义、客户构图和新主体 alpha 不在此检查范围内。

## 格式与兼容

旧 `collage-reviewed/1`、`collage-template/1` 与原人工 CLI 保持原语义。

新增制作规格 `collage-build/2` 使用 `status=planned` 和独立 `provenance`：

```json
{
  "kind": "automatic",
  "policy_version": "auto-rebuild-policy/1",
  "evidence_sha256": ["实际决策证据的小写 SHA-256"],
  "unresolved": []
}
```

上述哈希文本是说明，占位内容不能通过校验。人工来源 `human` 另需真实的 `review` 记录；`automatic` 和 `fixture` 不接受伪造的人工审核字段。未决项、缺失来源证据或非法证据哈希会阻止制作。新 `preserve` 动作要求已分离的透明素材，保留其局部画布；`basic_shape` 继续复用本地图形绘制。

v2 图片槽将 `clip_mask_sha256` 与模板绑定，取整后为空或全黑的窗口会被拒绝。坐标基于 EXIF 规范化工作图，槽位 mask 在旋转前使用局部尺寸；旋转沿用现有顺时针、以槽位中心为轴的约定。框线笔画受局部素材边界裁切，固定前景按有序 asset 层合成。

自动/fixture 制作的模板为 `needs_validation`。A1 没有自动发布入口，旧 `approve` 仍只处理人工 `needs_review` 模板。后续 A2/A5 必须补齐真实内容检查、自动验收证据和状态流程。

## 凭据文件

现有 provider 支持显式 `YIBU_CREDENTIALS_FILE`，读取 JSON 中 `api_keys` 数组的第一把非空 Key；显式 `YIBU_API_KEY` 优先，旧 `YIBU_SHARED_PATH` 继续兼容。文件里的 `base_url` 不覆盖本机审计代理路由。工作台显示凭据来源类型，并在“清除凭据”时移除进程内的文件引用，原凭据文件保留。

```powershell
.\examples\configure_yibu.ps1 -CredentialsFile ..\yibu_credentials.local.json
python -B -m collage studio
```

只向 provider 解析提供此文件路径，密钥不进入源代码、模板和基线报告。

## 真实样板基线

```powershell
python -B -m collage benchmark-baseline --samples <样板目录> --catalog benchmarks/auto_rebuild/catalog.json --out artifacts/auto_rebuild_baseline.json
```

`<样板目录>` 需替换成获准读取的实际目录。命令核对原始文件哈希，记录 EXIF 变换后的尺寸及 RGBA 像素哈希、Git 提交与工作区指纹、依赖和配置存在性，不调用模型。输入指纹变化时返回 `BASELINE_SOURCE_CHANGED`，已存在的基线不会被覆盖。

当前 A0 仍需精确边界标注与阈值标定。首轮 S02 外发和费用不限已获授权，五张客户人像用于本地处理；当前三个组合不是三组独立素材集，尚未覆盖不同长宽比。其它样板的模型外发和客户照片外发不在本次试验范围内。


## A2 真实试验入口

`python -m collage.devtools.real_trial` 提供 `--reference`、`--materials`、`--out`、`--authorize-upload`、`--no-fee-limit`，以及 `--stage structure|geometry|all`。前两个路径必须指向已授权素材，输出使用新的私有目录；此入口专用于已明确不限费用的开发试验。

使用现有 `YIBU_CREDENTIALS_FILE` 加载凭据，`FIGCOPY_DATA_DIR` 指向制作端数据根目录。`YIBU_VLM_MODEL=qwen3.8-max` 仅覆盖当前试验；Qwen 不沿用 Kimi 的 reasoning_effort。全局默认仍为 Kimi high / Seedream。

本地人像定位需要可选依赖 `auto-trial` 和经核验的 OpenCV YuNet 权重。权重从 `FIGCOPY_FACE_MODEL` 或数据根目录的 `cache/face_detection/face_detection_yunet_2026may.onnx` 读取；代码不自动下载，不把客户照片交给外部模型。头部矩形是构图近似，不是头发分割。

输出包括原始模型请求及提示词、结构、候选边界、几何复核、固定素材、模板、探针、换图和 `comparison.html`；未完成的阶段不会伪造对应产物。上游失败仍保留已有结果；已确认的几何 HTTP 服务失败最多重试一次，未知完成状态不盲目重发。

首次 Kimi 请求成功，但两次几何请求返回 HTTP 504；该轮没有模板和客户结果。[查看 Kimi 失败记录](../../artifacts/auto_rebuild_a2_s02_real/README.md)。Qwen 的结构、几何和一次纠错均成功返回，但保留五槽误判及未决项；用户随后明确要求继续，已经生成三张诊断换图。

模板及结果均属开发诊断，`production_acceptance=false`。可见窗口探针、模型辅助检查、局部头部可见比例及重复像素一致各验证不同问题；单项通过不能代替完整 A2 验收。

## 按用户要求沿用错误结构继续

仅用于明确要求“结构先将错就错，继续下游”的开发诊断：在已有成功模型证据的私有试验目录，给原 real_trial 命令增加 --continue-known-structure，并保持 --stage all。

此模式直接加载最近已完成的结构决策并验证证据一致性，不重新调用结构分析，也不清空 unresolved。生成的模板 provenance.kind 为 diagnostic，continuation 记录指令来源、原因和结构哈希；模板不能作为 ready 发布。缺失继续记录或修改过模型结果仍会报错。

三张本地换图一生成就保存 comparison.html，不必等在线模板复核结束才能查看。后续检查结论与用户已知的结构问题都会保留。[当前 S02 三组结果](../../artifacts/auto_rebuild_a2_s02_qwen/README.md)。
