# 问答复核、完整素材与独立图层改造

2026-09-11 实施记录。这里区分离线 fixture、真实模型试验和待人工确认的内容。

## 可以使用的功能

1. **问答复核**：识别疑问逐项回答；“其他错误或补充说明”默认空白。提交后 VLM 返回完整纠正稿，可继续反馈。只有当前版本无未决问题且手动确认，才能制作。
2. **独立素材制作**：可执行简单图形由代码绘制，其余固定装饰默认参考生成。检查原始边界、透明背景、组成和完整文字；每件最多初次生成＋一次修复。保留失败原图、检查和修复记录。
3. **图层编辑**：生成后单独拖动、改尺寸、旋转和调整层序；保存新项目版本，固定素材不重新生成，旧版不覆盖。

## 改动代码

| 代码块 | 作用 |
|---|---|
| template/analysis.py、schemas/draft.py、schemas/reviewed.py | 独立装饰、可执行 shape 和待确认完整文字 |
| template/review/feedback.py、studio/review_session.py | 问答修订历史、同版本快照、最终确认、重复请求保护 |
| studio/review_server.py、studio/static/review.js、workbench/application.py | 同一套问答交互与异步纠正任务 |
| template/build/overlays.py、asset_validation.py、providers/yibu/image.py | 全幅原始输出检查、语义/文字检查、单件修复与缓存 |
| studio/workbench/layout.py、static/layers.js | 本地独立图层编辑、正式 Renderer 预览、不可覆盖的布局修订 |
| devtools/real_trial.py、template/automatic.py | 新试验默认进入标准工作流；旧实验也复用标准素材构建器 |

## 已完成验证

- Python 回归：**178 passed in 23.29s**。独立临时目录运行，不复用旧测试输出。
- [真实浏览器 fixture 验证](browser_03/validation.json)：使用 Edge 与合成图，验证问答、默认空白补充项、VLM fixture 纠正、手动确认、实际指针拖动、尺寸、旋转、排序、正式渲染预览和新版本保存。
- 浏览器示例中的圆形和虚线框都是代码绘制的测试图形，VLM 回答也是明确的 fixture，**没有真实模型调用**，不证明 S02 生图质量。
- [纠正后界面](browser_03/02_corrected.png)、[独立图层编辑](browser_03/04_moved.png)、[新版本](browser_03/06_new_version.png)。
- 另外覆盖了旧版本确认、清空 questions 绕过、无回答、失败后原稿保留、未知请求不重放、裁切重试、缺少组成、错字、无效布局和原验收保护。

## 打开工作台

本次已启动 [S02 问答复核页](http://127.0.0.1:8775/projects/s02-review-20260911-01/review)，使用已授权的本机凭据与 Qwen3.8-Max。尚未填写任何真实客户回答。

## 真实样板项目

[s02_project.json](s02_project.json) 对应外部数据目录中的 S02 标准工作流项目。使用 Qwen3.8-Max 实际分析，识别到 4 个照片槽、7 个独立装饰并提出 3 个问题，阶段为 awaiting_review。问题与固定文字仍需用户核对，没有代填答案或写入人工通过。

客户替换照片没有上传到新 VLM 或 Seedream。已有替换照片继续按原授权在本地使用。

## 单件真实 Seedream 检查

本次还使用已授权的 S02 参考区域做独立回形针生成探针，外部试验目录：
- pipeline_s02_asset_probe_20260911_01：两次图片返回，因色键背景残片触发检查而拒收，保留原始输出。
- pipeline_s02_asset_probe_20260911_02：使用修订后的色键分离、PNG 输出及无角落标记参数重新验证。首张通过本地边界检查后被 Qwen 判为组成不完整，修复图仍触发边缘检查，故停止并拒收。

[真实试验记录](real_asset_validation.json)：累计 4 次 Seedream 图片返回、1 次 Qwen 素材检查；**没有得到可验收的回形针成品**。这说明失败门禁和有限修复已实际生效，不证明生成质量已解决。

这是单件生成环节验证，不是整套 S02 模板通过，也不是客户照片效果验收。程序和 VLM 检查都可能漏检，最终仍需人工查看。

Seedream 输出参数依据 [官方图片生成教程](https://docs.byteplus.com/api/docs/ModelArk/1824121)，通过现有 Yibu 审计代理实际发送；不增加新 provider。所有模型调用继续记录来源，关闭角落标记不改变其 AI 生成事实。
