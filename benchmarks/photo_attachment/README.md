# 照片与附属装饰：实现验证

日期：2026-09-14。代码与离线验证已完成，真实模型新 prompt 试跑 blocked。

照片所属的框、垫纸等按 below → 照片 → above 展开，照片单元参与全局层序。独立装饰保持 attachment=null，可插在照片之间。只支持一层关系，不增加逐件 VLM 复核或质量重试。

## 验证结果

- 全量 pytest：**334 passed，0 failed，0 skipped，78.82 秒**，包含 11 项实际 Chromium/Edge 浏览器测试。
- 本次新增 13 项附件相关用例（含 2 项浏览器测试）；覆盖遮挡、独立装饰、组变换、照片裁切、slot mask 缩放、缺失恢复、另存、背景修订和反馈纠正。
- 正式预览与保存后 PNG 做像素一致性检查。布局调整和另存的模型调用数为 0；单件缺失恢复只调用 mock 图片 provider 一次，反馈纠正只调用 mock VLM 一次。
- Ruff check 通过。测试代码位于 tests/test_photo_attachment.py、tests/test_photo_attachment_browser.py；其他既有 fixture 已更新到新协议。
- **真实模型调用 0 次**：新进程没有凭据，错误 YIBU_CREDENTIAL_MISSING。正在运行的旧 studio 只在进程内存持有 Key；没有读取该内存或重启用户进程。本次没有验证 VLM 对真实参考图的新归属识别质量。

复跑命令（PowerShell，临时目录选用短且未占用的名称）：

```powershell
python -m pytest -q --basetemp "$env:TEMP\fc-attachment-check"
python -m ruff check collage tests
```

## 图像产物

以下均为合成色块/代码图形或测试浏览器截图，不含真实客户图片。

| 文件 | 含义 |
|---|---|
| [occlusion-comparison.png](occlusion-comparison.png) | 左：把框统一置顶会穿过上层照片；右：框属于照片，正确遮挡 |
| [occlusion-before.png](occlusion-before.png)、[occlusion-after.png](occlusion-after.png) | 对照原尺寸图片 |
| [structure.png](structure.png) | 制作前的编号照片结构预览 |
| [transformed-restored.png](transformed-restored.png) | 移动、等比放大、旋转、重排后恢复缺失附属物 |
| [mask-resized.png](mask-resized.png) | 带圆形槽位蒙版的照片放大并旋转，原蒙版文件哈希保留 |
| [review-browser.png](review-browser.png) | 真实浏览器内确认页，包含结构预览 |
| [layout-browser.png](layout-browser.png) | 真实浏览器内布局调整与正式 Renderer 预览 |

遮挡样例：红色照片在绿色照片下方；白色虚线框跟随各自照片。洋红独立装饰放在两张照片之间，因此被绿色照片遮住；青色独立装饰位于顶层。

另有对用户旧 test1 的本地层序重放，仅保存在仓库忽略目录 .tmp_preview/photo-attachment-real/。显式设置三件已知边框的归属后，截图中穿过上层照片的下层框消失；源工程文件未改写。这不是生产旧协议迁移器，也不是新模型识别试跑。

## Prompt 与新协议

- [analysis-prompt.txt](analysis-prompt.txt)：真实 provider 拼接函数经 stub transport 捕获的请求文本，没有发网请求。
- [analysis-prompt-before.txt](analysis-prompt-before.txt)：改前按源码拼接的基线。
- 相同 1320×1767 画布及默认产品策略：**4,120 → 3,211 字符，减少 22.1%**。包含空白和 JSON 文本，不是 token 统计，不含图像输入。
- 首次分析与反馈纠正共用 collage/schemas/draft_prompt.py，关系规则只维护一份；反馈上下文剔除程序侧 source/provider 等审计字段。
- [draft-example.json](draft-example.json)：可用的人工 Draft / 模型原始返回示例，版本与程序元数据由分析入口补齐。两张照片带框，包含 below 垫纸与两个独立装饰。
- 三种新版本为 collage-draft/3、collage-build/4、collage-template/4。Template 保存 overlays 布局定义和根层 layer_order，不再持久化平面 layers；素材缺失仍保留定义。
- [validation.json](validation.json) 为机器可读验证结果。

## 继续真实工作流

重启 studio，重新输入此前仅存在内存的 Provider Key，新建项目并上传参考图。先在确认页核对归属与结构预览，再制作、上传、检查成图、调整整组或单件重做，最后人工验收。

旧工程可以保留，但新代码不提供旧协议读取或自动迁移。当前 Key 阻塞只影响真实模型复测；不能把本目录的离线通过写成真实模型验收。
