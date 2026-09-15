# 单件重做原地替换与浏览器断连修复

日期：2026-09-15。

## 行为

- 单件重做使用独立的生成缓存和短路径工作目录，生成完成后替换当前项目中的一件素材；不新建项目、不复制客户照片或其他装饰。
- 先暂存新素材，再更新模板与本地预览。生成失败保留旧素材；本地合成或保存失败恢复原清单、项目状态和预览。成功替换后重新等待人工验收。
- 保留已有槽位、附属物布局、客户图片和绑定设置。旧页面版本和并发重复点击被拒绝；每次后续主动重做调用一次该件 provider。
- 前端按钮改为“生成一次并替换当前素材”，完成后在当前页面更新，不跳转项目。
- GET/POST 的正常响应及错误响应统一处理 ConnectionAbortedError、ConnectionResetError、BrokenPipeError；断连仅记录 DEBUG，不再向同一连接发送第二次错误响应。其他服务器异常继续报告。

## 验证

- Python 3.11（env_py11）：152 项相关回归全部通过。主批次 151 passed，新增上传测试因夹具漏传 slots 失败；补齐合法测试输入后单独复跑 1 passed，生产代码未再变动。
- 覆盖模块：candidate_workflow、workbench_disconnect、layer_editing、photo_attachment、intranet_providers、workbench、diagnostics、background_source、chroma_pyav。
- Python 3.14（已有 websocket/browser-test 依赖）：4 项真实无头浏览器测试通过，覆盖单件重做及日志页面。确认项目列表仍为 1 项、项目 URL 不变、页面实例不变、缺失装饰提示消失、无浏览器脚本异常。
- 断连修复前，确定性注入 ConnectionAbortedError 已复现双重异常；修复后 37 个断连边界用例通过（包含上述 152 项）。
- 修改文件 Ruff 与 git diff --check 通过。

全部模型返回和图片均为本地 fixture，未调用真实生成模型，也未修改用户数据目录中的现有项目。浏览器截图保存在本次测试输出目录 inplace_browser_02 中。
