#!/usr/bin/env python3
"""探测根服务器可达性与 RTT，生成优化后的 root.hints。

从国内网络访问根服务器，各站点 RTT 与丢包率差异极大（实测 4ms ~ 258ms，
丢包 0 ~ 55%）。unbound 默认会在全部 13 台根之间做选择，
因此很大概率命中慢速或有丢包的根，显著拉高冷查询延迟。

本脚本筛选出「零丢包且 RTT 最低」的若干台根，生成 root.hints。
配合 recursor.conf 中的 do-not-query-address 排除其余根，
使 unbound 只使用最快的根。

用法:
    python3 probe-roots.py                            # 探测并打印
    python3 probe-roots.py -o /etc/unbound/root.hints # 写入文件
    python3 probe-roots.py -n 4                       # 保留最快的 4 台
"""
import argparse
import random
import socket
import statistics
import struct
import sys
import time

ROOTS = {
    "a.root-servers.net": "198.41.0.4",
    "b.root-servers.net": "199.9.14.201",
    "c.root-servers.net": "192.33.4.12",
    "d.root-servers.net": "199.7.91.13",
    "e.root-servers.net": "192.203.230.10",
    "f.root-servers.net": "192.5.5.241",
    "g.root-servers.net": "192.112.36.4",
    "h.root-servers.net": "198.97.190.53",
    "i.root-servers.net": "192.36.148.17",
    "j.root-servers.net": "192.58.128.30",
    "k.root-servers.net": "193.0.14.129",
    "l.root-servers.net": "199.7.83.42",
    "m.root-servers.net": "202.12.27.33",
}


def probe(ip, count=20, timeout=1.0):
    """向根服务器发 count 次 NS . 查询，返回 (丢包数, 中位 RTT ms)。"""
    lat, fail = [], 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    for _ in range(count):
        tid = random.randint(1, 65535)
        # header + root QNAME + qtype NS + qclass IN
        pkt = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
        pkt += b"\x00" + struct.pack(">HH", 2, 1)
        t0 = time.perf_counter()
        try:
            sock.sendto(pkt, (ip, 53))
            sock.recvfrom(512)
            lat.append((time.perf_counter() - t0) * 1000)
        except (socket.timeout, OSError):
            fail += 1
    sock.close()
    return fail, (statistics.median(lat) if lat else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", help="写入 root.hints 的路径")
    ap.add_argument("-n", "--keep", type=int, default=4, help="保留最快的 N 台（默认 4）")
    ap.add_argument("-c", "--count", type=int, default=20, help="每台探测次数（默认 20）")
    args = ap.parse_args()

    print("%-22s %-16s %6s %9s" % ("name", "ip", "fail", "p50(ms)"), file=sys.stderr)
    results = []
    for name, ip in ROOTS.items():
        fail, med = probe(ip, args.count)
        results.append((name, ip, fail, med))
        print("%-22s %-16s %2d/%d %9s" % (
            name, ip, fail, args.count, ("%.0f" % med) if med else "-"), file=sys.stderr)

    # 优先零丢包，再按 RTT 升序
    ok = [r for r in results if r[3] is not None]
    ok.sort(key=lambda r: (r[2], r[3]))
    selected = ok[: args.keep]

    if not selected:
        sys.exit("没有可达的根服务器，检查 UDP 53 出站")

    lines = [
        "; 自定义 root hints - 仅保留实测可达且延迟最低的根服务器",
        "; 由 scripts/probe-roots.py 探测生成，建议每季度复测",
        "; 修改后需同步更新 recursor.conf 中的 do-not-query-address",
    ]
    for name, ip, fail, med in selected:
        lines.append(".                        3600000  NS    %s." % name)
        lines.append("%-24s 3600000  A     %s" % (name + ".", ip))
    out = "\n".join(lines) + "\n"

    print("\n选中的根:", file=sys.stderr)
    for name, ip, fail, med in selected:
        print("  %-22s %-16s fail=%d p50=%.0fms" % (name, ip, fail, med), file=sys.stderr)

    excluded = [r for r in results if r not in selected]
    print("\n以下根应加入 do-not-query-address:", file=sys.stderr)
    for name, ip, fail, med in excluded:
        print("    do-not-query-address: %s/32" % ip, file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            f.write(out)
        print("\n已写入 %s" % args.output, file=sys.stderr)
    else:
        print()
        print(out)


if __name__ == "__main__":
    main()
