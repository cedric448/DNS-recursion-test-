# DNS 递归服务 - 方案与实测

本机自建 DNS 递归服务，云上机器指向本机解析域名。完整递归到根服务器，不依赖任何运营商/云厂商上游 DNS。

## 架构

```
云上机器 ──UDP/TCP 53──> dnsdist ──> unbound(127.0.0.1:5353) ──> 根服务器迭代
```

| 组件 | 版本 | 监听 | 职责 |
|---|---|---|---|
| dnsdist | 1.9.6 | `0.0.0.0:53` UDP+TCP | 接入层、ACL、健康检查、多节点负载分发 |
| unbound | 1.17.1 | `127.0.0.1:5353` | 递归解析、DNSSEC 校验、缓存 |

## 设计要点

1. **完整递归**：unbound 直接从根服务器迭代，不经 183 等外部上游
2. **一致性**：`cache-min-ttl: 60` 稳定短 TTL 记录、`rrset-roundrobin: no` 避免同域名多次解析结果轮换
3. **横向扩展**：dnsdist 每进程约 1.5 万 QPS（单核上限），多进程/多节点线性扩展
4. **容错**：`serve-expired` 在权威不可达时返回过期缓存而非 SERVFAIL

## 实测数据（4 核 / 7.5G）

| 场景 | QPS | 说明 |
|---|---|---|
| 目标 2 万（限速） | 19.1k | 0 丢包，平均延迟 1.17ms |
| dnsdist 单进程不限速 | 48k | 单核约 1.5 万 |
| 2 进程 | 56k | 接近线性 |
| 4 进程 | 61.8k | 4 核占满 |
| Unbound 直连 | 125k | 未到上限 |
| 冷域名全递归 | 1.1k | 受上游 RTT 限制，正常 |

**结论：2 万 QPS 达标，余量 2.4 倍；瓶颈是 dnsdist 单核。**

## 部署

```bash
yum install -y unbound dnsdist bind-utils dnsperf

cp etc/unbound/conf.d/recursor.conf /etc/unbound/conf.d/
cp etc/unbound/unbound.conf           /etc/unbound/unbound.conf
cp etc/dnsdist/dnsdist.conf           /etc/dnsdist/dnsdist.conf
cp etc/sysctl.d/99-dns.conf           /etc/sysctl.d/
cp etc/security/limits.d/99-dns.conf  /etc/security/limits.d/
cp etc/systemd/dnsdist-override.conf  /etc/systemd/system/dnsdist.service.d/override.conf

chown root:dnsdist /etc/dnsdist/dnsdist.conf && chmod 640 /etc/dnsdist/dnsdist.conf

sysctl -p /etc/sysctl.d/99-dns.conf
systemctl daemon-reload
systemctl enable --now unbound dnsdist
```

## 验证

```bash
dig @127.0.0.1 www.example.com A        # 递归可用
dig @127.0.0.1 dnssec-failed.org A      # 应返回 SERVFAIL（DNSSEC 生效）
dig @127.0.0.1 www.example.com A +tcp   # TCP 53 可用
```

## 压测

```bash
cd bench && python3 gen_queries.py
dnsperf -s 127.0.0.1 -d cache_hit.txt -c 200 -T 8 -l 15           # 不限速上限
dnsperf -s 127.0.0.1 -d cache_hit.txt -c 100 -T 8 -Q 20000 -l 20 # 2 万 QPS 限速
dnsperf -s 127.0.0.1 -d cold_mix.txt -c 100 -T 8 -l 15           # 冷域名递归
```

## 横向扩展（10 万 QPS）

```
              [ VIP / Anycast ]  ← 云 CLB 或 Anycast，UDP 53
                     │
   ┌─────────┬───────┴───────┬─────────┐
 [DNS-1]   [DNS-2]         [DNS-3]   [DNS-N]
 dnsdist   dnsdist         dnsdist   dnsdist
   │         │               │         │
 unbound   unbound         unbound   unbound
   └─────────┴── 根服务器迭代 ─────────┘
```

- 单节点 4C 生产按 1.5 万 QPS 使用（留余量）
- 10 万 QPS：8 台 4C 节点 + VIP 健康检查分流
- 单机扩容：挂多个内网 IP，每个 IP 起一个 dnsdist 进程
- 20 台以上：需在节点间共享根/NXDOMAIN 缓存，避免超 ICANN 根服务器 QPM 配额

## 注意事项

- **ACL**：`dnsdist.conf` 中 `setACL({"0.0.0.0/0"})` 仅为测试用，上线前收紧到实际 VPC 网段
- **控制台密钥**：`setKey()` 需用 `makeKey()` 重新生成
- **CVE-2025-30194**：影响 1.9.0–1.9.8 的 DoH(nghttp2) 场景；本配置未启用 DoH，不受影响。TencentOS EPOL 源无 1.9.9+ 包

## 踩坑记录

1. **Unbound 起不来（fatal: trust anchor presented twice）**
   TencentOS 默认 `unbound.conf` 同时启用 `auto-trust-anchor-file` 与 `auth-zone "."`，根信任锚被重复注册。需替换主配置，只 include 自定义 recursor.conf。

2. **dnsdist 起不来（Unable to read configuration file）**
   发行版将 `/etc/dnsdist/dnsdist.conf` 设为 `root:root 640`，但服务以 `dnsdist` 用户运行。改为 `root:dnsdist 640`。

3. **dnsdist 单核瓶颈**
   1.9 版本 `reusePort=true` 不提升吞吐，UDP 收包仍是单线程。需多进程 + 多 IP 监听才能用满多核。

4. **UDP 53 出站未被云厂商封锁**
   13 台根服务器中 12 台可达（b.root 199.9.14.201 超时，不影响递归）。
