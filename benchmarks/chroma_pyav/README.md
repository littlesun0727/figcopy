# PyAV 色键去底验证

本次将固定装饰的 `remove_chroma_background` 切换为本地 PyAV。绿色和蓝色色键使用 `colorkey + despill`，其余已有色键使用 `colorkey`。不增加 NumPy 核心依赖、GPU 运行时或模型权重。

## 行为与兼容性

- 保留现有函数入口及 `chroma_key`、`chroma_tolerance` 字段。默认 tolerance=40、softness=24 对应快速试验的 similarity=0.12、blend=0.16。
- 从外围 8% 区域中与指定色键色相接近、具有可见 alpha 的像素取背景中位数；背景变化较大时按采样跨度扩大去底半径。
- 直接在原始画幅上生成软 alpha；显式处理 PyAV 行对齐，支持奇数宽度；输出 alpha 乘以输入 alpha，不复活原本透明的像素。
- 不再对生成装饰追加旧的 alpha 内缩。被前景线条包围的同色色键区域也会去除；不再保留旧 flood fill 留下的封闭背景块。
- 处理版本独立于模型生成缓存键。版本变化时复用已落盘原图、重新检查候选像素；生成上限仍为两次。未知生成或视觉检查请求保持阻断。
- 缺少 PyAV 或必要滤镜时，在调用生成模型前返回稳定业务 code 并标记 blocked。

## 实测产物

输入为用户明确指定的两张既有 Seedream 输出。本次只做本地计算，没有新的模型请求，也没有修改输入文件。数值证据见 [validation.json](validation.json)。

| 素材 | 尺寸 | 与已认可的 PyAV 平衡档试验成品逐像素相同 | 本机热调用完整去底 | 读取、去底及 PNG 保存 |
|---|---|---|---|---|
| 星芒 | 2176 × 1792 | 是 | 142.81–151.94 ms | 2766.64 ms |
| 回形针 | 1888 × 2176 | 是 | 145.38–146.77 ms | 2765.87 ms |

完整去底计时包括实际底色采样、滤镜执行、像素传递与输入 alpha 保留；端到端是各一遍测量，大部分时间消耗在保存原尺寸 PNG。机器、编码设置和并发负载会影响这些数值。试验的独立原始输出保存在本地 `artifacts/pyav_chroma_integration/`，源图片和处理后图片未加入本次提交。

两张 PNG 均保持原始尺寸与 0–255 alpha 范围；源 SHA-256 不变；连续三次重复处理得到相同像素。这是对已生成素材的本地处理验证，不是新模型输出或完整模板验收。去绿色后仍可能有灰色边缘，保留既有的后续内容检查。

## 回归验证

- `python -B -m pytest -q -p no:cacheprovider --tb=short --basetemp artifacts/pyav_integration_tests_final`：**227 passed in 23.57s**。
- 本次修改的 Python 文件通过 Ruff 检查，补丁通过 `git diff --check`。
- 独立进程禁止导入 NumPy后，PyAV 去底仍正常完成。
- 测试覆盖细线与星点、封闭色键区域、软 alpha、输入透明度、非对齐行宽、已有色键调色板、蓝色去污染、缺少依赖及滤镜错误、处理版本迁移、生成上限与未知检查阻断。
- 图像单元测试使用程序构造 fixture；缓存与语义检查使用 mock provider，不能视为真实 VLM 验收。

运行环境：Windows x64、Python 3.14.6、Pillow 12.3.0、PyAV 18.1.0。
