# tests/perf — 链路性能基准

测「规则引擎 → 混合语义路由(embedding) → 端侧小钢炮(1B)模型」三段链路的延迟，
数据与解读见 [docs/design/performance.md](../../docs/design/performance.md)。

## 文件
- `bench_pipeline.py` — 基准脚本（分段拆解 + 端到端 Router + 冷启动）

## 运行（设备 /opt/bibit 下，llama-server 8081 / llama-embed 8082）
```bash
.venv/bin/python tests/perf/bench_pipeline.py --n 2        # 稳态基准
.venv/bin/python tests/perf/bench_pipeline.py --cold       # 含索引冷启动构建
.venv/bin/python tests/perf/bench_pipeline.py --no-e2e     # 跳过端到端 Router
```

## 注意事项
- 默认语料是 `tests/ubuntu_cmd/corpus.jsonl`（140 条 rule/model 用例），
  与准确率探针共用同一套语料，保证「性能 + 准确率」可比。
- 端到端部分只跑安全只读查询，避免修改类工具的二次确认干扰计时。
- 模型耗时会随 llama-server 负载波动（p95 约 2.3s），多次采样取均值。
