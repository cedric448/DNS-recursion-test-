# 部署手册

本文档描述从零部署 dnsdist + Unbound DNS 递归服务的完整步骤。已在 TencentOS Server 4.4 (对应 RHEL 9 / el9) 验证通过。

## 1. 前置检查

### 1.1 系统要求

| 项 | 最低 | 推荐 |
|---|---|---|
| CPU | 2 核 | 4 核（2 万 QPS） |
| 内存 | 4 GB | 8 GB |
| 磁盘 | 20 GB | 20 GB |
| OS | el8+ | el9 (TencentOS 4 / Rocky 9 / Alma 9) |

### 1.2 网络检查：UDP 53 出站是否被封锁

**部署前必须先确认能直连根服务器**，否则递归无法工作。

```bash
python3 - <<'EOF'
import socket, struct, random
roots = ["198.41.0.4","199.9.14.201","192.33.4.12","199.7.91.13","192.203.230.10",
         "192.5.5.241","192.112.36.4","198.97.190.53","192.36.148.17","192.58.128.30",
         "193.0.14.129","199.7.83.42","202.12.27.33"]
ok = 0
for r in roots:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2)
    tid = random.randint(1, 65535)
    pkt = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + b"\x00\x00\x00\x02\x00\x01"
    try:
        s.sendto(pkt, (r, 53)); d, _ = s.recvfrom(512); print(r, "OK"); ok += 1
    except Exception as e:
        print(r, "FAIL", e)
    s.close()
print(f"\n可达 {ok}/13")
EOF
```

> 实测结果：12/13 可达，仅 `199.9.14.201` (b.root) 超时。**至少需要 5 台以上可达**才能正常递归。

若全部不可达，说明云厂商封锁了 UDP 53 出站，需要改用 DNS over TCP 或申请放行。

### 1.3 安装软件包

```bash
# TencentOS / OpenCloudOS
yum install -y unbound dnsdist bind-utils dnsperf

# Rocky / Alma / RHEL 9（dnsdist 需 EPEL + PowerDNS 官方源）
dnf install -y epel-release
curl -o /etc/yum.repos.d/powerdns-dnsdist.repo \
  https://repo.powerdns.com/repo-files/el-dnsdist-latest.repo
dnf install -y unbound dnsdist bind-utils dnsperf
```

验证版本：

```bash
unbound -V | head -1     # 期望 >= 1.17
dnsdist --version | head -1
```

## 2. 系统调优

### 2.1 内核参数

```bash
cp etc/sysctl.d/99-dns.conf /etc/sysctl.d/99-dns.conf
sysctl -p /etc/sysctl.d/99-dns.conf
```

关键项说明：

| 参数 | 值 | 作用 |
|---|---|---|
| `net.core.rmem_max` / `wmem_max` | 8 MB | 提高 UDP 缓冲区，防丢包 |
| `net.ipv4.ip_local_port_range` | 10240-65535 | 扩大出站源端口，支撑高并发递归 |
| `net.netfilter.nf_conntrack_max` | 1048576 | 防止 conntrack 表满导致新连接被丢 |
| `net.ipv4.udp_rmem_min` | 8192 | UDP 最小接收缓冲 |
| `fs.file-max` | 4000000 | 系统级文件句柄上限 |

### 2.2 文件句柄

```bash
cp etc/security/limits.d/99-dns.conf /etc/security/limits.d/99-dns.conf
```

dnsdist 的 systemd 单元同样需要提高：

```bash
mkdir -p /etc/systemd/system/dnsdist.service.d
cp etc/systemd/dnsdist-override.conf /etc/systemd/system/dnsdist.service.d/override.conf
systemctl daemon-reload
```

## 3. 部署 Unbound（递归层）

### 3.1 下发配置

```bash
mkdir -p /run/unbound
cp etc/unbound/conf.d/recursor.conf /etc/unbound/conf.d/recursor.conf
cp etc/unbound/unbound.conf          /etc/unbound/unbound.conf
chown unbound:unbound /run/unbound
unbound-checkconf && echo OK
```

### 3.2 ⚠️ 关键：替换发行版主配置

**TencentOS / OpenCloudOS 默认的 `/etc/unbound/unbound.conf` 会导致 unbound 启动失败**，原因：

- 同时启用 `auto-trust-anchor-file` **和** 内置 `auth-zone: name: "."`
- 根信任锚被重复注册 → `fatal error: trust anchor presented twice`

因此主配置只保留一行 include：

```
include: /etc/unbound/conf.d/recursor.conf
```

（本仓库 `etc/unbound/unbound.conf` 即为此内容）

> 部署到其他发行版时，若其默认配置不含 `auth-zone "."`，可直接在默认配置后追加 include，不必替换。

### 3.3 根信任锚

```bash
ls -l /var/lib/unbound/root.key
# 若不存在或过期：
unbound-anchor -a /var/lib/unbound/root.key -v
```

保证 `auto-trust-anchor-file` 指向的文件 **只包含根 KSK**，不要混入其他信任锚。

### 3.4 启动与验证

```bash
systemctl enable --now unbound
systemctl status unbound

# 直接测试递归器
dig @127.0.0.1 -p 5353 www.example.com A
dig @127.0.0.1 -p 5353 dnssec-failed.org A    # 期望 SERVFAIL
```

若失败：

```bash
journalctl -u unbound -n 50 --no-pager
# 或前台调试
unbound -c /etc/unbound/unbound.conf -d -vvv
```

## 4. 部署 dnsdist（接入层）

### 4.1 下发配置

```bash
cp etc/dnsdist/dnsdist.conf /etc/dnsdist/dnsdist.conf
```

### 4.2 ⚠️ 关键：修正配置文件属主

发行版将 `/etc/dnsdist/dnsdist.conf` 设为 `root:root 640`，而服务以 `dnsdist` 用户运行，**会报 `Unable to read configuration file` 而启动失败**。

```bash
chown root:dnsdist /etc/dnsdist/dnsdist.conf
chmod 640 /etc/dnsdist/dnsdist.conf
```

### 4.3 上线前必须修改的安全项

```bash
# 1. 收紧 ACL 到实际 VPC 网段（当前为 0.0.0.0/0，仅适合测试）
sed -i 's|setACL({"0.0.0.0/0","::/0"})|setACL({"10.0.0.0/8","172.16.0.0/12","192.168.0.0/16"})|' \
  /etc/dnsdist/dnsdist.conf

# 2. 生成控制台密钥
dnsdist -C /etc/dnsdist/dnsdist.conf --supervised \
  -e 'print(makeKey())' 2>/dev/null | tail -1
# 将输出替换到 setKey("...") 中
```

> **当前仓库中的 `setKey("dn5d1st-c0ntr0l-l0c4l")` 是测试用占位值，绝对不可用于生产。**

### 4.4 启动与验证

```bash
dnsdist --check-config -C /etc/dnsdist/dnsdist.conf
systemctl enable --now dnsdist
systemctl status dnsdist

ss -lunp | grep ':53 '
```

预期输出：

```
UNCONN 0  0  0.0.0.0:53  0.0.0.0:*  users:(("dnsdist",pid=...,fd=7))
LISTEN 0  4096  0.0.0.0:53  0.0.0.0:*  users:(("dnsdist",pid=...,fd=14))
```

## 5. 冒烟测试

```bash
IP=<本机内网IP>

# 基础解析
dig @$IP www.example.com A +time=3

# TCP 53
dig @$IP www.example.com A +tcp +time=3

# DNSSEC 校验生效
dig @$IP dnssec-failed.org A +time=3      # 期望 status: SERVFAIL

# 缓存生效（第二次应为 0 msec）
dig @$IP www.baidu.com A | grep 'Query time'
dig @$IP www.baidu.com A | grep 'Query time'

# 一致性（多次解析结果顺序应完全相同）
for i in 1 2 3; do dig @$IP www.example.com A +short; echo ---; done
```

**全部通过的标准：**

| 检查项 | 期望 |
|---|---|
| `www.example.com` A 记录 | NOERROR + 有 A 记录 |
| TCP 53 | NOERROR |
| `dnssec-failed.org` | SERVFAIL |
| 二次查询 | Query time: 0 msec |
| 多次解析 | A 记录顺序一致 |

## 6. 压测

### 6.1 生成测试数据集

```bash
mkdir -p bench && cd bench
python3 ../bench/gen_queries.py
wc -l cache_hit.txt cold_mix.txt
```

### 6.2 目标 QPS 验证（2 万）

```bash
# 预热缓存
for d in www.example.com www.qq.com www.baidu.com www.cloudflare.com \
         google.com www.taobao.com www.aliyun.com github.com; do
  dig @127.0.0.1 $d A +short > /dev/null
done

dnsperf -s 127.0.0.1 -d cache_hit.txt -c 100 -T 8 -Q 20000 -l 20
```

**参考结果（4C 机器）：**

```
Queries per second:   19107
Queries lost:         0 (0.00%)
Average Latency:      0.001166 s
```

### 6.3 极限吞吐

```bash
dnsperf -s 127.0.0.1 -d cache_hit.txt -c 200 -T 8 -l 15
```

参考值（4C）：约 48k QPS。

### 6.4 对比基线

```bash
# 绕过 dnsdist 直连 unbound —— 用于定位瓶颈
dnsperf -s 127.0.0.1 -p 5353 -d cache_hit.txt -c 200 -T 8 -l 15
```

参考值：约 125k QPS。若该值远高于 dnsdist 结果，说明瓶颈在 dnsdist 接入层。

### 6.5 冷域名（真递归）

```bash
dnsperf -s 127.0.0.1 -d cold_mix.txt -c 100 -T 8 -l 15
```

参考值：约 1.1k QPS，平均延迟 100ms（受上游 RTT 限制，属正常）。

## 7. 多节点部署（10 万 QPS）

在每台节点执行第 1~6 步，然后：

1. **云 CLB / VIP**：配置 UDP 53 四层监听，后端为所有节点内网 IP
2. **健康检查**：UDP 探活 + TCP 探活（部分云厂商 UDP 探活不可靠，建议同时配 TCP）
3. **dnsdist 策略**：改为 `setServerPolicy(roundrobin)`（单机时用 `firstAvailable` 无所谓，但多节点必须轮询）
4. **节点数**：`峰值QPS / 15000`，向上取整 + 1（冗余）

## 8. 运维手册

### 8.1 常用命令

```bash
# 查看状态
systemctl status unbound dnsdist

# 查看监听
ss -lunp | grep ':53 '

# 查看缓存统计
unbound-control stats_noreset | grep -E 'cache.hits|cache.miss|answer.servfail'

# 查看 dnsdist 后端状态
dnsdist -C /etc/dnsdist/dnsdist.conf -c 'showServers()'

# 查看 dnsdist 统计
dnsdist -C /etc/dnsdist/dnsdist.conf -c 'showStats()'
```

### 8.2 缓存操作

```bash
# 需要在 recursor.conf 中启用 remote-control
unbound-control flush www.example.com     # 清除单条
unbound-control flush_zone example.com    # 清除整个 zone
unbound-control flush_negative            # 清除负缓存
unbound-control reload                    # 重新加载
```

### 8.3 配置变更流程

```bash
# 1. 修改本仓库 etc/ 下配置
# 2. 本地校验
unbound-checkconf /etc/unbound/unbound.conf
dnsdist --check-config -C /etc/dnsdist/dnsdist.conf

# 3. 灰度单台
# 4. 观察 10 分钟指标
# 5. 全量下发
```

### 8.4 故障排查

| 现象 | 排查方向 |
|---|---|
| unbound 起不来 | `journalctl -u unbound`，常见：信任锚重复、root.key 权限 |
| dnsdist 起不来 | `journalctl -u dnsdist`，常见：配置文件权限、ACL 语法、setKey 校验失败 |
| 解析超时 | `dig +trace` 检查迭代链路；检查 UDP 53 出站 |
| 大量 SERVFAIL | 检查 DNSSEC 时间是否准确（`timedatectl`）、上游可达性 |
| 缓存命中率低 | 检查是否存在大量随机子域名；考虑 `prefetch: yes` 已开启 |
| 吞吐上不去 | 对比 6.4 直连 unbound，定位是否 dnsdist 瓶颈 |

## 9. 部署检查清单

上线前逐项确认：

- [ ] UDP/TCP 53 出站可达根服务器（≥ 5 台）
- [ ] 内核参数已生效（`sysctl -p`）
- [ ] ulimit 与 systemd `LimitNOFILE` 已配置
- [ ] unbound 主配置已替换（避免 auth-zone 冲突）
- [ ] root.key 存在且仅含根 KSK
- [ ] dnsdist.conf 属主为 `root:dnsdist` 权限 640
- [ ] **ACL 已收紧到实际 VPC 网段**
- [ ] **控制台密钥已用 `makeKey()` 重新生成**
- [ ] 冒烟测试 5 项全部通过
- [ ] 压测达到目标 QPS 且丢包为 0
- [ ] 已配置监控告警
- [ ] 已规划多节点/VIP 容灾
