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
  5. 推送飞书：个人机器人每天固定发「余额日报」（随时可查）；大群仅在有告警时通报

Token 获取（access_token 仅 24h 有效，且 TikTok REST API 不发 refresh_token）：
  1) 优先从托管 OAuth Worker 拉取最新 token（TT_TOKEN_URL + TT_TOKEN_KEY）。
     该 Worker 把 OAuth 回调搬到云端，使你能在手机上完成重新授权，
     无需开着电脑（详见 worker/README.md）。
  2) 若配置了 TT_REFRESH_TOKEN（App 过审后才有），用它自动换新 token。
  3) 否则回退到 TT_ACCESS_TOKEN（Secret，24h 内有效）。
  4) 三者皆无 → 运行失败，向个人飞书推送带「手机可点授权链接」的重新授权提醒。

依赖环境变量（GitHub Secrets）：
  TT_APP_ID             Marketing API App ID
  TT_APP_SECRET         Marketing API App Secret
  TT_TOKEN_URL         托管 OAuth Worker 地址（如 https://xxx.workers.dev），PC 关机也可重授权
  TT_TOKEN_KEY         Worker 的 WORKER_KEY（与 Worker 端一致）
  TT_REFRESH_TOKEN     App 过审后 oauth 下发的 refresh_token（自动续期用，可选）
  TT_ACCESS_TOKEN       兜底用 access token（未配 refresh_token 或刷新失败时回退）
  FEISHU_BOT_WEBHOOK    飞书自定义机器人 Webhook URL（个人通知）
  FEISHU_GROUP_WEBHOOK  飞书大群机器人 Webhook URL（可选，配置后告警同时发大群）
"""
import json
import os
import secrets
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote

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


class TokenExpiredError(RuntimeError):
    """TikTok access_token 失效（40100/40101 等鉴权错误）。"""


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
                code = data.get("code")
                msg = data.get("message", "")
                # 鉴权失败（token 失效/无权限）→ 抛特定异常便于上层优雅降级
                if code in (40100, 40101) or "token" in str(msg).lower():
                    raise TokenExpiredError(f"API {path} 鉴权失败: code={code} msg={msg}")
                raise RuntimeError(f"API {path} 错误: code={code} msg={msg}")
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


def refresh_access_token(app_id: str, app_secret: str, refresh_token: str) -> str:
    """用 refresh_token（365 天有效）换取当日有效的 access_token。

    注意：refresh 接口要的是 refresh_token 字段，不是 access_token。
    """
    resp = requests.post(
        f"{BASE}/oauth2/refresh_token/",
        json={"app_id": app_id, "secret": app_secret,
              "refresh_token": refresh_token, "grant_type": "refresh_token"},
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


def build_daily_msg(results: list[dict], required_days: int, extra_days: int) -> dict:
    """每日余额播报（固定推送个人飞书，确保随时可查余额）。"""
    title = "📊 TikTok 每日余额播报"
    lines = [
        title,
        f"⏰ {datetime.now():%Y-%m-%d %H:%M}（北京时间）",
        f"口径：余额 < 近7天日均消耗 × {required_days} 天",
    ]
    if extra_days > 0:
        lines.append(f"🔔 明起 {extra_days} 天为周末/法定节假日，安全线已上浮提前预防")
    lines.append("━━━━━━━━━━━━━━━━━━")
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
    lines.append("✅ 本播报由余额监控每日自动发送（仅供参考）")
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
    fallback_token = os.environ.get("TT_ACCESS_TOKEN", "")
    refresh_token = os.environ.get("TT_REFRESH_TOKEN", "")
    token_url = os.environ.get("TT_TOKEN_URL", "")
    token_key = os.environ.get("TT_TOKEN_KEY", "")
    webhook = os.environ.get("FEISHU_BOT_WEBHOOK", "")
    group_webhook = os.environ.get("FEISHU_GROUP_WEBHOOK", "")

    # 0. 获取 token：优先级 = 托管 Worker 拉取（手机重授权）> 兜底 Secret
    token = ""
    if token_url and token_key:
        try:
            r = requests.get(
                f"{token_url.rstrip('/')}/token",
                params={"key": token_key},
                timeout=30,
            )
            r.raise_for_status()
            tok = r.json().get("token")
            if tok:
                token = tok
                log("已从托管 OAuth Worker 获取最新 access_token")
        except Exception as e:
            log(f"从托管地址获取 token 失败，继续尝试其他方式：{e}")

    if not token and fallback_token:
        token = fallback_token
        log("使用兜底 Secret TT_ACCESS_TOKEN")

    if not token and refresh_token and app_id and app_secret:
        try:
            token = refresh_access_token(app_id, app_secret, refresh_token)
            log("已用 TT_REFRESH_TOKEN 自动换取当日 access_token")
        except Exception as e:
            log(f"refresh_token 续期失败，回退到 TT_ACCESS_TOKEN：{e}")

    if not token:
        log("缺少可用 token（TT_ACCESS_TOKEN / TT_REFRESH_TOKEN / 托管地址均不可用）")
        return 1

    # 1. 计算"提前预防"安全天数：基准 14 天 + 距下一个可操作日的天数
    today = date.today()
    extra_days = days_until_next_operable(today)
    required_days = 14 + extra_days
    log(f"今天 {today}，距下一个可操作日 {extra_days} 天，安全天数 = {required_days}")
    if extra_days > 0:
        log(f"提示：{holiday_hint(extra_days)}")

    # 2. 监控账户列表
    accounts = DEFAULT_ACCOUNTS
    log(f"本次监控 {len(accounts)} 个账户: {[a['name'] for a in accounts]}")

    try:
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
    except TokenExpiredError as e:
        log(f"⚠️ {e}")
        log("access_token 已失效，向个人飞书推送重新授权提醒（含手机可点链接）")
        if webhook:
            auth_link = ""
            if app_id and token_url:
                redirect = f"{token_url.rstrip('/')}/callback"
                rid = secrets.token_hex(8)
                auth_link = (
                    "https://ads.tiktok.com/marketing_api/auth?app_id=" + app_id +
                    "&state=balance_monitor&redirect_uri=" + quote(redirect, safe="") +
                    "&rid=" + rid
                )
            text = (
                "🔑 TikTok access_token 已失效（约 24h 过期）\n"
                f"⏰ {datetime.now():%Y-%m-%d %H:%M}\n\n"
                "余额监控已暂停。请点击下方链接，在手机上登录授权即可恢复"
                "（无需开电脑，授权后自动存好新 token）：\n"
            )
            if auth_link:
                text += f"\n👉 {auth_link}\n"
            else:
                text += "\n（未配置 TT_TOKEN_URL，请联系管理员补充托管回调地址）\n"
            text += (
                "\n💡 彻底免手动：把 App 提交 TikTok 审核过审后授权会下发 "
                "refresh_token，约一年不用再管。"
            )
            send_feishu(webhook, {"msg_type": "text", "content": {"text": text}})
            log("已向个人飞书推送重新授权提醒（含授权链接）")
        else:
            log("未配置 FEISHU_BOT_WEBHOOK，跳过重新授权提醒")
        return 1

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

    # 6. 推送飞书：
    #    - 个人机器人：每天固定发送「余额日报」（无论是否告警，确保随时可查余额）
    #    - 大群机器人：仅在有告警时通报（避免日常刷屏）
    if webhook:
        try:
            send_feishu(webhook, build_daily_msg(results, required_days, extra_days))
            log("飞书每日余额播报已推送（个人）")
        except Exception as e:
            log(f"飞书每日播报推送失败（个人）：{e}")
    if group_webhook and summary["any_alert"]:
        try:
            send_feishu(group_webhook, build_feishu_msg(results, True, required_days, extra_days, group_mode=True))
            log("飞书告警已推送（大群）")
        except Exception as e:
            log(f"飞书告警推送失败（大群）：{e}")
    if not webhook and not group_webhook:
        log("未配置任何飞书 Webhook，跳过推送")
        return 1 if summary["any_alert"] else 0

    # 7. 告警状态下非零退出，便于 workflow 标记失败
    return 1 if summary["any_alert"] else 0


if __name__ == "__main__":
    sys.exit(main())
