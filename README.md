# DNS 递归服务

本机自建 DNS 递归服务，云上机器指向本机解析域名。完整递归到根服务器，不依赖任何运营商/云厂商上游 DNS。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 架构设计：选型依据、一致性、容量模型、横向扩展 |
| [docs/deployment.md](docs/deployment.md) | 部署手册：从零安装、调优、冒烟测试、压测、运维、检查清单 |
| [docs/tuning-report.md](docs/tuning-report.md) | **性能调优报告**：风险定位、根因分析、优化效果、黑盒交叉验证 |
| [docs/optimization.md](docs/optimization.md) | 扩展性与优化建议：监控、性能、高可用、演进路线、风险登记 |
| [DNS压测报告_172.21.80.142.md](DNS压测报告_172.21.80.142.md) | 基线压测数据（优化前，内网 CVM 黑盒） |
| [DNS压测对比报告_优化前后.md](DNS压测对比报告_优化前后.md) | 优化前后对比复测（内网 CVM 黑盒） |

## 快速开始

```bash
yum install -y unbound dnsdist bind-utils dnsperf
git clone <repo> && cd <repo>

# 1. 按本机网络生成最优 root.hints（重要，不能跳过）
python3 scripts/probe-roots.py -n 4 -o /etc/unbound/root.hints

# 2. 下发配置
cp etc/unbound/conf.d/recursor.conf /etc/unbound/conf.d/
cp etc/unbound/unbound.conf          /etc/unbound/unbound.conf
cp etc/unbound/root.hints            /etc/unbound/root.hints
cp etc/dnsdist/dnsdist.conf          /etc/dnsdist/dnsdist.conf
cp etc/sysctl.d/99-dns.conf          /etc/sysctl.d/
cp etc/security/limits.d/99-dns.conf /etc/security/limits.d/
mkdir -p /etc/systemd/system/dnsdist.service.d
cp etc/systemd/dnsdist-override.conf /etc/systemd/system/dnsdist.service.d/override.conf

chown root:dnsdist /etc/dnsdist/dnsdist.conf && chmod 640 /etc/dnsdist/dnsdist.conf
sysctl -p /etc/sysctl.d/99-dns.conf
systemctl daemon-reload
systemctl enable --now unbound dnsdist

# 3. TLD 预热（重要，否则冷启动吞吐只有 1k）
install -m 755 scripts/preheat-tld.sh /usr/local/bin/
/usr/local/bin/preheat-tld.sh

dig @127.0.0.1 www.example.com A
```

详细步骤及排错见 [docs/deployment.md](docs/deployment.md)。

## 架构

```
云上机器 --UDP/TCP 53--> dnsdist --> unbound(127.0.0.1:5353) --> 根服务器迭代
```

| 组件 | 版本 | 监听 | 职责 |
|---|---|---|---|
| dnsdist | 1.9.6 | `0.0.0.0:53` UDP+TCP | 接入层、ACL、健康检查、多后端分发 |
| unbound | 1.17.1 | `127.0.0.1:5353` | 递归解析、DNSSEC 校验、缓存 |

## 核心特性

- **完整递归**：从根服务器迭代，不经任何第三方递归器
- **DNSSEC 校验**：validator 模式，`val-permissive-mode: no`
- **一致性**：`cache-min-ttl: 60` + `rrset-roundrobin: no`，节点间结果收敛
- **容错**：`serve-expired` 权威不可达时返回过期缓存
- **根选择优化**：`root.hints` + `do-not-query-address` 锁定快速根，根查询 38ms → 4ms
- **冷启动优化**：TLD + CDN 二级后缀预热，避免重启后吞吐掉到千级、p90 涨到 900ms

## 实测数据（4C / 7.5G）

| 场景 | 数值 | 说明 |
|---|---|---|
| Unbound 热缓存 | 93k–99k QPS | 直连 5353 |
| 经 dnsdist 热缓存 | ~48k QPS | 瓶颈在 dnsdist 单核 |
| 4 进程 dnsdist | 61.8k QPS | 多进程线性扩展 |
| **回源（全新域名）** | **1,049 QPS** | 优化前 858，+22% |
| 真实冷域名 p50 | 151 ms | 优化前 ~220ms |
| 真实冷域名 p90 | 329 ms | 优化前 ~950ms |

> 黑盒复测受压测机 virtio 链路限制（封顶 54k），只能佐证方向、无法量化幅度。详见 [docs/tuning-report.md](docs/tuning-report.md) 第 6 节。

**容量规划按命中率计算，不要按 CPU：**

```
C_total = 1 / ( H/50000 + (1-H)/800 )
99%   命中 -> 15,500 QPS/节点
99.5% 命中 -> 22,000 QPS/节点
95%   命中 ->  5,250 QPS/节点
```

## 仓库结构

```
.
├── docs/
│   ├── architecture.md            架构设计
│   ├── deployment.md              部署手册
│   ├── tuning-report.md           性能调优报告（含黑盒交叉验证）
│   └── optimization.md            扩展性与优化建议
├── DNS压测报告_172.21.80.142.md     基线压测数据
├── DNS压测对比报告_优化前后.md      优化前后对比数据
├── etc/
│   ├── dnsdist/dnsdist.conf       接入层配置
│   ├── unbound/
│   │   ├── unbound.conf           主配置（替换发行版）
│   │   ├── root.hints             自定义根服务器列表
│   │   └── conf.d/recursor.conf   递归器配置
│   ├── sysctl.d/99-dns.conf       内核调优
│   ├── security/limits.d/99-dns.conf
│   └── systemd/dnsdist-override.conf
├── scripts/
│   ├── probe-roots.py             探测根服务器并生成 root.hints
│   ├── preheat-tld.sh             TLD 与 CDN 后缀缓存预热
│   └── gen_cold.py                生成冷缓存压测数据集
└── bench/gen_queries.py           压测数据集生成
```

## 压测

```bash
cd bench && python3 gen_queries.py

dnsperf -s 127.0.0.1 -d cache_hit.txt -c 200 -T 8 -l 15            # 热缓存上限
dnsperf -s 127.0.0.1 -d cache_hit.txt -c 100 -T 8 -Q 20000 -l 20  # 2 万 QPS 限速

# 冷缓存必须用全新域名，否则测的是热缓存
python3 ../scripts/gen_cold.py   # 生成 /tmp/cold.txt
dnsperf -s 127.0.0.1 -p 5353 -d /tmp/cold.txt -c 100 -T 8 -l 15
```

## 注意事项

> **上线前必须处理：**
> 1. `dnsdist.conf` 中 `setACL({"0.0.0.0/0"})` 仅为测试配置，须收紧到实际 VPC 网段
> 2. `setKey()` 控制台密钥为占位值，须用 `makeKey()` 重新生成
> 3. 每台节点都要执行 `probe-roots.py`，root.hints 与 `do-not-query-address` 按本机网络生成
> 4. 必须启用 TLD 预热，并按业务流量补充 `SUFFIXES` 中的二级 CDN 后缀
>
> 详见 [docs/deployment.md](docs/deployment.md) 第 4.3 节。

## 踩坑记录

1. **Unbound 起不来（fatal: trust anchor presented twice）** — TencentOS 默认 `unbound.conf` 同时启用 `auto-trust-anchor-file` 与 `auth-zone "."`，根信任锚重复注册。需替换主配置。
2. **dnsdist 起不来（Unable to read configuration file）** — 发行版配置为 `root:root 640`，服务以 `dnsdist` 用户运行。改为 `root:dnsdist 640`。
3. **dnsdist 单核瓶颈** — 1.9.x `reusePort=true` 不提升吞吐，需多进程 + 多 IP。
4. **UDP 53 出站未被封锁** — 13 台根服务器中 12 台可达（b.root 超时不影响递归）。
5. **CVE-2025-30194** — 影响 1.9.0–1.9.8 的 DoH(nghttp2) 场景；未启用 DoH 不受影响。TencentOS EPOL 源无 1.9.9+ 包。
6. **`root-hints` 单独配置无效** — unbound 首次查询根区 NS 会做 NS priming，把全部 13 台根重新纳入选择池。必须配合 `do-not-query-address` 排除慢速根。
7. **冷缓存压测用同一数据集会失真** — 第二轮已全部命中缓存，实测 86k QPS，完全无法反映回源能力。
8. **`prefetch` 计数长期为 0 是正常的** — 触发条件是剩余 TTL < 原 TTL 的 10% 且窗口内被再次查询，只对高度集中的热点有效。
9. **服务重启后 TLD 缓存为空导致吞吐掉到千级** — 必须做 TLD 预热，并配置定时任务。
10. **预热只做一级 TLD 不够** — `fastly.net`、`akamaized.net` 等二级 CDN 后缀的 NS 要单独迭代，未预热时 p90 达 896ms。需在 `SUFFIXES` 中补充。
11. **黑盒压测有 ~54k 的路径上限** — 2 vCPU + virtio 的压测机无法分辨 54k 以上的差异，评估容量必须看服务端 `unbound-control stats_noreset`。
