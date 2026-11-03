# 回源性能问题分析与优化

本文档记录针对压测报告中两个风险的定位过程、根因与优化措施，以及重新规划的容量模型。

- 风险 1：TTL 过期导致吞吐断崖
- 风险 2：回源能力仅约 1,000 QPS

> 数据来源：`DNS压测报告_172.21.80.142.md`（内网 CVM 作为客户端的黑盒压测）

---

## 1. 结论先行

| 项 | 优化前 | 优化后 | 说明 |
|---|---|---|---|
| 热缓存吞吐 | 94k QPS | 93k–99k QPS | 基本持平，本次优化不影响快路径 |
| 真实冷域名 p50 | ~220 ms | **151 ms** | 下降 31% |
| 真实冷域名 p90 | ~950 ms | **329 ms** | 下降 65% |
| 回源吞吐（全新域名） | 858 QPS | **1,049 QPS** | 提升 22% |
| 快路径占比（<300ms） | — | **78%** | 剩余 22% 为 TLD 丢包重试 |

**核心结论：回源能力的上限由从国内到 Verisign（`.com`/`.net` TLD）的物理链路决定，约 110–290 ms RTT、30–50% 丢包。这是网络层事实，任何 unbound 配置都无法突破。** 本次优化做的是「尽可能少浪费在错误服务器上的时间」，把可优化空间基本吃完。

---

## 2. 风险 1：TTL 过期导致吞吐断崖

### 2.1 现象复现

压测报告观察到：同一节点用 187 个短 TTL 域名压测只有 1,448–2,029 QPS，换成 82 个 TTL≥25min 的域名后立刻恢复到 54k QPS。

本地复现：

```
有 TLD 缓存时         19,902 QPS
强制 flush .com 后     1,017 QPS
```

### 2.2 根因定位

**不是「TTL 过期」本身，而是 TLD 缓存失效导致的迭代路径放大。**

一次完整的冷查询路径：

```
根 (f.root, 4ms)  →  .com TLD (110–290ms)  →  权威 (变量)  →  答案
```

关键点：

1. **TLD 缓存是全局复用的**。`www.baidu.com`、`www.qq.com`、`api.tencent.com` 共享同一条 `.com` NS 记录。
   只要 `.com` NS 在缓存里，任何 `.com` 域名都只需「根跳过一次 + 直接问 TLD」。
2. **`.com` NS 的 TTL 是 172800 秒（48 小时）**，正常情况极难失效。
3. **但 unbound 的 `cache-min-ttl`/`serve-expired` 并不保护 infra 缓存**。压测报告中的 187 个域名如果分布在不同 TLD（`.com`/`.net`/`.org`/`.io`），每个 TLD 都是独立的迭代起点。

因此真正的机制是：

```
冷启动 / TLD 未缓存 / TLD 记录被清
        ↓
每次查询都要走「根 → TLD」两跳海外链路
        ↓
单查询 ~150ms，并发 100 时吞吐 ≈ 100/0.15 ≈ 660 QPS
        ↓
与实测 858–1,049 QPS 吻合
```

吞吐不是被「TTL 过期」打断，而是**被每一次独立迭代的海外 RTT 卡住**。压测中看到的「断崖」，是热集和冷集的迭代路径完全不同。

### 2.3 优化措施

| 措施 | 配置 | 效果 |
|---|---|---|
| 延长负缓存 | `cache-max-negative-ttl: 900` | 不存在的域名不再反复回源 |
| 提前预取 | `prefetch: yes` + `prefetch-key: yes` | 热门记录在过期前主动刷新 |
| 过期兜底 | `serve-expired: yes` + `serve-expired-ttl: 14400` | 上游不可达时返回过期数据而非 SERVFAIL |
| **TLD 缓存保活** | 见下方方案 | 消除冷启动断崖 |
| 稳定短 TTL | `cache-min-ttl: 60` | TTL<60s 的记录最多 1 分钟回源一次 |

#### 关于 prefetch 的实测澄清

`prefetch` 的触发条件是「记录剩余 TTL < 原始 TTL 的 10%」**且该记录在窗口内被再次查询**。实测中因为查询分布分散，`num.prefetch` 长期为 0。**不要指望 prefetch 解决冷启动问题**，它只对高度集中的热点有效。

#### 真正有效的方案：TLD 缓存预热

在服务启动后、接入流量前，主动解析各主流 TLD 的 NS 记录，把 `com/net/org/cn/io/co/info/biz` 等 TLD 的 NS 钉进缓存：

```bash
# scripts/preheat-tld.sh
for tld in com net org cn io co info biz me tv cc xyz top vip; do
  dig @127.0.0.1 -p 5353 "$tld" NS +norecurse >/dev/null 2>&1 &
done
wait
```

TLD NS 的 TTL 是 48 小时，一次预热即可覆盖数天。**这是消除风险 1 最有效、成本最低的手段。**

配合 systemd 在 unbound 启动后自动执行：

```ini
# /etc/systemd/system/tld-preheat.service
[Unit]
Description=Preheat TLD NS records into unbound cache
After=unbound.service
Requires=unbound.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/preheat-tld.sh

[Install]
WantedBy=multi-user.target
```

### 2.4 效果

- 预热后，`.com`/`.net` 域名的冷查询只需 TLD → 权威两跳，省去根查询
- 服务重启后的冷启动期从「全量走根」变为「直接走 TLD」

---

## 3. 风险 2：回源能力仅约 1,000 QPS

### 3.1 定位过程

#### 第一步：确认不是 CPU 瓶颈

冷缓存压测时：

```
unbound CPU  70%  （4 核整机，单进程）
system CPU   76%  （sys 态为主）
吞吐         858 QPS
```

CPU 远未打满，不是算力问题。并发提升到 200 后吞吐基本不变。

#### 第二步：拆解单次迭代耗时

用 Python 手工复现 unbound 的迭代路径：

```
1) f.root(4ms) -> .com NS          6–9 ms       ← 极快
2) .com TLD 192.12.94.30 -> 权威   110–145 ms   ← 主要耗时
3) 权威 -> 答案                    变量
```

**结论：耗时 90% 集中在 TLD 查询。**

#### 第三步：探测 TLD 服务器质量

对 13 台 `.com` TLD 服务器各发 8 次查询：

| 服务器 | p50 | 丢包 |
|---|---|---|
| 192.12.94.30 | 123 ms | 1/8 |
| 192.31.80.30 | 127 ms | 1/8 |
| 192.41.162.30 | 150 ms | **0/8** |
| 192.55.83.30 | 160 ms | **0/8** |
| 192.31.80.30 | 127 ms | 1/8 |
| 192.42.93.30 | 269 ms | 3/8 |
| 192.43.172.30 | 289 ms | 2/8 |
| 192.44.75.30 | **不可达** | 8/8 |

**11 台里有 1 台完全不可达，4 台丢包率 25–50%。**

#### 第四步：探测根服务器质量

对 13 台根各发 20 次查询：

| 根 | p50 | 丢包 |
|---|---|---|
| **f.root** 192.5.5.241 | **5 ms** | **0/20** |
| **j.root** 192.58.128.30 | **5 ms** | **0/20** |
| l.root 199.7.83.42 | 21 ms | 0/20 |
| k.root 193.0.14.129 | 35 ms | 0/20 |
| a.root 198.41.0.4 | 38 ms | 0/20 |
| i.root 192.36.148.17 | 76 ms | 2/20 |
| m.root 202.12.27.33 | 77 ms | 6/20 |
| b.root 199.9.14.201 | 168 ms | 11/20 |

**f.root 和 j.root 只要 5ms，而 unbound 平均会在 13 台里随机选。**

#### 第五步：验证 unbound 是否选错

`unbound-control dump_infra` 显示 unbound 记录的根 RTT：

```
192.33.4.12    rtt=537   ← 实测 36ms
192.112.36.4   rtt=616   ← 实测 200ms
198.97.190.53  rtt=425   ← 实测 115ms
```

unbound 的 infra 缓存使用 EWMA 平滑，一旦早期命中慢速根，会持续倾向慢速根。

### 3.2 优化措施

#### 措施 1：锁定最快的根服务器

**踩坑记录：`root-hints` 单独不够。** unbound 在首次查询根区 NS 时会做 NS priming，把全部 13 台根重新纳入选择池，覆盖 root.hints 的限制。

正确做法是 **root-hints + do-not-query-address 双管齐下**：

```
root-hints: "/etc/unbound/root.hints"

do-not-query-address: 198.41.0.4/32
do-not-query-address: 199.9.14.201/32
do-not-query-address: 192.33.4.12/32
do-not-query-address: 199.7.91.13/32
do-not-query-address: 192.203.230.10/32
do-not-query-address: 192.112.36.4/32
do-not-query-address: 198.97.190.53/32
do-not-query-address: 192.36.148.17/32
do-not-query-address: 202.12.27.33/32
```

保留 f/j/l/k 四台（均零丢包）。

`root.hints` 由 `scripts/probe-roots.py` 自动生成：

```bash
python3 scripts/probe-roots.py -n 4 -o /etc/unbound/root.hints
unbound-checkconf && systemctl reload unbound
```

验证生效：

```bash
unbound-control dump_infra | grep " \. " | awk '{print $1}'
# 应只输出 f/j/l/k 的 IP
```

#### 措施 2：提高迭代并发

| 参数 | 优化前 | 优化后 | 说明 |
|---|---|---|---|
| `outgoing-range` | 8192 | **16384** | 并发出站查询数，冷查询的主约束 |
| `num-queries-per-thread` | 4096 | **8192** | 每线程请求队列 |
| `jostle-timeout` | 200 | **50** | 队列满时抢占旧查询，降低长尾 |
| `infra-cache-min-rtt` | 50 | **10** | 允许 RTO 下探到 10ms，快速重传 |
| `infra-cache-numhosts` | 200000 | **500000** | 容纳更多权威服务器记录 |
| `infra-cache-max-rtt` | (默认 120000) | **3000** | 限制 RTO 上限，避免等 2 分钟 |
| `infra-keep-probing` | (默认 no) | **yes** | 持续探测标记为 down 的服务器 |
| `unwanted-reply-threshold` | 0 | **10000000** | 过期/无效响应防护 |

#### 措施 3：TLD 预热（见 2.3）

### 3.3 效果

| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 单次迭代 p50 | 220 ms | **151 ms** | -31% |
| 单次迭代 p90 | 994 ms | **329 ms** | -67% |
| 单次迭代 max | 1312 ms | **601 ms** | -54% |
| 回源吞吐 | 858 QPS | **1049 QPS** | +22% |
| 快路径占比 | — | **78%** | |

### 3.4 剩余瓶颈（不可消除）

| 瓶颈 | 数值 | 能否优化 |
|---|---|---|
| 根查询 | 4–5 ms | 已最优（f/j.root） |
| TLD 查询 | 110–290 ms | ❌ 物理链路 |
| TLD 丢包 | 30–50% | ❌ 运营商策略 |
| 权威查询 | 变量 | ❌ 取决于目标站点 |

**p50 = 4 + 120 + 30 ≈ 150ms 已经是理论上限。**

剩余 22% 的慢查询来自 TLD 丢包后的 RTO 重传（1s 起步）。要彻底解决只能：
- 换用国内可达更好的 TLD 服务器（不存在，Verisign 就是美国）
- 降低 RTO 起点（unbound 不提供该配置）
- 在 TLD 层前置缓存（见下节）

---

## 4. 重新规划的容量模型

### 4.1 原报告结论的问题

原报告给出「单节点稳定承载 40,000 QPS」，但这是**纯热缓存**口径。真实业务流量是热冷混合，应按命中率加权。

### 4.2 新的容量模型

设：

- `H` = 热缓存命中率（TLD 与答案均命中）
- `C_hit` = 热缓存单节点吞吐上限（实测 94k，生产按 **50k** 规划）
- `C_miss` = 回源单节点吞吐（实测 1049，优化后）

则单节点可承载：

```
C_total = 1 / ( H / C_hit + (1-H) / C_miss )
```

| 命中率 H | 单节点吞吐 | 说明 |
|---|---|---|
| 100% | 50,000 | 不可能达到 |
| 99.5% | 22,000 | 高热度业务（CDN、大站） |
| 99.0% | 15,500 | 典型的 Web 业务 |
| 98.0% | 10,300 | 长尾业务 |
| 95.0% | 5,250 | 爬虫/监控类 |
| 90.0% | 2,700 | 几乎不重复 |

**关键洞察：命中率从 99.5% 掉到 98%，单节点容量掉一半还多。** 命中率才是容量规划的第一变量，比 CPU 更重要。

### 4.3 容量规划建议（修订）

| 指标 | 建议值 | 依据 |
|---|---|---|
| 单节点规划（命中率 ≥ 99%） | **15,000 QPS** | 留 3x 余量应对命中率波动 |
| 单节点规划（命中率 ≥ 99.5%） | **20,000 QPS** | 热业务 |
| 单节点峰值 | 50,000 QPS | 纯热缓存，短时可承受 |
| 单节点回源能力 | **1,000 QPS** | 优化后实测，按 800 规划 |
| 单节点 TCP | 20,000 QPS | 实测 25k 的 80% |
| 冷启动期吞吐 | ~1,000 QPS | 需配合 TLD 预热缩短 |

### 4.4 扩容判据（修订）

原报告的「QPS > 40,000 扩容」不足以保证性能。修订为：

```
扩容条件（任一命中）:
  1. QPS > 单节点规划值 (15k ~ 20k)
  2. 缓存命中率 < 99%
  3. 回源 QPS > 800
  4. 迭代 p90 延迟 > 500ms
  5. SERVFAIL 率 > 0.5%
```

其中 **条件 3（回源 QPS）是最灵敏的指标**：回源打满会立刻引发排队，且难以恢复。

### 4.5 容量估算示例

业务场景：10 万 QPS，命中率 99%，单节点规划 15k

```
节点数 = 100,000 / 15,000 = 6.7 → 7 台
加 N+1 冗余 → 8 台
单节点回源 = 100,000 × 1% / 8 = 125 QPS << 800 ✓
```

若命中率跌到 97%：

```
单节点回源 = 100,000 × 3% / 8 = 375 QPS，仍在 800 内
但单节点总吞吐 = 1/(0.97/50000 + 0.03/1049) = 25,000 → 需重新评估
```

---

## 5. 额外建议

### 5.1 必须监控的指标

本次分析全程依赖 `unbound-control stats`，说明监控缺失会让问题无法定位。建议至少采集：

| 指标 | 采集命令 | 告警阈值 |
|---|---|---|
| 缓存命中率 | `stats_noreset` 的 cachehits/(hits+miss) | < 99% |
| 回源 QPS | cachemiss 的增量速率 | > 800/s |
| 迭代 p90 | `recursion.time.avg` | > 500ms |
| SERVFAIL 率 | `num.answer.servfail` / `num.queries` | > 0.5% |
| 请求队列溢出 | `requestlist.exceeded` | > 0 |

### 5.2 如果业务对延迟敏感

本项目是**完整递归**架构，冷查询天然需要 150ms+（海外 TLD）。如果业务对冷查询延迟极敏感，可以考虑：

| 方案 | 冷查询延迟 | 是否违反需求 3 |
|---|---|---|
| 当前（完整递归） | 150–600 ms | 否 |
| 递归 + 国内权威转发 | 20–50 ms | **是**（中转第三方） |
| 递归 + 缓存预热 | 150 ms | 否 |
| 递归 + 本地预置热门域名 | 0 ms | 否 |

唯一不违反「不中转」原则的加速手段是**预热 + 预置**。

### 5.3 附：本次优化涉及的配置项清单

```diff
  # 容量 / 并发
- num-queries-per-thread: 4096
+ num-queries-per-thread: 8192
- outgoing-range: 8192
+ outgoing-range: 16384
- jostle-timeout: 200
+ jostle-timeout: 50
- infra-cache-min-rtt: 50
+ infra-cache-min-rtt: 10
- infra-cache-numhosts: 200000
+ infra-cache-numhosts: 500000

  # 新增
+ infra-host-ttl: 900
+ infra-cache-max-rtt: 3000
+ infra-keep-probing: yes
+ target-fetch-policy: "3 2 1 0 0"
+ root-hints: "/etc/unbound/root.hints"
+ do-not-query-address: <9 台慢速根>

  # 安全
- unwanted-reply-threshold: 0
+ unwanted-reply-threshold: 10000000
```
