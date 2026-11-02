# 扩展性与优化建议

本文档汇总后续演进方向，按优先级排序。第 1 节为**上线前必做**，其余按需推进。

## 1. 上线前必做（P0）

### 1.1 收紧 ACL

当前 `setACL({"0.0.0.0/0","::/0"})` 允许任意来源查询，等于开放递归器 —— 会被利用做 DNS 放大攻击。

```lua
setACL({"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"})
```

同时建议在安全组限制 TCP/UDP 53 的入站源地址。

### 1.2 更换控制台密钥

```bash
dnsdist -e 'print(makeKey())'          # 生成
# 填入 setKey("...")
```

### 1.3 配置加密解析（如需）

如果客户端在不可信网络（公网、办公网），建议至少提供 DoT：

```lua
addTLSLocal("0.0.0.0:853", "/etc/dnsdist/certs/dns.crt", "/etc/dnsdist/certs/dns.key",
            {provider="openssl"})
```

> **DoH 需要先升级 dnsdist 到 1.9.9+**（CVE-2025-30194 仅影响 DoH + nghttp2）。

## 2. 监控告警（P1，强烈建议）

当前系统**完全无监控**，故障只能靠用户反馈发现。

### 2.1 dnsdist 指标

启用内置 web 服务：

```lua
webserver("127.0.0.1:8083", "<password>")
```

`curl http://127.0.0.1:8083/metrics` 输出 Prometheus 格式，关键指标：

| 指标 | 含义 | 告警 |
|---|---|---|
| `dnsdist_queries` | 累计查询数 | 速率突降/突增 |
| `dnsdist_frontend_servfail` | SERVFAIL 响应数 | 占比 > 1% |
| `dnsdist_backend_down` | 后端 down 数 | > 0 |
| `dnsdist_responses_latency_avg100` | 平均延迟 | p99 > 500ms |
| `dnsdist_udp_drops` | UDP 丢包 | > 0 持续 |

### 2.2 Unbound 指标

启用 remote-control：

```
remote-control:
    control-enable: yes
    control-interface: 127.0.0.1
    control-port: 8953
```

`unbound-control stats_noreset` 关键输出：

| 指标 | 含义 | 告警 |
|---|---|---|
| `total.num.cachehits` / `total.num.cachemiss` | 缓存命中/未命中 | 命中率 < 70% |
| `total.num.answer.servfail` | SERVFAIL 数 | > 1% |
| `total.recursion.time.avg` | 平均递归耗时 | > 200ms |
| `mem.cache.rrset` / `mem.cache.message` | 缓存内存 | > 配置上限 80% |
| `num.query.tcpout` | TCP 递归查询数 | 突增说明 UDP 被截断 |

可用 `unbound_exporter` 直接导出 Prometheus 指标。

### 2.3 推荐监控栈

```
dnsdist /metrics ─┐
                  ├─→ Prometheus ─→ Grafana
unbound_exporter ─┘        │
                           └─→ Alertmanager
```

### 2.4 外部拨测

Prometheus 无法发现「本机挂了」的场景，需要外部拨测：

- 每分钟从多个区域 `dig @<节点IP> www.example.com` 一次
- 同时校验返回的 A 记录值是否符合预期
- 失败 2 次触发告警

## 3. 性能优化（P1）

### 3.1 dnsdist 单核瓶颈

**现状**：dnsdist 1.9.x 的 UDP 收包固定在单线程，每进程约 1.5 万 QPS，4 核机器只能用到 1.5 核。

**方案对比：**

| 方案 | 吞吐（4C） | 复杂度 | 结论 |
|---|---|---|---|
| 单进程 + reusePort | 48k | 低 | 实测无提升 |
| **多进程 + 多 IP** | 61.8k | 中 | 立即可用 |
| 升级 dnsdist 2.0 | 预估 120k+ | 中（需重新验证） | 长期方案 |
| 去掉 dnsdist | 125k | 低 | 备选，但失去 ACL/健康检查 |

**多进程部署（现可用）：**

```bash
# 每核一个进程，绑定不同 IP
for n in 1 2 3 4; do
  sed "s|^setLocal(.*)|setLocal(\"10.0.0.1${n}:53\")|" /etc/dnsdist/dnsdist.conf > /etc/dnsdist/d${n}.conf
  sed -i "s|^controlSocket(.*)|controlSocket(\"127.0.0.1:590${n}\")|" /etc/dnsdist/d${n}.conf
done

cat > /etc/systemd/system/dnsdist@.service <<""EOF""
[Unit]
Description=dnsdist instance %i
After=network-online.target
[Service]
ExecStart=/usr/bin/dnsdist -C /etc/dnsdist/d%i.conf --supervised -u dnsdist -g dnsdist
LimitNOFILE=1048576
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now dnsdist@1 dnsdist@2 dnsdist@3 dnsdist@4
```

> 客户端侧由一个 VIP/CLB 把流量分发到 4 个 IP。

### 3.2 Unbound 参数优化

当 QPS 显著上升（> 2 万）时调整：

```
num-threads: 4                # 与 CPU 核数一致
num-queries-per-thread: 4096  # 每线程请求队列
outgoing-range: 8192          # 并发出站查询数
jostle-timeout: 200           # 高负载时允许抢占旧查询
delay-close: 0                # 减少 TIME_WAIT
so-reuseport: yes             # 必需，否则各线程无法并行收包
```

内存随缓存增长：

```
msg-cache-size: 256m -> 1g     # 每 100 万缓存对象约 1.5-2G
rrset-cache-size: 512m -> 2g
```

### 3.3 冷域名递归优化

冷域名（首次查询）走完整迭代，延迟 ~100ms、吞吐 ~1.1k QPS。若业务冷域名比例高：

- **提高 prefetch 命中**：`prefetch: yes` 已启用，对即将过期的热门记录主动刷新
- **增加 outgoing-range**：提高并发迭代数
- **qname 最小化**：已开启，减少无用查询
- **不建议关闭 DNSSEC**：安全代价过高

## 4. 高可用（P1）

### 4.1 多节点 + VIP

```
业务 -> CLB/VIP (UDP 53) -> 节点1..N
```

要点：

- CLB 健康检查同时配 **UDP 和 TCP**，部分云厂商 UDP 探活不可靠
- 至少 N+1 冗余，10 万 QPS 用 8 台而不是 7 台
- 节点故障时剩余节点需承接流量，单节点冗余度 = (N-1)/N

### 4.2 单机多活

同一台机器跑 2 个 dnsdist 进程，绑定不同 IP + 不同 unbound 实例：

- 进程级故障隔离（一个崩了另一个继续服务）
- CPU 利用更充分
- 代价：内存翻倍

### 4.3 分级降级

更合理的降级是 `serve-expired`（已启用）：上游不可达时返回过期缓存，业务可用性优先。

## 5. 一致性演进（P2）

### 5.1 现状

逻辑一致（`cache-min-ttl: 60` + `rrset-roundrobin: no`），节点间可能存在最长 60 秒的窗口差异。

### 5.2 若业务要求更强一致

| 强度 | 方案 | 代价 |
|---|---|---|
| 60s 窗口（当前） | 各节点独立缓存 | 无 |
| 30s 窗口 | `cache-min-ttl: 30` | 权威查询量增加 |
| 5s 窗口 | `cache-min-ttl: 5` | 根/TLD 查询量大幅上升，不推荐 |
| 严格一致 | **改用权威 DNS** | 架构重构，节点间 AXFR/NOTIFY 同步 |

> **重要判断**：如果业务依赖 DNS 做**流量调度**（按 IP 分配流量、故障转移），递归器天然做不到严格一致，必须改用权威 DNS 模式。递归器适合「解析域名到真实 IP」的场景。

### 5.3 共享缓存实验（不建议）

理论上可用 Redis 作为 unbound 的外部缓存层（`cachedb` 模块）。但：

- 一次网络往返（~0.2ms）会让 p50 延迟翻倍
- Redis 成为单点，故障时全部降级
- 多节点共享缓存时，某节点写入的脏数据会污染全局

除非有极强一致性要求，否则**不推荐**。

## 6. 扩展能力（P2）

### 6.1 按域名分流

```lua
local overseas = newSuffixMatchNode()
overseas:add(newDNSName("google.com."))
overseas:add(newDNSName("github.com."))

addAction(SuffixMatchNodeRule(overseas), PoolAction("overseas"))
```

适用场景：国内节点直连海外权威延迟高，需要专门的出海线路。

### 6.2 客户端限速

```lua
local dbr = dynBlockRulesGroup()
dbr:setQueryRate(1000, 10, "Exceeded query rate", 60)   -- 10s 内超 1000 次，封 60s
dbr:setRCodeRate(DNSRCode.NXDOMAIN, 500, 10, "Exceeded NXDOMAIN rate", 60)
dbr:apply()
```

### 6.3 内部域名静态解析

在 unbound 用 `local-zone` / `local-data`，避免内部服务名走递归：

```
local-zone: "internal." static
local-data: "db.internal. 300 IN A 10.0.0.5"
```

### 6.4 IPv6

```
do-ip6: yes
interface: ::1@5353
```

配合 dnsdist `addLocal("[::]:53")`。当前已关闭（无 IPv6 出口，避免超时拖慢解析）。

## 7. 配置管理（P2）

### 7.1 Git + Ansible

```yaml
- hosts: dns_nodes
  tasks:
    - name: 安装软件包
      dnf: name={{ item }} state=present
      loop: [unbound, dnsdist, bind-utils]

    - name: 下发配置
      copy: src=etc/{{ item }} dest=/etc/{{ item }} owner=root group=root mode=0644
      loop: [unbound/unbound.conf, unbound/conf.d/recursor.conf]

    - name: 修正 dnsdist 配置属主
      file: path=/etc/dnsdist/dnsdist.conf owner=root group=dnsdist mode=0640

    - name: 校验配置
      command: "{{ item }}"
      loop:
        - unbound-checkconf /etc/unbound/unbound.conf
        - dnsdist --check-config -C /etc/dnsdist/dnsdist.conf

    - name: 重启服务
      systemd: name={{ item }} state=restarted daemon_reload=yes
      loop: [unbound, dnsdist]

    - name: 冒烟测试
      command: dig @{{ ansible_default_ipv4.address }} www.example.com A +time=3 +tries=1
      register: r
      failed_when: "\"NOERROR\" not in r.stdout"
```

### 7.2 灰度发布

1. 选择 1 台非核心节点
2. 下发新配置并重启
3. 观察 30 分钟：QPS、SERVFAIL 率、平均延迟、缓存命中率
4. 无异常则按批次（25% -> 50% -> 100%）推进

### 7.3 配置版本化

每次变更提交一个 commit，commit message 记录变更原因。回滚 = `git revert` + 重新下发。

## 8. 容量与成本（P2）

### 8.1 容量规划表

| 目标 QPS | 节点数（4C） | 节点数（8C） | 备注 |
|---|---|---|---|
| 2 万 | 1（当前） | 1 | 余量 2.4x |
| 5 万 | 3 | 2 | N+1 |
| 10 万 | 8 | 4 | N+1 |
| 50 万 | 34 | 17 | 需 Anycast |
| 100 万 | 67 | 34 | 需分层架构 |

> 计算基准：4C 节点按 1.5 万 QPS 规划（单核 15k x 保守系数）。

### 8.2 成本优化

- **缓存命中率优先**：命中率从 90% -> 95%，上游查询量减半，可减少节点数
- **混合部署**：低频节点与业务机器混部（注意 CPU 争抢）
- **按需扩缩**：云上使用弹性伸缩 + CLB

## 9. 技术债与风险登记

| 项 | 风险 | 优先级 | 建议 |
|---|---|---|---|
| ACL 全开 | 高（可被滥用） | P0 | 上线前收紧 |
| 控制台密钥为默认值 | 中 | P0 | 生成新密钥 |
| 无监控 | 高（故障不可见） | P1 | 见第 2 节 |
| dnsdist 单核瓶颈 | 中（限单机吞吐） | P1 | 多进程或升级 2.0 |
| 单机部署无冗余 | 高（宕机全挂） | P1 | 多节点 + VIP |
| dnsdist 1.9.6 CVE | 低（未用 DoH） | P2 | 启用 DoH 前必须升级 |
| 配置手工下发 | 中（易出错） | P2 | Ansible |
| 一致性为逻辑一致 | 低（已明确） | P3 | 业务侧确认可接受 |

## 10. 演进路线图

```
现在 ------------------------------------------------------->
  |
  +- 阶段一：上线（1 周）
  |    收紧 ACL、换密钥、配监控、外部拨测
  |
  +- 阶段二：高可用（1 月）
  |    4 台节点 + CLB，N+1 冗余，容量 6 万 QPS
  |
  +- 阶段三：性能突破（按需）
  |    多进程 dnsdist 或升级 2.0，单机 12 万 QPS
  |
  +- 阶段四：大规模（按需）
       Anycast 多 PoP，分层缓存，容量 100 万 QPS
```
