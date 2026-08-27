#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TikTok 广告账户余额监控（US / MX 双店铺）
=========================================
功能：
  1. 查询指定广告账户的当前余额（通过 advertiser/info，无需 BC finance role）
  2. 查询每个账户近 7 天的实际花费，计算日均花费 = sum(spend_7d) / 7
  3. 安全余额线 = 日均花费 × 安全天数（基准 14 天）
     - 周末 / 法定节假日前夕自动上浮：安全天数 = 14 + 距下一个可操作日的天数
     （例如周五按 16 天算；国庆前最后工作日按 21 天算，提前预防假期断粮）
  4. 当前余额 < 安全线 → 标记 ALERT
  5. 结果推送飞书机器人：个人（FEISHU_BOT_WEBHOOK）+ 大群（FEISHU_GROUP_WEBHOOK，可选）

依赖环境变量（GitHub Secrets）：
  TT_APP_ID             Marketing API App ID
  TT_APP_SECRET         Marketing API App Secret
  TT_ACCESS_TOKEN       当前有效 access token（24h；脚本会自动用 app_id/secret 刷新）
  FEISHU_BOT_WEBHOOK    飞书自定义机器人 Webhook URL（个人通知）
  FEISHU_GROUP_WEBHOOK  飞书大群机器人 Webhook URL（可选，配置后告警同时发大群）
"""
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests

BASE = "https://business-api.tiktok.com/open_api/v1.3"

# ---------------------------------------------------------------------------
# 2026 年中国法定节假日 & 调休上班日（国务院办公厅通知，国办发明电〔2025〕7号）
HOLIDAYS_2026 = {
    # 元旦：1/1-1/3
    date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3),
    # 春节：2/15-2/23
    date(2026, 2, 15), date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 21), date(2026, 2, 22),
    date(2026, 2, 23),
    # 清明：4/4-4/6
    date(2026, 4, 4), date(2026, 4, 5), date(2026, 4, 6),
    # 劳动节：5/1-5/5
    date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3), date(2026, 5, 4),
    date(2026, 5, 5),
    # 端午：6/19-6/21
    date(2026, 6, 19), date(2026, 6, 20), date(2026, 6, 21),
    # 中秋：9/25-9/27
    date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27),
    # 国庆：10/1-10/7
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 4),
    date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7),
}
# 调休上班的周末（这些日子虽然是周六/周日，但可以正常操作）
WORKDAY_OVERRIDES_2026 = {
    date(2026, 1, 4),   # 元旦调休
    date(2026, 2, 14), date(2026, 2, 28),  # 春节调休
    date(2026, 5, 9),   # 劳动节调休
    date(2026, 9, 20),  # 国庆调休
    date(2026, 10, 10), # 国庆调休
}


def is_operable_day(d: date) -> bool:
    """是否为可操作日（可充值/可处理告警）：调休上班日 > 节假日 > 正常周末。"""
    if d in WORKDAY_OVERRIDES_2026:
        return True
    if d in HOLIDAYS_2026:
        return False
    return d.weekday() < 5  # 周一~周五


def days_until_next_operable(today: date) -> int:
    """从明天起到下一个可操作日之间，连续不可操作的天数（0 = 明天就可操作）。"""
    n, d = 0, today + timedelta(days=1)
    while not is_operable_day(d):
        n += 1
        d += timedelta(days=1)
        if n > 60:  # 日历缺失保护
            break
    return n


def holiday_hint(extra_days: int) -> str:
    """提前预防提示文案。"""
    if extra_days <= 0:
        return ""
    return f"（接下来 {extra_days} 天为周末/节假日，安全线已上浮提前预防）"

# 监控账户：GMV Max 投放主体（消耗走 /gmv_max/report/get/，必须带 store_id）
# 注意：普通 /report/integrated/get/ 查不到 GMV Max 消耗（恒为 0）
DEFAULT_ACCOUNTS = [
    {"advertiser_id": "7493492086189113361", "store_id": "7495352939804002843", "name": "US-DrBioCare"},
    {"advertiser_id": "7650062062028439570", "store_id": "7494687738309805944", "name": "MX-BioCare"},
]


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def api_get(path: str, params: dict, token: str, tries: int = 4) -> dict:
    last_err = None
    for i in range(tries):
        try:
            resp = requests.get(
                f"{BASE}{path}",
                params=params,
                headers={"Access-Token": token},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"API {path} 错误: code={data.get('code')} msg={data.get('message')}")
            return data.get("data", {})
        except RuntimeError:
            raise
        except Exception as e:
            last_err = e
            log(f"网络错误（第 {i+1}/{tries} 次重试）: {type(e).__name__}")
            time.sleep(3)
    raise last_err


def api_get_all(path: str, params: dict, token: str) -> list[dict]:
    """分页拉取全部行。"""
    page, rows = 1, []
    while True:
        p = dict(params)
        p["page"] = page
        data = api_get(path, p, token)
        batch = data.get("list", [])
        rows.extend(batch)
        total_page = data.get("page_info", {}).get("total_page", 1) or 1
        if page >= total_page or not batch:
            return rows
        page += 1


def refresh_token(app_id: str, app_secret: str, access_token: str) -> str:
    """用 app_id/secret 刷新 24h access token。"""
    resp = requests.post(
        f"{BASE}/oauth2/access_token/refresh/",
        json={"app_id": app_id, "secret": app_secret, "access_token": access_token},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"token 刷新失败: code={data.get('code')} msg={data.get('message')}"
        )
    return data["data"]["access_token"]


def get_advertiser_info(advertiser_ids: list[str], token: str) -> list[dict]:
    """批量查询广告账户信息（含 balance 字段）。"""
    data = api_get(
        "/advertiser/info/",
        {
            "advertiser_ids": json.dumps(advertiser_ids),
            "fields": json.dumps(["advertiser_id", "name", "balance", "currency", "status"]),
        },
        token,
    )
    return data.get("list", [])


def get_7d_spend(advertiser_id: str, store_id: str, token: str) -> float:
    """近 7 个完整天（截至昨日）的 GMV Max 消耗之和。

    必须用 /gmv_max/report/get/（带 store_ids）；
    普通 /report/integrated/get/ 对 GMV Max-only 账户恒返回 0。
    """
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=6)
    rows = api_get_all(
        "/gmv_max/report/get/",
        {
            "advertiser_id": advertiser_id,
            "store_ids": json.dumps([store_id]),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "metrics": json.dumps(["cost"]),
            "dimensions": json.dumps(["campaign_id", "stat_time_day"]),
            "page_size": 1000,
        },
        token,
    )
    total = 0.0
    for row in rows:
        try:
            total += float(row.get("metrics", {}).get("cost", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def build_feishu_msg(results: list[dict], any_alert: bool, required_days: int,
                     extra_days: int, group_mode: bool = False) -> dict:
    title = "🚨 TikTok 广告账户余额告警" if not group_mode else "🚨 TikTok 广告余额告警（大群通报）"
    lines = [
        title,
        f"⏰ {datetime.now():%Y-%m-%d %H:%M}（北京时间）",
        f"口径：余额 < 近7天日均消耗 × {required_days} 天",
    ]
    if extra_days > 0:
        lines.append(f"🔔 明起 {extra_days} 天为周末/法定节假日，无法充值，安全线已上浮提前预防！")
    lines.append("━━━━━━━━━━━━━━━━")
    for r in results:
        flag = "⚠️ 余额不足" if r["alert"] else "✅ 余额充足"
        seg = (
            f"▍{r['name']}（{r['currency']}）\n"
            f"  当前余额：{r['balance']:,.2f}\n"
            f"  近7天日均消耗：{r['avg_daily_spend']:,.2f}\n"
            f"  安全线（{required_days}天）：{r['safety_line']:,.2f}\n"
            f"  {flag}"
        )
        if r["alert"]:
            seg += f"\n  可撑约 {r['days_left']:.1f} 天，需充值约 {r['topup']:,.0f} 才够 {required_days} 天"
        lines.append(seg)
        lines.append("────")
    if any_alert:
        lines.append("💡 请及时充值，避免账户因余额不足而停投！")
    return {
        "msg_type": "text",
        "content": {"text": "\n".join(lines)},
    }


def send_feishu(webhook: str, msg: dict) -> None:
    resp = requests.post(webhook, json=msg, timeout=30)
    resp.raise_for_status()
    fb = resp.json()
    if fb.get("code") != 0:
        raise RuntimeError(f"飞书推送失败: {fb}")


def main() -> int:
    app_id = os.environ.get("TT_APP_ID", "")
    app_secret = os.environ.get("TT_APP_SECRET", "")
    token = os.environ.get("TT_ACCESS_TOKEN", "")
    webhook = os.environ.get("FEISHU_BOT_WEBHOOK", "")
    group_webhook = os.environ.get("FEISHU_GROUP_WEBHOOK", "")

    if not token:
        log("缺少 TT_ACCESS_TOKEN")
        return 1

    # 0. 计算"提前预防"安全天数：基准 14 天 + 距下一个可操作日的天数
    today = date.today()
    extra_days = days_until_next_operable(today)
    required_days = 14 + extra_days
    log(f"今天 {today}，距下一个可操作日 {extra_days} 天，安全天数 = {required_days}")
    if extra_days > 0:
        log(f"提示：{holiday_hint(extra_days)}")

    # 1. 刷新 token（长期运行必需）
    if app_id and app_secret:
        try:
            token = refresh_token(app_id, app_secret, token)
            log("access token 已刷新")
        except Exception as e:
            log(f"token 刷新失败，继续用现有 token：{e}")

    # 2. 监控账户列表
    accounts = DEFAULT_ACCOUNTS
    log(f"本次监控 {len(accounts)} 个账户: {[a['name'] for a in accounts]}")

    # 3. 批量查余额
    advertiser_ids = [a["advertiser_id"] for a in accounts]
    info_list = get_advertiser_info(advertiser_ids, token)
    info_map = {str(a.get("advertiser_id")): a for a in info_list}

    results = []
    for acc in accounts:
        adv_id = acc["advertiser_id"]
        info = info_map.get(adv_id, {})
        if not info:
            log(f"未获取到账户 {adv_id} 的信息，跳过")
            continue
        balance = float(info.get("balance", 0) or 0)
        currency = info.get("currency", "?")
        name = acc["name"]

        # 4. 近 7 个完整天 GMV Max 消耗
        try:
            spend_7d = get_7d_spend(adv_id, acc["store_id"], token)
        except Exception as e:
            log(f"账户 {adv_id} 消耗查询失败：{e}")
            spend_7d = 0.0
        avg_daily = spend_7d / 7.0
        safety_line = avg_daily * float(required_days)
        alert = avg_daily > 0 and balance < safety_line
        days_left = balance / avg_daily if avg_daily > 0 else float("inf")
        topup = max(0.0, safety_line - balance)

        results.append(
            {
                "advertiser_id": adv_id,
                "name": name,
                "balance": balance,
                "currency": currency,
                "spend_7d": spend_7d,
                "avg_daily_spend": avg_daily,
                "safety_line": safety_line,
                "days_left": days_left,
                "topup": topup,
                "alert": alert,
            }
        )
        status = "⚠️ ALERT" if alert else "OK"
        log(f"{name}: balance={balance:.2f} {currency}, 7d_spend={spend_7d:.2f}, "
            f"avg_daily={avg_daily:.2f}, safety_line={safety_line:.2f}, "
            f"days_left={days_left:.1f} → {status}")

    # 5. 汇总输出
    summary = {
        "date": date.today().isoformat(),
        "required_days": required_days,
        "extra_days": extra_days,
        "advertiser_ids": advertiser_ids,
        "results": results,
        "any_alert": any(r["alert"] for r in results),
    }
    with open("balance_report.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    log(f"报告已写入 balance_report.json，共 {len(results)} 个账户")

    # 6. 仅在有告警时推送飞书（用户要求：余额不足才提醒）
    #    同时推送：个人机器人 + 大群机器人（若配置 FEISHU_GROUP_WEBHOOK）
    targets = []
    if webhook:
        targets.append(("个人", webhook, False))
    if group_webhook:
        targets.append(("大群", group_webhook, True))
    if not targets:
        log("未配置任何飞书 Webhook，跳过推送")
        return 1 if summary["any_alert"] else 0

    if summary["any_alert"]:
        ok = True
        for label, url, gm in targets:
            try:
                send_feishu(url, build_feishu_msg(results, True, required_days, extra_days, group_mode=gm))
                log(f"飞书告警推送成功（{label}）")
            except Exception as e:
                log(f"飞书推送失败（{label}）：{e}")
                ok = False
        if not ok:
            return 1
    else:
        log("余额充足，不发送飞书消息")

    # 7. 告警状态下非零退出，便于 workflow 标记失败
    return 1 if summary["any_alert"] else 0


if __name__ == "__main__":
    sys.exit(main())
