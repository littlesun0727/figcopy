# 内网 Provider 接入验证记录

日期：2026-09-14。分支：`feat/intranet-model-providers`。基线：`7238c19`。

代码接入及本地回归已完成。**所有模型响应均来自本机 HTTP 模拟器，没有调用真实 Yibu、内网 VLM 或 A100 模型。** 浏览器测试使用真实 Edge/Chromium，但参考图和生成响应都是合成 fixture，不能据此评价模型画质或真实推理耗时。

## 验证结果

| 项目 | 实际结果 |
|---|---|
| 全量回归 | 358 passed，0 failed，0 skipped；其中 13 项浏览器测试 |
| 本次新增覆盖 | 24 项：22 项接口/工作流测试，2 项浏览器测试 |
| 静态检查 | `python -m ruff check collage tests` 通过 |
| 四种组合 | Yibu＋Yibu、内网 VLM＋Yibu 图片、Yibu VLM＋Qwen 图片、内网 VLM＋Qwen 图片均生成本地预览，停在待人工验收阶段 |
| 全内网运行 | 无 Yibu Key 时可以配置、新建、恢复失败项目并制作；生成请求只到达所选通道 |
| 项目与修订 | 默认选择不改已有项目；反馈走项目 VLM；单件切回 Yibu 图片后新修订记录选择，原项目与模板哈希保持；背景修订继承选择并可修改 |
| 缓存与失败 | 新通道配置区分缓存，Yibu 缓存键不变；多个 Qwen 实例串行调用；忙时有限等待，超时不自动重发；明确失败可重试，不确定记录阻止自动重复请求 |
| 图像后处理 | 原始 PNG 保留；背景恢复原画布且保护区域像素不变；装饰保留高分辨率输出，继续本地去底和主体范围排版 |
| 原有功能 | Yibu 默认参数/鉴权/审计、客户换图、布局及照片附属关系相关回归通过 |

计数、耗时、模拟组合和产物指纹见 [validation.json](validation.json)。原有 334 项回归与新增 24 项合计 358 项，浏览器样例重复复测不重复计数。

## 实际产物

这些文件复制自本次自动测试的工作区。24×16 的参考图和结果只用于验证协议、坐标与文件链路；模拟图片服务按请求尺寸绘制纯色图形，不执行 Prompt 的美术语义。

- [合成参考图](reference.png)、[最终本地预览](result.png)。
- [Qwen 协议模拟器原始背景 PNG](background_raw.png)、[本地保护合成后的背景](background.png)。
- [单件原始 PNG](overlay_raw.png)、[本地去底后的高分辨率素材](overlay_processed.png)。
- [浏览器新建并保存项目服务选择](browser_create.png)、[浏览器恢复失败项目](browser_retry.png)。

浏览器截图中项目识别服务最后切回 Yibu，是测试“项目选择独立于工作台默认值”的结果；并未发起真实 Yibu 请求。截图中的小参考图也是上述合成 fixture。

## 复现本地回归

在仓库目录执行：

```powershell
python -m pytest -q
python -m ruff check collage tests
```

仅验证本次新增入口：

```powershell
python -m pytest -q tests/test_intranet_providers.py tests/test_provider_migration_browser.py
```

浏览器测试自动查找 Windows Edge/Chrome，也可通过 `FIGCOPY_TEST_BROWSER` 指定 Chromium 可执行文件。没有浏览器时这些用例会 skip；本次 13 项均实际运行且通过。模拟服务只监听回环随机端口，不访问公司内网。

## 真实联调：blocked

用户已提供 A100 地址 `http://127.0.0.1:8037`，并说明该环境无法访问公司内网，因此本次未尝试真实请求。用户报告独立服务部署和单次生成成功，本端未复测。

后续在可访问服务的机器上，按 [操作指南](../../USAGE_GUIDE.md#在内网做实际联调) 完成：

1. 配置内网 VLM 地址和 Key、A100 地址，检查 `GET /health`。
2. 使用获准的真实参考图，完成识别、反馈纠正、人工确认、制作、客户换图和最终预览。
3. 查看固定背景接缝、手写细线、纯色去底及重叠边框效果，记录真实输入/输出和耗时；按需单件重做。
4. Yibu 可用时验证混用和切回；确认原版本保留，再由用户人工验收。

真实画质、内网认证、服务支持的输出预算和推理耗时仍待联调；没有把模拟结果记为真实模型通过。
