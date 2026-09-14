# 分析失败诊断与任务日志验证

日期：2026-09-14。用户授权修复失败 Draft 丢失、前端错误详情缺失、过程日志不可见，并对齐提供的 A100 `/edit` 示例。

## 实现范围

- 初次分析与反馈纠正各建立独立的 `analysis/attempts/<attempt_id>/`，保留脱敏后的原始服务响应、助手回答、校验前候选、响应元数据和完整校验报告。请求失败没有回答时只记录实际错误，不生成伪造草稿。
- JSON 解析、输出截断、字段类型及补齐画布元数据后的校验，均在可能抛错前保留已有证据。只有通过完整校验的候选进入正式 Draft 与缓存；失败尝试保留，现有确认保护继续生效。
- `generation_brief` 明确要求字符串，`questions` 明确要求非空字符串数组；无问题用 `[]`，无生成说明的简单图形用空字符串。分析提示版本为 `collage-draft/8`，反馈提示版本为 `collage-review-feedback/6`。未新增模型自动纠正、重试或类型强制转换。
- HTTP 错误保留操作名、状态、请求 ID 与最多 16 KiB 的脱敏响应正文。非 JSON 的服务响应也能作为诊断文本保留。
- A100 请求只包含 `image_base64`、`prompt`、`seed`，输入图片仍在本地保持比例并适配尺寸；成功响应继续解码 PNG。适配器缓存身份升级，避免误用旧请求协议的缓存。
- 工作台保存按项目、任务隔离的 INFO/WARNING/ERROR 日志，每个任务保留最近 1000 条，界面列出最近 30 个任务与分析尝试。日志存放在运行数据目录的 `logs/jobs/<project_id>/<job_id>.json`，重启后可读取；未完成的旧任务明确标记中断。
- 前端复用 2.5 秒轮询并按序号增量读日志；失败自动展开，完整字段问题与 HTTP 详情可查看，历史任务可切换，诊断记录和候选可打开或下载。模型内容按纯文本显示，不执行其中的 HTML。
- 不写入密钥、图片 Base64 或制作端绝对路径；诊断文件经过脱敏，因此不保证与线上响应逐字节一致。详细字段问题不截成前五条。

## 验证证据

本目录的 `pytest.xml` 为本次最终相关回归生成的实际 JUnit 报告。全部模型响应来自本地 fixture 或协议模拟器；浏览器测试使用真实 Chromium 与合成参考图。

最终批次：141 项，140 passed、1 failed，7 项真实浏览器用例全部通过。唯一失败是既有 HTTP 工作台用例在未带 CSRF Token 的请求上遇到 Windows `ConnectionAbortedError / WinError 10053`；该用例独立复核为 1 passed，报告为 `http-api-recheck.xml`。保留原批次的失败状态，不把复核结果伪装成一次全绿运行。两份报告中的本机路径已脱敏。Ruff 与 `git diff --check` 均通过。

浏览器截图：[全部字段错误](schema-fixture.png)、[HTTP 500 错误详情](http500-fixture.png)。两张图均为合成 fixture 场景，不是真实模型结果。

覆盖场景：错误 JSON、无助手文本、输出截断、七条字段错误、画布校验失败不进入缓存、失败重做保留既有 Draft、401 无草稿、JSON/文本 500 错误脱敏、跨项目并发日志隔离、增量游标、日志上限、磁盘写入失败、进程重启、诊断文件路由和路径校验、浏览器刷新、任务切换、HTML 内容不执行，以及原有内网/Yibu、反馈复核与图形字段流程。

测试命令（从仓库根目录运行，`work` 父目录需存在；使用新的 basetemp 避免覆盖已有产物）：

```text
python -B -m pytest -q -p no:cacheprovider tests/test_diagnostics.py tests/test_diagnostics_browser.py tests/test_analyze_review.py tests/test_intranet_providers.py tests/test_workbench.py tests/test_review_feedback.py tests/test_workflow.py tests/test_yibu_provider.py tests/test_logging_config.py tests/test_shape_fields.py tests/test_provider_migration_browser.py tests/test_review_defaults_browser.py --tb=short --basetemp work/d7 --junitxml=benchmarks/diagnostics/pytest.xml
python -B -m pytest -q -p no:cacheprovider tests/test_workbench.py::test_workbench_http_api_runs_both_human_gates --tb=short --basetemp work/d8 --junitxml=benchmarks/diagnostics/http-api-recheck.xml
```

初轮使用系统 pytest 临时目录时遇到 Windows 权限错误；改用仓库内独立短路径后执行。较长的初轮路径还触发了既有背景修订目录复制的 Windows 长路径限制；分析尝试使用短标识符，最终验证使用短测试根目录。初轮发现的 verbose 日志继承问题已修复。

浏览器复跑中发现启动文件短暂占用和脚本变量尚未就绪的时序问题；已修正工具的端口就绪等待，并给原 Provider 浏览器测试补充变量存在判断。纠正结果写盘失败也作为失败尝试保存，避免校验通过后误报整个纠正完成。

## 使用与边界

同步代码后重启工作台，打开项目页的“运行日志与分析诊断”。历史失败若未保存原始响应，本次改动不能追溯恢复；从下一次分析开始建立证据。重启时原来只保存在进程内存中的 Provider 设置需按原选择重新配置。

另一台主机的真实 VLM 和 A100 服务验证仍为 blocked：本次没有该服务的可访问地址与凭据，没有重新发送客户图片或调用真实模型。三字段协议已通过本地模拟验证，不能据此认定远端 HTTP 500 根因已经解决；更新后可通过保留的错误正文进一步定位。
