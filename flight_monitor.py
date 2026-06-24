#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
机票低价监控脚本（Travelpayouts 数据 API + PushPlus）—— 为 Max 定制
运行环境：GitHub Actions（每隔几小时自动运行一次，跟你的电脑无关）

为什么用 Travelpayouts 而不是直接抓谷歌：
谷歌已把航班结果改成「浏览器里用 JS 现加载」，纯抓取（fast-flights 等）一律拿不到数据，
云端 IP 还会被封。Travelpayouts 是正经数据接口、免费、不被封、云端能跑。
代价：它给的是「全球用户搜过的最低缓存价」（保留约 7 天），是近似值、且具体日期不一定每次都有数据。
收到提醒后请务必上携程/Aviasales 核对再下单。

它做的事：
1. 调 Travelpayouts 接口查每条航线的最低往返缓存价（直接要人民币）
2. 在返回结果里筛出你能接受的出发/返回日期
3. 低于阈值就通过 PushPlus 推送到你的微信
"""

import os
import time

try:
    import requests
except ImportError:
    requests = None


# =======================================================================
#  ↓↓↓ 你可以自己改的配置 ↓↓↓
# =======================================================================

# 乘客人数（仅用于展示提醒文案；Travelpayouts 的价格本身是「每人往返价」）
ADULTS = 2

# 北京城市代码（BJS 同时涵盖首都PEK与大兴PKX两个机场）
ORIGIN = "BJS"

# 你能接受的出发日 / 返回日（脚本会在返回结果里只挑落在这些日子的）
DEPART_DAYS = {"2026-09-25", "2026-09-26"}
RETURN_DAYS = {"2026-10-06", "2026-10-07"}

# 查询用的月份（接口按月返回缓存里的最低价，再由脚本按上面的具体日子筛）
DEPART_MONTH = "2026-09"
RETURN_MONTH = "2026-10"

# 监控目标：  名字 -> (目的城市代码列表, 每人往返价阈值/人民币)
#   BKK = 曼谷   CNX = 清迈   MAD = 马德里   BCN = 巴塞罗那
TARGETS = {
    "曼谷":                 (["BKK"],        3000),
    "清迈":                 (["CNX"],        3000),
    "西班牙(马德里/巴塞罗那)": (["MAD", "BCN"], 6000),
}

CURRENCY = "cny"  # 让接口直接返回人民币价

# 每人低于这个金额（人民币）视为无效价，丢弃（防止异常的 0 价误触发）
MIN_PRICE_CNY = 200

# 每次请求之间等待的秒数（礼貌起见，避免触发接口限流）
SLEEP_BETWEEN = 2

API_URL = "https://api.travelpayouts.com/v1/prices/cheap"

# =======================================================================
#  ↑↑↑ 配置到此为止，下面的代码一般不用动 ↑↑↑
# =======================================================================


PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
TRAVELPAYOUTS_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()


def query_dest(dest):
    """查一个目的地的最低往返缓存价。
    返回 (落在你接受日期内的最低每人价, 说明) ；查不到返回 (None, 原因)。
    同时把所有返回条目打印出来，方便你看接口到底给了什么。"""
    if requests is None:
        return None, "缺少 requests 库"
    try:
        r = requests.get(
            API_URL,
            params={
                "origin": ORIGIN,
                "destination": dest,
                "depart_date": DEPART_MONTH,
                "return_date": RETURN_MONTH,
                "currency": CURRENCY,
                "token": TRAVELPAYOUTS_TOKEN,
            },
            headers={"X-Access-Token": TRAVELPAYOUTS_TOKEN},
            timeout=30,
        )
    except Exception as e:  # noqa: BLE001
        return None, "请求出错: %s" % e

    if r.status_code != 200:
        return None, "HTTP %s: %s" % (r.status_code, r.text[:120])
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return None, "返回非JSON: %s" % r.text[:120]
    if not body.get("success"):
        return None, "接口success=false: %s" % str(body.get("error"))[:120]

    entries = (body.get("data") or {}).get(dest) or {}
    if not entries:
        return None, "缓存里暂无该航线数据"

    best = None       # (每人价, 出发日, 返回日)
    cheapest_any = None
    for _, info in entries.items():
        price = info.get("price")
        dep = (info.get("departure_at") or "")[:10]
        ret = (info.get("return_at") or "")[:10]
        if not price or price < MIN_PRICE_CNY:
            continue
        # 打印每一条，方便诊断数据质量
        print("      · 出发%s 返回%s 每人¥%s" % (dep or "?", ret or "?", price))
        if cheapest_any is None or price < cheapest_any[0]:
            cheapest_any = (price, dep, ret)
        if dep in DEPART_DAYS and ret in RETURN_DAYS:
            if best is None or price < best[0]:
                best = (price, dep, ret)

    if best is not None:
        return best[0], "出发%s 返回%s（命中你的日期）" % (best[1], best[2])
    if cheapest_any is not None:
        # 没有命中你的具体日子，但有别的日期数据——返回 None，附带提示
        return None, "无命中日期；该月最低是 出发%s 返回%s 每人¥%s" % (
            cheapest_any[1], cheapest_any[2], cheapest_any[0])
    return None, "无有效价格"


def push_wechat(title, content):
    """通过 PushPlus 推送到微信。"""
    if not PUSHPLUS_TOKEN:
        print("！未设置 PUSHPLUS_TOKEN 环境变量，跳过推送。")
        return
    if requests is None:
        print("！缺少 requests 库，无法推送。")
        return
    try:
        r = requests.post(
            "http://www.pushplus.plus/send",
            json={
                "token": PUSHPLUS_TOKEN,
                "title": title,
                "content": content,
                "template": "html",
            },
            timeout=20,
        )
        print("PushPlus 返回:", r.text[:200])
    except Exception as e:  # noqa: BLE001
        print("！推送失败:", e)


def main():
    print("=== 机票监控开始（数据源：Travelpayouts 缓存价）===")
    if not TRAVELPAYOUTS_TOKEN:
        print("！未设置 TRAVELPAYOUTS_TOKEN，无法查询。请在 GitHub Secrets 里配置后再运行。")
        return

    alerts = []
    for target_name, (dests, threshold) in TARGETS.items():
        print("\n--- 监控目标：%s（每人阈值 ¥%d）---" % (target_name, threshold))
        cheapest = None  # (每人价, 说明)
        for dest in dests:
            print("  查 %s→%s ..." % (ORIGIN, dest))
            price, note = query_dest(dest)
            if price is None:
                print("    [无命中] %s" % note)
            else:
                print("    %s→%s 每人 ≈ ¥%s（%s）" % (ORIGIN, dest, price, note))
                if cheapest is None or price < cheapest[0]:
                    cheapest = (price, "%s→%s %s" % (ORIGIN, dest, note))
            time.sleep(SLEEP_BETWEEN)

        if cheapest is None:
            print("  %s：本轮没查到命中日期的有效价格" % target_name)
            continue

        per_person, desc = cheapest
        print("  >>> %s 本轮最低：每人 ≈ ¥%s" % (target_name, per_person))
        if per_person < threshold:
            alerts.append(
                "<b>%s</b> 触发低价！<br>"
                "每人约 <b>¥%s</b>（阈值 ¥%d）<br>"
                "%s<br>"
                "⚠️ 这是 Travelpayouts 缓存的近似价，请上携程/Aviasales 核对后再下单。"
                % (target_name, per_person, threshold, desc)
            )

    if alerts:
        push_wechat("✈️ 机票低价提醒", "<br><br>".join(alerts))
        print("\n已发送提醒。")
    else:
        print("\n本轮没有触发提醒。")
    print("=== 机票监控结束 ===")


if __name__ == "__main__":
    main()
