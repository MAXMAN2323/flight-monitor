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
import json
import time

try:
    import requests
except ImportError:
    requests = None

# 记录「上次已提醒的最低价」，避免同一个低价每 2 小时重复推送。
# 这个文件由 GitHub Actions 的缓存在多次运行之间保留。
STATE_FILE = "state.json"


# =======================================================================
#  ↓↓↓ 你可以自己改的配置 ↓↓↓
# =======================================================================

# 乘客人数（仅用于展示提醒文案；Travelpayouts 的价格本身是「每人往返价」）
ADULTS = 2

# 北京城市代码（BJS 同时涵盖首都PEK与大兴PKX两个机场）
ORIGIN = "BJS"

# 你的出行日期（精确）：9/25 或 9/26 出发，10/6 或 10/7 返回。
# 用区间表示，因为区间内只有这两天，等价于「只认这 4 个日期组合」。
DEPART_MIN, DEPART_MAX = "2026-09-25", "2026-09-26"
RETURN_MIN, RETURN_MAX = "2026-10-06", "2026-10-07"

# 查询用的月份（日历接口按月返回「每天一个最低价」）
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

API_URL = "https://api.travelpayouts.com/v1/prices/calendar"

# =======================================================================
#  ↑↑↑ 配置到此为止，下面的代码一般不用动 ↑↑↑
# =======================================================================


PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
TRAVELPAYOUTS_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()


def query_dest(dest):
    """查一个目的地：日历接口返回当月每天的最低往返价，脚本筛出落在出行窗口内的最低每人价。
    返回 (窗口内最低每人价, 说明)；查不到返回 (None, 原因)。
    同时把返回的条目打印出来，方便看接口数据。"""
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
                "calendar_type": "departure_date",
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

    # 日历接口的 data 是 {日期: {price, departure_at, return_at, ...}}
    entries = body.get("data") or {}
    if not entries:
        return None, "缓存里暂无该航线数据"

    best = None        # (每人价, 出发日, 返回日)
    cheapest_any = None
    for info in entries.values():
        price = info.get("price")
        dep = (info.get("departure_at") or "")[:10]
        ret = (info.get("return_at") or "")[:10]
        if not price or price < MIN_PRICE_CNY:
            continue
        print("      · 出发%s 返回%s 每人¥%s" % (dep or "?", ret or "?", price))
        if cheapest_any is None or price < cheapest_any[0]:
            cheapest_any = (price, dep, ret)
        in_window = (DEPART_MIN <= dep <= DEPART_MAX) and (RETURN_MIN <= ret <= RETURN_MAX)
        if in_window and (best is None or price < best[0]):
            best = (price, dep, ret)

    if best is not None:
        return best[0], "出发%s 返回%s（在你的出行窗口内）" % (best[1], best[2])
    if cheapest_any is not None:
        return None, "窗口内无数据；该月最低是 出发%s 返回%s 每人¥%s" % (
            cheapest_any[1], cheapest_any[2], cheapest_any[0])
    return None, "无有效价格"


def load_state():
    """读取上次已提醒的价格记录 {目标名: 上次提醒的最低价}。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        print("！保存状态失败:", e)


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

    state = load_state()
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

        prev = state.get(target_name)  # 上次已提醒的价（仅低于阈值时才记）
        if per_person < threshold:
            if prev is None or per_person < prev:
                # 首次跌破阈值，或刷新了更低价 → 才提醒
                alerts.append(
                    "<b>%s</b> 触发低价！<br>"
                    "每人约 <b>¥%s</b>（阈值 ¥%d）<br>"
                    "%s<br>"
                    "⚠️ 这是 Travelpayouts 缓存的近似价，请上携程/Aviasales 核对后再下单。"
                    % (target_name, per_person, threshold, desc)
                )
                print("      → 触发提醒（上次提醒价：%s）" % (prev if prev is not None else "无"))
            else:
                print("      → 已提醒过同等或更低价(¥%s)，本次不重复推送" % prev)
            state[target_name] = per_person if prev is None else min(prev, per_person)
        else:
            # 高于阈值 → 清掉记录，下次再跌破会重新提醒
            state.pop(target_name, None)

    save_state(state)

    if alerts:
        push_wechat("✈️ 机票低价提醒", "<br><br>".join(alerts))
        print("\n已发送提醒。")
    else:
        print("\n本轮没有触发提醒。")
    print("=== 机票监控结束 ===")


if __name__ == "__main__":
    main()
