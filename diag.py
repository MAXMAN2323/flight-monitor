#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性诊断：看清迈(CNX)/巴塞罗那(BCN)在 Travelpayouts 里到底有没有数据。
用曼谷(BKK)做对照。手动触发，不影响正式监控。"""

import os
import requests

TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()
H = {"X-Access-Token": TOKEN}


def call(name, url, params):
    params = dict(params)
    params["token"] = TOKEN
    try:
        r = requests.get(url, params=params, headers=H, timeout=30)
    except Exception as e:  # noqa: BLE001
        print("    %s -> 请求异常: %s" % (name, e))
        return
    if r.status_code != 200:
        print("    %s -> HTTP %s: %s" % (name, r.status_code, r.text[:100]))
        return
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        print("    %s -> 非JSON: %s" % (name, r.text[:100]))
        return
    data = body.get("data")
    # data 可能是 dict(按目的地/日期) 或 list
    if isinstance(data, dict):
        # 统计条目数：可能是 {dest:{...}} 或 {date:{...}}
        cnt = 0
        sample = None
        for v in data.values():
            if isinstance(v, dict) and "price" in v:
                cnt += 1
                sample = sample or v
            elif isinstance(v, dict):
                for vv in v.values():
                    cnt += 1
                    sample = sample or vv
        print("    %s -> success=%s 条目≈%d 例:%s" % (
            name, body.get("success"), cnt,
            ({k: sample.get(k) for k in ("price", "departure_at", "return_at")} if sample else "无")))
    elif isinstance(data, list):
        print("    %s -> success=%s 条目=%d 例:%s" % (
            name, body.get("success"), len(data),
            ({k: data[0].get(k) for k in ("price", "departure_at", "return_at")} if data else "无")))
    else:
        print("    %s -> success=%s 无data error=%s" % (name, body.get("success"), body.get("error")))


def probe(origin, dest):
    print("\n##### %s -> %s #####" % (origin, dest))
    call("A.cheap无日期", "https://api.travelpayouts.com/v1/prices/cheap",
         {"origin": origin, "destination": dest, "currency": "cny"})
    call("B.cheap按月", "https://api.travelpayouts.com/v1/prices/cheap",
         {"origin": origin, "destination": dest, "depart_date": "2026-09",
          "return_date": "2026-10", "currency": "cny"})
    call("C.calendar按月", "https://api.travelpayouts.com/v1/prices/calendar",
         {"origin": origin, "destination": dest, "depart_date": "2026-09",
          "return_date": "2026-10", "calendar_type": "departure_date", "currency": "cny"})
    call("D.单程cheap", "https://api.travelpayouts.com/v1/prices/cheap",
         {"origin": origin, "destination": dest, "depart_date": "2026-09", "currency": "cny"})
    call("E.v2最新价", "https://api.travelpayouts.com/v2/prices/latest",
         {"origin": origin, "destination": dest, "currency": "cny",
          "period_type": "year", "one_way": "false", "limit": 30, "page": 1,
          "show_to_affiliates": "true"})


def main():
    if not TOKEN:
        print("未设置 TRAVELPAYOUTS_TOKEN")
        return
    print("=== 诊断开始（BKK=曼谷对照, CNX=清迈, BCN=巴塞罗那）===")
    for dest in ["BKK", "CNX", "BCN"]:
        probe("BJS", dest)
    # 清迈/巴塞罗那再用首都机场代码单独试（看是不是城市码聚合的问题）
    print("\n--- 换出发机场 PEK 再试清迈/巴塞罗那 ---")
    for dest in ["CNX", "BCN"]:
        probe("PEK", dest)
    print("\n=== 诊断结束 ===")


if __name__ == "__main__":
    main()
