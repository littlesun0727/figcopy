# S02 Qwen 诊断继续结果

按用户要求沿用 Qwen 的五槽结构，已生成三组真实人像换图；已知识别错误和两项边框疑问保留。模板辅助视觉检查已结束；模型给出 passed，但漏检了已知问题，离线评测已注明。

- [打开完整对照报告](../../../figcopy-data/benchmarks/auto_rebuild_s02_qwen_20260911_01/comparison.html)
- [组合 G1](../../../figcopy-data/benchmarks/auto_rebuild_s02_qwen_20260911_01/renders/G1/result.png)
- [组合 G2](../../../figcopy-data/benchmarks/auto_rebuild_s02_qwen_20260911_01/renders/G2/result.png)
- [组合 G3](../../../figcopy-data/benchmarks/auto_rebuild_s02_qwen_20260911_01/renders/G3/result.png)
- [窗口试拼](../../../figcopy-data/benchmarks/auto_rebuild_s02_qwen_20260911_01/probes/inspection.html)
- [本地验证](validation.json)

三组使用同一批五张照片的不同分配组合。照片仅在本地裁切、缩放和合成，没有重新生成客户人物；同输入重复合成的解码像素一致。满版主图的头部估算区域仍有遮挡，不能把文件已生成视为构图已通过。

模板来源为 diagnostic，原始模型结果未修改；本次用户允许继续记录为一次运行决策覆盖。全局默认 Kimi high / Seedream 未改变。源码仓库只保留入口和不含原图的测试记录，实际图片位于私有数据目录。

四个手绘前景来自本地白色笔画提取，Seedream 调用为 0；回形针、烟花仍有不完整或框线夹带。详见私有报告中的离线评测。
