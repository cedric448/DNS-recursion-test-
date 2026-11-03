#!/usr/bin/env python3
"""生成压测用的冷域名查询集。

冷缓存测试必须每轮使用全新域名，否则第二次压测时全部命中缓存，
测出的是热缓存结果（实测可达 86k QPS），完全无法反映回源能力。

用法:
    python3 gen_cold.py              # 默认 20000 条 -> /tmp/cold.txt
    python3 gen_cold.py -n 5000 -o /tmp/c.txt
"""
import argparse
import random
import string


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--count", type=int, default=20000)
    ap.add_argument("-o", "--output", default="/tmp/cold.txt")
    ap.add_argument("-d", "--domain", default="example.com", help="使用的域（需可控，避免污染真实解析）")
    args = ap.parse_args()

    rnd = random.Random()
    with open(args.output, "w") as f:
        for i in range(args.count):
            tag = "".join(rnd.choices(string.ascii_lowercase, k=10))
            f.write("%s%d.%s A\n" % (tag, i, args.domain))

    print("生成 %d 条 -> %s" % (args.count, args.output))


if __name__ == "__main__":
    main()
