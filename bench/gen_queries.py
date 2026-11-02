#!/usr/bin/env python3
"""生成 dnsperf 查询集：热缓存集 与 冷域名集。"""
HOT = ["www.example.com","www.qq.com","www.baidu.com","www.cloudflare.com",
       "google.com","www.taobao.com","www.aliyun.com","github.com",
       "www.bing.com","www.sohu.com","www.163.com","www.jd.com",
       "www.xiaomi.com","www.zhihu.com","www.weibo.com","www.icloud.com",
       "www.amazon.com","www.microsoft.com","www.apple.com","www.netflix.com"]

def main():
    with open("cache_hit.txt", "w") as f:
        n = 0
        while n < 200000:
            for d in HOT:
                f.write(f"{d} A\n"); n += 1
                if n >= 200000: break
    with open("cold_mix.txt", "w") as f:
        for i in range(20000):
            f.write(f"host{i}.example.com A\n")

if __name__ == "__main__":
    main()
