#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TikTok 广告账户余额监控（US / MX 双店铺）
=========================================
功能：
  1. 通过 Business Center 查询旗下所有广告账户的当前余额（Billing）
  2. 查询每个账户近 7 天的实际花费，计算日均花费 = sum(spend_7d) / 7
  3. 安全余额线 = 日均花费 × 14（保证余额能覆盖两周投放）
  4. 当前余额 < 安全线 → 标记 ALERT
  5. 结果推送飞书自定义机器人（仅工作日由 GitHub Actions 触发）

依赖环境变量（GitHub Secrets）：
  TT_APP_ID         Marketing API App ID
  TT_APP_SECRET     Marketing API App Secret
  TT_ACCESS_TOKEN   当前有效 access token（24h；脚本会自动用 app_id/secret 刷新）
  TT_BC_ID          Business Center ID，默认 7317194879292014593
  FEISHU_WEBHOOK    飞书自定义机器人 Webhook URL
可选：
  TT_ADVERTISER_NAMES  广告账户名称映射，格式 "id1=名称1,id2=名称2"（用于展示）
"""
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests

BASE = "https://business-api.tiktok.com/open_api/v1.3"
BC_ID = os.environ.get("TT_BC_ID", "7317194879292014593")

# 已知账户名称映射（可追加，未知账户显示原始 advertiser_id）
NAME_MAP = {}
_raw = os.environ.get("TT_ADVERTISER_NAMES", "").strip()
if _raw:
    for pair in _raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            NAME_MAP[k.strip()] = v.strip()


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def api_get(path: str, params: dict, token: str) -> dict:
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


def get_balances(token: str) -> list[dict]:
    """Business Center 下所有广告账户余额。"""
    data = api_get("/advertiser/balance/get/", {"bc_id": BC_ID}, token)
    return data.get("list", [])


def get_7d_spend(advertiser_id: str, token: str) -> float:
    """近 7 天（含今天）实际花费之和。"""
    end = date.today()
    start = end - timedelta(days=6)
    data = api_get(
        "/report/integrated/get/",
        {
            "advertiser_id": advertiser_id,
            "report_type": "BASIC",
            "dimensions": json.dumps(["advertiser_id", "stat_time_day"]),
            "metrics": json.dumps(["spend"]),
            "data_level": "AUCTION_ADVERTISER",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "page_size": 100,
        },
        token,
    )
    total = 0.0
    for row in data.get("list", []):
        try:
            total += float(row.get("metrics", {}).get("spend", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def build_feishu_msg(results: list[dict], any_alert: bool) -> dict:
    lines = [
        "📊 TikTok 广告账户余额日报",
        f"⏰ {datetime.now():%Y-%m-%d %H:%M}（北京时间）",
        "━━━━━━━━━━━━━━━━",
    ]
    for r in results:
        flag = "⚠️ 余额不足" if r["alert"] else "✅ 余额充足"
        lines.append(
            f"▍{r['name']}（{r['currency']}）\n"
            f"  当前余额：{r['balance']:,.2f}\n"
            f"  近7天日均花费：{r['avg_daily_spend']:,.2f}\n"
            f"  安全余额线（×14）：{r['safety_line']:,.2f}\n"
            f"  {flag}"
        )
        lines.append("────")
    if any_alert:
        lines.append("💡 请及时充值，避免账户因余额不足而停投！")
    return {
        "msg_type": "text",
        "content": {"text": "\n".join(lines)},
    }


def main() -> int:
    app_id = os.environ.get("TT_APP_ID", "")
    app_secret = os.environ.get("TT_APP_SECRET", "")
    token = os.environ.get("TT_ACCESS_TOKEN", "")
    webhook = os.environ.get("FEISHU_WEBHOOK", "")

    if not token:
        log("缺少 TT_ACCESS_TOKEN")
        return 1

    # 1. 刷新 token（长期运行必需）
    if app_id and app_secret:
        try:
            token = refresh_token(app_id, app_secret, token)
            log("access token 已刷新")
        except Exception as e:
            log(f"token 刷新失败，继续用现有 token：{e}")

    # 2. 查询余额
    balances = get_balances(token)
    log(f"获取到 {len(balances)} 个广告账户余额")

    results = []
    for acct in balances:
        adv_id = str(acct.get("advertiser_id", ""))
        if not adv_id:
            continue
        balance = float(acct.get("balance", 0) or 0)
        currency = acct.get("currency", "?")
        name = NAME_MAP.get(adv_id, f"广告账户 {adv_id}")

        # 3. 近 7 天花费
        try:
            spend_7d = get_7d_spend(adv_id, token)
        except Exception as e:
            log(f"账户 {adv_id} 花费查询失败：{e}")
            spend_7d = 0.0
        avg_daily = spend_7d / 7.0
        safety_line = avg_daily * 14.0
        alert = avg_daily > 0 and balance < safety_line

        results.append(
            {
                "advertiser_id": adv_id,
                "name": name,
                "balance": balance,
                "currency": currency,
                "spend_7d": spend_7d,
                "avg_daily_spend": avg_daily,
                "safety_line": safety_line,
                "alert": alert,
            }
        )
        status = "⚠️ ALERT" if alert else "OK"
        log(f"{name}: balance={balance:.2f} {currency}, 7d_spend={spend_7d:.2f}, "
            f"avg_daily={avg_daily:.2f}, safety_line={safety_line:.2f} → {status}")

    # 4. 汇总输出
    summary = {
        "date": date.today().isoformat(),
        "bc_id": BC_ID,
        "results": results,
        "any_alert": any(r["alert"] for r in results),
    }
    with open("balance_report.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    log(f"报告已写入 balance_report.json，共 {len(results)} 个账户")

    # 5. 推送飞书
    if not webhook:
        log("未配置 FEISHU_WEBHOOK，跳过推送")
        return 1 if summary["any_alert"] else 0

    msg = build_feishu_msg(results, summary["any_alert"])
    resp = requests.post(webhook, json=msg, timeout=30)
    resp.raise_for_status()
    fb = resp.json()
    if fb.get("code") != 0:
        log(f"飞书推送失败: {fb}")
        return 1
    log("飞书推送成功")

    # 6. 告警状态下非零退出，便于 workflow 标记失败
    return 1 if summary["any_alert"] else 0


if __name__ == "__main__":
    sys.exit(main())
