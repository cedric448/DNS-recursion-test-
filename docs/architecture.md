# 架构设计

## 1. 背景与目标

在自有服务器上构建 DNS 递归解析服务，供云上其他机器将本机作为 DNS 服务器使用。

需求约束（来自原始需求 `p.md`）：

| # | 需求 | 解读 |
|---|---|---|
| 1 | 支持分布式，本机不够时快速横向扩展 | 节点无状态、可堆叠，扩展不改架构 |
| 2 | 解析一致性，相同域名解析到相同 IP | 缓存内 TTL 有效期内结果稳定，节点间结果收敛 |
| 3 | 不中转到云厂商 DNS，自行递归到根 | 必须实现完整迭代式递归 |
| 4 | 2 万 QPS 起步，目标 10 万 QPS | 单机满足 2 万，多机支撑 10 万 |

## 2. 方案选型

### 2.1 候选方案

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 云厂商 DNS / 公网 DoH 转发 | 零运维 | 不满足需求 3，且受制于人 | ❌ |
| BIND 9 (recursive) | 成熟、生态好 | 单进程多线程，性能弱于 unbound；配置冗长 | 备选 |
| PowerDNS Recursor | 自带 Lua 脚本能力，性能好 | 内存占用更高，社区版有 QPS 软限 | 备选 |
| **Unbound** | 性能极强、内存占用低、DNSSEC 严谨、安全审计充分 | 无内置负载均衡层 | ✅ 递归层 |
| **dnsdist** | 高性能接入层、健康检查、多后端分发、ACL、DoT/DoH 扩展 | 单进程单核收包（1.9.x） | ✅ 接入层 |
| 自研（Go/Java） | 完全可控 | 递归器复杂度极高（DNSSEC、EDNS、缓存、并发、反射攻击防护），风险不可接受 | ❌ |

### 2.2 最终架构

采用 **dnsdist + Unbound** 双层结构：

```
                   云上业务机器（VPC 内）
                            │
                    UDP/TCP 53
                            │
        ┌───────────────────▼───────────────────┐
        │            dnsdist (接入层)            │
        │  · ACL / 反射攻击防护                  │
        │  · 后端健康检查与故障摘除              │
        │  · 多后端分发（为扩展预留）            │
        │  · 命中率统计、限速、黑白名单          │
        └───────────────────┬───────────────────┘
                            │ 127.0.0.1:5353
        ┌───────────────────▼───────────────────┐
        │            Unbound (递归层)            │
        │  · 完整迭代递归（根 → TLD → 权威）      │
        │  · DNSSEC 校验（validator 模块）       │
        │  · msg/rrset/neg/infra/key 多级缓存    │
        │  · qname 最小化、0x20 编码防护         │
        └───────────────────┬───────────────────┘
                            │ UDP/TCP 53 出站
        ┌───────────────────▼───────────────────┐
        │        根服务器 (a~m.root-servers.net) │
        │        → TLD → 权威 → 最终答案         │
        └───────────────────────────────────────┘
```

**为什么分两层而不是单 Unbound：**

- Unbound 单进程即可 125k QPS（实测），但对外暴露时**缺少 ACL、限速、后端健康检查**等边缘能力
- dnsdist 提供「淘汰未响应后端」的能力，是多节点架构的天然入口
- 两层解耦后，将来换成 PowerDNS Recursor 或加自定义策略（例如按域名分流到不同递归器）无需改动下游

**代价**：dnsdist 每进程约 1.5 万 QPS（单核），是整个系统第一个瓶颈，扩展策略见第 5 节。

## 3. 关键设计决策

### 3.1 递归而非转发（需求 3）

`Unbound` 以 `iterator` 模式工作：从 `root.hints` 内置根提示出发，逐级向根、TLD、权威服务器发起迭代查询，全程不经任何第三方递归器。

配置上**不得**出现 `forward-zone`，否则退化为转发模式。

实测根服务器连通性：13 台根中 12 台 UDP/53 可达（`199.9.14.201` b.root 超时，属于该站点网络策略，不影响递归可用性）。

### 3.2 一致性（需求 2）

**「跨节点缓存完全一致」在递归架构中不可实现。** 递归结果是随时间变化的（权威轮询、TTL 到期、权威故障转移），各节点独立缓存必然存在窗口差异。强行一致需要集中式共享缓存，会引入单点与额外延迟。

因此采用 **逻辑一致性**，由三项配置保证：

| 配置 | 作用 |
|---|---|
| `cache-min-ttl: 60` | 将 TTL < 60s 的记录统一提升到 60s，减少短 TTL 记录导致的节点间抖动 |
| `cache-max-ttl: 86400` | 限制超长 TTL，避免记录变更后长期不生效 |
| `rrset-roundrobin: no` | **关闭**多 A 记录轮询，同一域名多次解析结果顺序完全一致 |

配合 `serve-expired`：权威不可达时返回过期缓存而非 SERVFAIL，避免某节点因上游抖动返回不同结果。

> 若业务确实要求跨节点严格一致（例如基于 DNS 做流量调度），应改用 **权威 DNS**（自建 dnsdist 作为权威 + 后端负载均衡），而不是递归器。两者的语义不同。

### 3.3 安全

| 威胁 | 防护 |
|---|---|
| DNS 放大攻击（ANY 查询） | `deny-any: yes` |
| 缓存投毒 | `harden-glue`、`harden-referral-path`、`harden-dnssec-stripped`、`val-permissive-mode: no` |
| 伪造响应注入 | `unwanted-reply-threshold`、随机源端口（`outgoing-port-permit`） |
| 上游欺骗 | `do-not-query-localhost: yes` |
| 未授权访问 | dnsdist `setACL()`（上线前必须收紧） |
| 反射放大 | dnsdist 限速规则 + `setMaxUDPOutstanding` |

### 3.4 容量规划

**实测单机（4C / 7.5G）：**

| 指标 | 数值 |
|---|---|
| 单机热缓存吞吐上限 | ~48k QPS（dnsdist 单进程） |
| Unbound 独立吞吐 | ~125k QPS |
| dnsdist 每核吞吐 | ~15k QPS |
| 冷域名全递归吞吐 | ~1.1k QPS（受上游 RTT 限制） |
| 2 万 QPS 下平均延迟 | 1.17 ms |

**生产容量公式：**

```
节点数 = 峰值 QPS / (单节点安全阈值)
单节点安全阈值 = 单核吞吐 × 核数 × 0.6（安全系数）
```

- 4C 节点：15k × 4 × 0.6 ≈ **36k 理论**，生产建议按 **1.5 万** 规划（预留突发与冷域名比例）
- 10 万 QPS → **8 台 4C 节点**（N+1 冗余）

### 3.5 内存规划

Unbound 缓存按需分配，配置值为上限：

| 缓存 | 配置 | 建议 |
|---|---|---|
| msg-cache | 256m | 按 QPS × 平均 TTL 估算，每 100 万条约 1.5-2G |
| rrset-cache | 512m | 约为 msg-cache 的 2 倍 |
| neg-cache | 64m | NXDOMAIN 记录 |
| key-cache | 64m | DNSSEC 密钥 |

7.5G 机器当前占用约 1.2G，余量充足。

## 4. 请求生命周期

```
1. 客户端 → dnsdist:53 (UDP)
2. dnsdist: ACL 检查 → 规则匹配 → 后端健康状态检查
3. dnsdist → unbound:127.0.0.1:5353
4. unbound: 查 msg-cache → 命中则直接返回（~0.04ms）
            → 未命中则走 iterator: 根 → .com → authority（~100ms）
5. unbound → dnsdist → 客户端
```

## 5. 横向扩展设计

### 5.1 扩展阶段（推荐路径）

| 阶段 | 规模 | 方案 |
|---|---|---|
| **L0** | ≤ 3 万 QPS | 单机 dnsdist + unbound（当前） |
| **L1** | 3~10 万 QPS | 多机 + VIP/CLB，每机 4C，6~8 台 |
| **L2** | 10~100 万 QPS | 多机 + **Anycast**，节点就近接入 |
| **L3** | 100 万+ | 分层：边缘 L2 缓存 + 核心递归池 + 共享根/NX 缓存 |

### 5.2 L1：多机 + VIP

```
                  云上业务机器
                        │
                  [ 云 CLB / VIP ]
                  UDP 53 健康检查
                        │
     ┌──────────┬───────┴───────┬──────────┐
   DNS-1      DNS-2           DNS-3      DNS-N
  dnsdist    dnsdist         dnsdist    dnsdist
     │          │               │          │
  unbound    unbound         unbound    unbound
     └──────────┴─ 各自向根迭代 ──────────┘
```

**要点：**

- CLB 必须支持 **UDP 健康检查**（部分云厂商 UDP 探活较弱，需用 dnsdist 内置的 `controlSocket` +`probe` 或自建拨测）
- 启用 `setServerPolicy(roundrobin)` 或 `wrandom`，不要用 `firstAvailable`（会导致全部压在一台）
- 节点间**不共享缓存**，靠 3.2 的一致性配置收敛

**单机内扩核（可选）：**

dnsdist 1.9.x 的 UDP 收包是单线程的，`reusePort=true` 实测无提升。单机想用满多核需：

```bash
# 每核一个 dnsdist 进程，绑定不同 IP
ip addr add 10.0.0.11/24 dev eth0
dnsdist -C /etc/dnsdist/d1.conf   # setLocal("10.0.0.11:53")
dnsdist -C /etc/dnsdist/d2.conf   # setLocal("10.0.0.12:53")
```

实测 4 进程 61.8k QPS（每进程 ~15.4k），接近理论线性。

> dnsdist 2.0 引入了真正的多线程 UDP 收包（`setNumUDPListeners` + `addLocal` 每线程一 socket），届时无需多进程。

### 5.3 L2：Anycast

- 每个 PoP 部署一组节点，宣告相同 VIP
- 客户端 BGP 就近接入，天然分流、天然容灾
- 需要：自有 ASN + IP 段、BGP 运维能力、节点一致性监控
- **注意**：Anycast 下每个 PoP 独立缓存，**一致性更弱**，只能靠 `cache-min-ttl` 收敛

### 5.4 根服务器配额（关键约束）

每台节点独立向根服务器发起查询。ICANN 对根服务器的查询速率有非正式限制（约 20-50 QPM/源 IP/GTLD，超限会限速或返回降级数据）。

**节点数 × 新域名比例 是约束项：**

```
若日均 10 亿请求、缓存命中率 95% → 每天 5000 万次迭代
5000 万 / 86400 ≈ 580 次/秒 全集群向根查询
分到 20 台节点 = 29 次/秒/台 → 安全
分到 200 台节点 = 2.9 次/秒/台 → 安全
```

只有节点数激增且缓存命中率低时才需要优化，方案见 5.5。

### 5.5 根/NXDOMAIN 共享缓存（L3 阶段可选）

若节点数 > 50 且向根查询频繁，可在节点间共享「根区数据 + 高频 NXDOMAIN」：

- 用 `unbound` 的 `auth-zone` 拉取根区 AXFR，定期分发到各节点
- 或部署一台 **TLD 级预取节点**，各节点对其发起 `forward-zone`（仅限根/TLD 层，不违反需求 3）

> 简单做法：在每台节点上配置 `unbound` 的 `local-zone` 预加载高频域名。

## 6. 未来扩展性设计

### 6.1 接入协议扩展

| 能力 | 现状 | 扩展方式 |
|---|---|---|
| DoT (DNS over TLS) | 未启用 | dnsdist `addTLSLocal()`，需证书 |
| DoH (DNS over HTTPS) | 未启用 | dnsdist `addDOHLocal()`，**需升级到 1.9.9+ 修复 CVE-2025-30194** |
| DNS over QUIC | 未启用 | dnsdist 1.9+ `addDOQLocal()` |
| IPv6 | 已关闭 | 网络支持后 `do-ip6: yes` + `interface: ::1@5353` |

### 6.2 策略扩展

- **按域名分流**：dnsdist `addAction(``SuffixMatchNodeRule``, ``PoolAction``)` → 不同池（例如国内域名走本地递归、海外域名走专门出海节点）
- **限速**：dnsdist `dynBlockRulesGroup()` 按源 IP 限速，防单客户端打爆
- **QPS 统计**：dnsdist `setStatistics` + Prometheus 导出
- **响应改写**：dnsdist `SpoofAction` / `LuaResponseAction`

### 6.3 可观测性

当前**未部署任何监控**。建议：

| 指标 | 来源 | 告警阈值 |
|---|---|---|
| QPS | dnsdist `showStats()` | 持续 > 80% 容量 |
| 缓存命中率 | unbound `num.cache.hits / num.cache.miss` | < 70% |
| 后端健康 | dnsdist `showServers()` 的 `down` 计数 | 任一后端 down |
| 递归延迟 | unbound `histogram` | p99 > 500ms |
| SERVFAIL 率 | unbound `num.answer.servfail` | > 1% |
| 内存 | 进程 RSS | > 80% 配置上限 |

实现路径：dnsdist `webserver()` + `/metrics` → Prometheus scrape → Grafana。

### 6.4 高可用

| 层级 | 当前 | 建议 |
|---|---|---|
| 进程崩溃 | systemd `Restart=on-failure` | ✅ 已有 |
| 后端故障 | dnsdist 健康检查自动摘除 | ✅ 已有 |
| 单机故障 | 无 | 多机 + VIP（L1） |
| 上游全挂 | `serve-expired` 返回过期缓存 | ✅ 已有 |
| 配置回滚 | 无 | 配置纳入 git（已完成），配合 Ansible 下发 |

### 6.5 配置管理

当前配置手工下发。扩展时建议：

- 配置全部纳入本仓库（`etc/` 目录）
- Ansible playbook 负责：安装 → 下发配置 → 校验 → 重启 → 冒烟测试
- 灰度：先升级 1 台，观察 30 分钟指标，再全量

## 7. 已知限制

1. **一致性为逻辑一致**，非严格一致（见 3.2）
2. **dnsdist 单核瓶颈**：1.9.x 需多进程扩展（见 5.2）
3. **无监控**（见 6.3）
4. **ACL 全开**（`0.0.0.0/0`），上线前必须收紧
5. **控制台密钥为测试值**，需 `makeKey()` 重新生成
6. **未启用 DoH/DoT**，如需加密解析需额外配置证书
7. **dnsdist 1.9.6 存在 CVE-2025-30194**（仅影响 DoH+nghttp2 场景，当前未启用 DoH，无实际风险）
