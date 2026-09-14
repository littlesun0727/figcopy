# 图形字段规则验证

日期：2026-09-14。合成图与 fixture provider 验证，真实模型调用数为 0。

## 实现

共享规则位于 [schemas/shapes.py](../../collage/schemas/shapes.py)，由 Draft 校验、确认编译和模型提示复用。

- 圆角半径接受 0..8192 的有限数值，保留 8.5 等小数。
- Draft 按 kind 要求参数：圆角框需要 radius；虚线框需要 dash/gap；其他图形不要求这些无关字段。
- 共同字段为 kind、outline、width；fill 省略时编译为 null。
- 确认时补齐内部字段；未使用的 radius/dash/gap 为 0/1/0，保持 Draft 原文和旧图形像素语义。
- width/dash/gap 的整数值浮点表示（如 2.0）在确认时无损转换；2.5、数字字符串和 bool 不会被猜测为整数。
- 错误颜色在 Draft 阶段报 INVALID_COLOR；reference_generate 携带 shape 的不一致也提前报错。
- 初次识别、问答纠正使用 shape-fields/2 的共同 prompt；直接调用 YibuVisionProvider 时也会补充同一合约，每个请求只包含一份。
- 分析 prompt 版本更新为 collage-draft/6，反馈 prompt 版本更新为 collage-review-feedback/4；未修改用户的模型选择、推理强度或凭据。

已有缺少整个 shape 的旧半自动稿保留原参考生成兼容路径。审查报告中的响应保存/恢复、其他羽化参数开放和其余几何预检不属于本轮字段修改。

## 实际测试

完整离线 pytest：**312 passed in 52.94s**，0 skipped。其中 35 个新图形字段测试、7 个真实浏览器测试，浏览器中的模型使用模拟实现。Ruff 和 git diff --check 通过。

首轮为 310 passed、2 failed；两项均因测试临时文件路径达到 Windows 260 字符限制。使用全新的较短 basetemp 重跑完整测试后全部通过。

验证覆盖：

1. 四类紧凑 shape 经过 fixture VLM → Draft → 反馈纠正 → 确认编译 → 构建 → 本地 PNG。
2. 流程停在 awaiting_approval；模板为 needs_review，没有代替真人批准。
3. 新旧整数图形的解码像素一致。
4. 圆角小数不取整；缺少有效参数、NaN/Infinity、bool、错误类型和错误颜色得到稳定错误。
5. 实际发送给审计代理 stub 的提示词包含一份新规则，标准和自定义 prompt 入口均覆盖。
6. 生成装饰、照片背景、旧工作流及人工确认规则的既有回归通过。

机器记录见 [validation.json](validation.json)。下面的 PNG 均复制自本轮测试实际输出，尺寸很小，只用于验证程序行为：

| 图形 | 完整合成图 | 独立装饰 |
|---|---|---|
| 普通矩形 | [render](rectangle-render.png) | [overlay](rectangle-overlay.png) |
| 圆角矩形，radius=8.5 | [render](rounded_rectangle-render.png) | [overlay](rounded_rectangle-overlay.png) |
| 椭圆 | [render](ellipse-render.png) | [overlay](ellipse-overlay.png) |
| 虚线框 | [render](dashed_rectangle-render.png) | [overlay](dashed_rectangle-overlay.png) |

复测命令（仓库根目录）：

```powershell
$shapeTemp = Join-Path $env:TEMP ("sf-" + [guid]::NewGuid().ToString("N").Substring(0,8))
python -B -m pytest -q -p no:cacheprovider --tb=short --basetemp $shapeTemp
```

真实 test2 未重跑：原失败响应没有保存，仍需重启工作台、恢复原 Provider 设置后重试分析，再由用户复核。以上结果不代表真实模型已经通过视觉验收。
