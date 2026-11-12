# MiniVectorDB 优化报告 v5

- 分支：`minivectordb-opt-v5`
- 优化基线：`f10eddfdf209b0fd6e374da7d271e39e41b4b2c9`（已计分 rewrite 版本）
- 优化代码 tip：`e21b23b844b0480b0cf856aaff7be783b217e740`
- 报告日期：2026-11-12
- 构建/运行环境：未改动。`Dockerfile`、`requirements.lock`、`.dockerignore` 与基线逐字节一致；仍为固定 digest 的 gcc/python 镜像、断网 RUN、Python 标准库 + 自编译 `libscreen.so`。

## 1. 摘要

本轮在已计分版本上追加 10 个提交，只修改 5 个源码文件，没有新增依赖、系统包或构建步骤：

```text
minidb/engine.py   41 +-
minidb/native.py   32 +-
minidb/sql.py      35 +-
minidb/storage.py  21 +-
native/screen.c    39 +-
```

同机、同 Docker 限额、同负载的三轮中位数：

| 阶段 | 指标 | 计分基线 f10eddf | 当前 opt-v5 | 变化 |
|---|---:|---:|---:|---:|
| 写入 | T | 7.268 s | **1.718 s** | 4.23×，-76.4% |
| 写入 | B | 20.3 MiB | 20.3 MiB | 持平 |
| 写入 | M | 24.0 MiB | **20.8 MiB** | -13.3% |
| 查询 | T | 3.230 s | **1.333 s** | 2.42×，-58.7% |
| 查询 | B | 25.2 MiB | 25.2 MiB | 持平 |
| 查询 | M | 28.8 MiB | **25.9 MiB** | -10.1% |

所有基准轮次查询结果与全扫真值一致，`bad=0`。

说明：官方 E 分数的 `T_base/B_base/M_base` 未公开，本报告不推算平台绝对分。按官方公式、以 f10eddf 自身为基线做归一化，当前综合 E 为 `0.6505`（基线自比为 `0.5`）；这不是平台成绩。

## 2. 基准口径

本地复现使用 10,000 行 × 128 维 float32，1000 次 TopK，K=10，一半查询带 `WHERE tag=N` 等值过滤；写入与查询使用独立的全新数据目录，查询前重新装载数据并执行 `CREATE VECTOR INDEX`，预热 10 次后计时；容器参数：

```text
--network=none --cpus=2 --memory=128m --memory-swap=128m
--pids-limit=128 --read-only --cap-drop=ALL --security-opt no-new-privileges
```

T 为驱动侧墙钟；B 为 cgroup `io.stat` 的 rbytes+wbytes；M 为 `memory.peak`。统计前在容器内执行 `sync`，避免延迟回写低估 B。每版独立跑三轮取中位数。该结果是本地对比，不是赛事官方评分。

## 3. 优化项

### 3.1 Covering TopK 跳过行读取

提交：`892c4f2a9`

当 TopK 只投影主键时，索引中的候选 ID 已经能回答查询，不再按 ID 回表打开主键/记录文件，也不再解码行。对其他投影保持原路径。

收益：该阶段查询 T 3.300→2.805 s（本地单轮序列），并减少查询 I/O。

### 3.2 过滤查询启用块内 native TopK

提交：`4701396dd`

过滤后的索引块原先只做 `mv_screen` 阈值筛选，Python 端对全部存活候选逐个做精确 `math.fsum`。现在过滤块同样使用 `mv_screen_topk`：C 维护块内 k 元素最大堆，阈值使用保守误差带 `(1+error)/(1-error)`，返回候选仍是真实 TopK 的超集，最终距离和 (距离, 主键) 排序仍由 Python 精确完成。

收益（本地 200 查询剖析）：过滤 `flat_topk` 1.19→0.61 ms，过滤查询执行 2.17→1.61 ms；结果与精确路径逐条一致。

### 3.3 顺序读块扩大

提交：`445f719a4`、`29c507a48`、`4a1f25802`

把索引块的 64 KiB 提升到 512 KiB、1 MiB，最终 4 MiB；C 侧 `mv_screen_topk` 的块上限同步放宽。4 MiB 对应 round2 C++ 实现的“64 个 64 KiB 页批量读”思路，减少 mmap 切片、ctypes 调用和块级循环次数。

收益：plain `flat_topk` 0.486→0.379 ms/查询；整体查询中位 1.635→1.363 s（4 MiB 阶段）。同时对 dim16、60,000 行、128 MiB 限额做过大块栈/内存验证。

### 3.4 SQL 向量字面量快速解析

提交：`890fce951`、`53b1819bf`

- 严格 JSON 数字数组形式的 `[...]` 在 tokenizer 中通过 `JSONDecoder.raw_decode` 原地解析为单个 `vec` token，不再生成 128 个数字 token 和 Token 对象；非 JSON 数字语法（如 `+1`、`.5`、`1.`、`01`）、非数字元素、NaN/Infinity、float32 溢出等全部回退旧字符扫描路径，错误码/消息保持不变。
- 回退路径之外的公开 `tokenize()` 行为不变，Parser 只在内部启用快速模式。

收益：

- 查询解析：约 0.59→0.057 ms/查询
- 百行 INSERT 解析：约 5.49→3.60 ms/批
- 写阶段 T（含解析收益）：约 7.26→1.80 s 的主要来源之一

### 3.5 float32 转换检查后置

提交：`2fdd4a5b9`

`vector()` 先做元素类型检查，再一次性 `struct.pack/unpack` 为 float32，最后用 `all(map(math.isfinite, result))` 检查溢出/非有限值。类型错误、巨大整数、float32 溢出、NaN/Infinity 的最终错误码仍一致。

收益：百行 INSERT 的向量转换 2.19→1.10 ms/批。

### 3.6 C 构建冷过滤掩码

提交：`88ec56a4e`

新增可选符号 `mv_id_mask`：Python 把 allowed/excluded ID 排序成 int64 数组，C 对索引块逐行二分生成字节掩码；旧库缺少该符号或 native 关闭时自动回退原 Python 生成逻辑。ABI 仍为 5。

收益：

- 冷过滤掩码约 2.5 ms/万行 → C 路径约 0.1 ms
- 过滤查询剖析：1.47→1.32 ms（组件口径）
- 官方负载只有 10 个过滤值，端到端在噪声内；该优化主要针对隐藏负载中高基数/多过滤值的冷查询

### 3.7 有界复用 Path 和索引段解析

提交：`e21b23b84`

`Storage.path()` 缓存最近 256 个已校验的不可变文件 Path；`index_segments()` 缓存最近一次表的 `(Path, excluded)` 结果。表每次写入/回滚都会替换成新对象，因此缓存以对象身份失效，不会跨版本复用。

收益：查询中位 1.363→1.338 s，写入中位约 1.752→1.702 s；Pathlib/解析开销从每查询热点中移除。

## 4. 正确性与环境验证

- 官方公开用例：20/20 PASS
- 官方环境仿真：断网 no-cache 构建 PASS；命名卷、bind 0777、bind root0700、tmpfs、`--user 1000` 握手 PASS；官方公开 20/20、崩溃恢复、128 MiB 资源冒烟 PASS。仅保留与基线相同的两个已知失败：宿主机 user0700 + 容器 root 的 `/data` 属主场景。
- `checks/contract.py`：78 项 PASS
- native screen：648 screen + 828 TopK + 891 reference-term 检查 PASS（维度 16–4096，含极值、次正规、并列、掩码、畸形长度）
- native TopK SQL：24 组 oracle PASS（K=1/10/127/128/512/1024、极端 BIGINT、事务可见性）
- value groups：24 组 PASS（含事务、回滚、重启、LRU、预算回退）
- bitmap boundaries：32 组 PASS
- projection distances：20 组、lookup boundaries：42 组、merge boundaries：21 组、general boundaries：全部 PASS
- large vector scan：70,000×256、72 MB 索引，64 MiB 限额 PASS
- index mapping：104 项位级/映射检查 PASS
- mapped views：23 组 PASS
- vector codec：102 项 PASS
- vector text：1530 项差分 PASS
- parser scan：6484 项差分 PASS
- `mv_id_mask`：额外 600 组随机差分 PASS（负 ID、INT64 极值、空集合、维度 16–4096、畸形块长度回退）
- dim16 × 60,000 行、128 MiB 容器内 TopK 与暴力真值一致
- 资源检查与崩溃恢复：PASS
- 基准：所有轮次 `bad=0`
- 环境文件：`Dockerfile`、`requirements.lock`、`.dockerignore` 相对 f10eddf 无差异

## 5. 评估过但未采用的方案

| 方案 | 实测/模拟结果 | 结论 |
|---|---|---|
| round2 磁盘 IVF + 三角不等式剪枝 | 官方同款均匀数据，nlist=32/64/100/128 均需扫描 100% 簇 | 无剪枝收益，反而增加建索引 I/O 和 B/M；不采用 |
| AVX2 每 64 维部分和提前退出 | 查询中位 1.36→1.50 s | 额外横向汇总/分支更贵；已撤销 |
| 首次过滤扫描即建值组 | 可省约 40 ms，但违反 `value_groups` 的“首查询不建组、第二查询扫描学习”回归约束 | 未采用 |
| NumPy 批量距离 | float64 1.42 ms/查询、float32 0.77 ms/查询；当前 native 扫描约 0.16 ms | 更慢，且当前镜像无 NumPy；不引入依赖 |
| SQ/RaBitQ 量化预筛 | 精确下界模拟只需精算 0.1–0.3% 的行；但需要新量化码索引和精确分支定界 | 理论收益大，属于索引格式/查询算法大改；不在本版实施 |
| 加载时预建持久倒排过滤索引 | 可省冷过滤全扫，但会改变现有值组缓存契约与索引格式 | 未采用 |
| 延迟提交/group commit | 协议要求逐请求自动提交确认 | 不可用 |

## 6. 剩余瓶颈

当前完整 JSONL 查询往返约 1.33 ms/查询，其中：

- Docker 管道/驱动往返基线约 0.54 ms（用同请求 echo 容器测得，非数据库时间）
- 引擎直接执行约 0.44 ms/查询
- server 入站 JSON 约 0.013 ms、出站约 0.031 ms
- 引擎内 native 块扫描约 0.16 ms，剩余为解析、索引调度、候选精确求和和结果编码

进一步的收益点已进入 1–3% 区间，且需要解析计划缓存、批量候选 terms 或新索引格式；当前平台准入优先，不再继续叠加大改动。

## 7. 复现

```bash
docker build --network=none --no-cache --platform linux/amd64 \
  -t minivectordb:opt-v5 .
python3 protocol_check/run.py --image minivectordb:opt-v5 --output protocol-results/opt-v5
```

基准负载：10,000×128 float32、1000 查询、K=10、交替 `WHERE tag=N` 过滤、独立写入/查询数据目录、2 CPU/128 MiB、三轮中位数；查询结果逐条对照全扫 `math.fsum` + 主键排序真值。

## 8. 提交列表

```text
892c4f2a9 Skip row lookup for covering TopK when only the primary key is projected
445f719a4 Increase native screening blocks to 512 KiB within the block budget
890fce951 Parse strict JSON vector literals as one token
4701396dd Use block-local native TopK for filtered index queries
29c507a48 Raise bounded native screening blocks to 1 MiB
53b1819bf Decode JSON vector literals in place instead of retokenizing
2fdd4a5b9 Defer float32 finite checks until after the packed conversion
4a1f25802 Batch native screening blocks to 4 MiB like the C++ 64-page reader
88ec56a4e Build cold predicate masks natively from sorted ids
e21b23b84 Reuse bounded path and index-segment lookups across queries
```
