#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
机票低价监控脚本（fast-flights + PushPlus）—— 为 Max 定制
运行环境：GitHub Actions（每隔几小时自动运行一次，跟你的电脑无关）

它做的事：
1. 用 fast-flights 抓取 Google Flights（谷歌航班）的【往返】机票价格
2. 遍历你设定的航线 × 日期组合，取“每人最低价”
3. 低于阈值就通过 PushPlus 推送到你的微信
"""

import os
import re
import time

from fast_flights import FlightData, Passengers, get_flights

try:
    import requests
except ImportError:
    requests = None


# =======================================================================
#  ↓↓↓ 你可以自己改的配置（改完保存、重新上传到 GitHub 即可生效）↓↓↓
# =======================================================================

# 乘客人数（经济舱 2 人）
ADULTS = 2

# 出发 / 返回的候选日期，脚本会把它们两两组合（4 个组合）
DEPART_DATES = ["2026-09-26"]  # 诊断用：临时只留1个日期
RETURN_DATES = ["2026-10-06"]  # 诊断用：临时只留1个日期

# 北京的两个机场：PEK = 首都，PKX = 大兴
BEIJING = ["PEK", "PKX"]

# 监控目标：  名字 -> (出发机场列表, 目的机场列表, 每人价格阈值/人民币)
#   BKK = 曼谷素万那普   DMK = 曼谷廊曼（廉航多）
#   CNX = 清迈
#   MAD = 马德里        BCN = 巴塞罗那
TARGETS = {
    "曼谷":                 (BEIJING, ["BKK", "DMK"], 3000),
    "清迈":                 (BEIJING, ["CNX"],        3000),
    "西班牙(马德里/巴塞罗那)": (BEIJING, ["MAD", "BCN"], 6000),
}

# 美元转人民币的近似汇率。
# fast-flights 跑在国外服务器时，谷歌常返回美元报价，脚本用它换算成人民币再比较。
# 不用很精确——收到提醒后你会自己上携程核对。可按当时汇率改这个数。
USD_TO_CNY = 7.2

# 每人低于这个金额（人民币）一律视为「无效价格」丢弃。
# 谷歌对部分航线会返回一个价格为 0 的占位航班，不拦掉就会算出「每人 ¥0」并误触发低价提醒。
# 跨国往返机票每人绝不可能低于这个数，按此兜底。
MIN_PRICE_CNY = 200

# 每次查询之间等待的秒数（防止请求太频繁被谷歌限流）
SLEEP_BETWEEN = 4

# fast-flights 抓取模式，脚本按顺序尝试，一个失败自动换下一个。
# local  = 在本机（GitHub 服务器）开真实浏览器，等结果加载完再读，最稳，但慢
# fallback = 作者的公共 serverless 浏览器，作为备用（可能限流/失效）
FETCH_MODES = ["local"]  # 诊断用：只跑local，暴露其真实报错

# =======================================================================
#  ↑↑↑ 配置到此为止，下面的代码一般不用动 ↑↑↑
# =======================================================================


PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()


def parse_price(price_str):
    """把 '$1,234' / '¥3,456' / 'CN¥3,456' 解析为 (人民币金额, 币种说明)。
    解析不出来返回 (None, 原因)。"""
    if not price_str:
        return None, "空价格"
    s = str(price_str)
    digits = re.sub(r"[^\d.]", "", s)
    if not digits:
        return None, "无法解析: %s" % s
    try:
        amount = float(digits)
    except ValueError:
        return None, "无法解析: %s" % s
    if amount <= 0:
        return None, "价格为0/无效: %s" % s

    up = s.upper()
    if "¥" in s or "￥" in s or "CNY" in up or "RMB" in up:
        return amount, "CNY"
    if "$" in s or "USD" in up:
        return amount * USD_TO_CNY, "USD→CNY(×%s)" % USD_TO_CNY
    # 认不出币种：GitHub 服务器多在美国，默认按美元换算，并标注“不确定”
    return amount * USD_TO_CNY, "未知币种,按USD换算(×%s)" % USD_TO_CNY


def query_one(origin, dest, depart, ret):
    """查一个具体往返组合，返回 (该组合每人最低价/人民币, 说明)；失败返回 (None, 错误信息)。"""
    flight_data = [
        FlightData(date=depart, from_airport=origin, to_airport=dest),
        FlightData(date=ret,    from_airport=dest,   to_airport=origin),
    ]
    last_err = None
    for mode in FETCH_MODES:
        try:
            result = get_flights(
                flight_data=flight_data,
                trip="round-trip",
                seat="economy",
                passengers=Passengers(
                    adults=ADULTS, children=0,
                    infants_in_seat=0, infants_on_lap=0,
                ),
                fetch_mode=mode,
            )
            flights = getattr(result, "flights", None) or []
            best = None
            best_note = ""
            for f in flights:
                cny_total, cur_note = parse_price(getattr(f, "price", None))
                if cny_total is None:
                    continue
                per_person = cny_total / ADULTS  # 谷歌显示的是全部乘客总价 → 折成每人
                if per_person < MIN_PRICE_CNY:  # 低于兜底线 → 当作占位/异常价，丢弃
                    continue
                if best is None or per_person < best:
                    best = per_person
                    best_note = "%s 原价%s [%s]" % (
                        getattr(f, "name", "?"),
                        getattr(f, "price", "?"),
                        cur_note,
                    )
            if best is not None:
                return best, best_note
            last_err = "未解析到任何价格"
        except Exception as e:  # noqa: BLE001
            last_err = "%s 模式出错: %s" % (mode, e)
            continue
    return None, last_err or "查询失败"


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
    print("=== 机票监控开始 ===")
    combos = [(d, r) for d in DEPART_DATES for r in RETURN_DATES]
    alerts = []

    for target_name, (origins, dests, threshold) in TARGETS.items():
        print("\n--- 监控目标：%s（每人阈值 ¥%d）---" % (target_name, threshold))
        cheapest = None  # (每人价/人民币, 描述)
        for origin in origins:
            for dest in dests:
                for depart, ret in combos:
                    per_person, note = query_one(origin, dest, depart, ret)
                    tag = "%s→%s %s~%s" % (origin, dest, depart, ret)
                    if per_person is None:
                        print("  [跳过] %s: %s" % (tag, note))
                    else:
                        print("  %s: 每人 ≈ ¥%.0f  (%s)" % (tag, per_person, note))
                        if cheapest is None or per_person < cheapest[0]:
                            cheapest = (per_person, "%s\n%s" % (tag, note))
                    time.sleep(SLEEP_BETWEEN)

        if cheapest is None:
            print("  %s：本轮没查到有效价格" % target_name)
            continue

        per_person, desc = cheapest
        print("  >>> %s 本轮最低：每人 ≈ ¥%.0f" % (target_name, per_person))
        if per_person < threshold:
            alerts.append(
                "<b>%s</b> 触发低价！<br>"
                "每人约 <b>¥%.0f</b>（阈值 ¥%d）<br>"
                "%s<br>"
                "⚠️ 这是 Google Flights 估算价，请上携程核对后再下单。"
                % (target_name, per_person, threshold,
                   desc.replace("\n", "<br>"))
            )

    if alerts:
        push_wechat("✈️ 机票低价提醒", "<br><br>".join(alerts))
        print("\n已发送提醒。")
    else:
        print("\n本轮没有触发提醒。")
    print("=== 机票监控结束 ===")


if __name__ == "__main__":
    main()
