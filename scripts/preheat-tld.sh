#!/usr/bin/env bash
# 预热主流 TLD 的 NS 记录到 unbound 缓存
#
# 背景: .com/.net 等 TLD 的 NS 记录 TTL 为 48 小时，一旦缓存，
#       所有该 TLD 下的域名查询都省去「根 -> TLD」这一跳。
#       服务重启或 TLD 缓存失效时，冷查询会退化为完整迭代，
#       单次延迟从 ~150ms 涨到 ~250ms，吞吐从 19k 掉到 1k。
#
# 用法: preheat-tld.sh [unbound 地址:端口]
set -u

TARGET="${1:-127.0.0.1}"
PORT="${2:-5353}"

# 一级 TLD
TLDS="com net org cn io co info biz me tv cc xyz top vip site online club shop app dev
      edu gov mil int arpa de uk jp kr fr ru br in au ca nl se ch it es pl be at"

# 二级 CDN / 托管后缀
# 这些后缀的 NS 记录同样缓存 48h，但预热脚本原本只覆盖一级 TLD，
# 导致访问 CDN 域名时仍要完整迭代（实测 p90 从 96ms 涨到 896ms）。
# 可按业务实际流量补充。
SUFFIXES="fastly.net akamaized.net edgekey.net edgesuite.net b-cdn.net azureedge.net
          cdn.cloudflare.net cloudfront.net akamaihd.net llndns.com"
TLDS="$TLDS $SUFFIXES"

echo "preheating TLD/suffix NS records via ${TARGET}#${PORT} ..."
ok=0
fail=0
for tld in $TLDS; do
    # 后台并发，避免串行等待
    ( dig +short +time=3 +tries=1 "@${TARGET}" -p "${PORT}" "${tld}" NS >/dev/null 2>&1 ) &
done
wait

# 复核
for tld in $TLDS; do
    n=$(dig +short +time=2 +tries=1 "@${TARGET}" -p "${PORT}" "${tld}" NS 2>/dev/null | wc -l)
    if [ "$n" -gt 0 ]; then ok=$((ok+1)); else fail=$((fail+1)); fi
done

echo "TLD preheat done: ok=${ok} fail=${fail}"
[ "$fail" -lt 5 ]
