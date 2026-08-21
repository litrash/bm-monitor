# -*- coding: utf-8 -*-
"""
考生之家(bm.e21cn.com) 报名信息监控程序

每天定时抓取首页，把所有报名项目推送到微信，并标注哪些是新增、状态变化、即将结束。

用法:
  python bm_monitor.py              # 本地循环监控
  python bm_monitor.py --once       # 只抓取一次
  python bm_monitor.py --test-notify # 测试推送
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

DEFAULT_STATE = "bm_monitor_state.json"
DEFAULT_LOG = "bm_monitor.log"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

log = logging.getLogger("bm_monitor")
IS_CI = os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _env(v):
    return os.environ.get(v, "")


def load_config():
    cfg = {
        "url": _env("BM_URL") or "https://bm.e21cn.com/",
        "state_file": DEFAULT_STATE,
        "log_file": DEFAULT_LOG,
        "alert_when_ending_within_hours": int(_env("BM_ENDING_HOURS") or "2"),
        "keyword_filter": [x.strip() for x in _env("BM_KEYWORDS").split(",") if x.strip()],
        "area_filter": [x.strip() for x in _env("BM_AREAS").split(",") if x.strip()],
        "sc_sendkey": _env("BM_SC_SENDKEY"),
        "tg_bot_token": _env("BM_TG_BOT_TOKEN"),
        "tg_chat_id": _env("BM_TG_CHAT_ID"),
    }
    return cfg


# --------------------------------------------------------------------------- #
# 抓取与解析
# --------------------------------------------------------------------------- #
def fetch_html(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            if i == retries - 1:
                raise
            log.warning("第%d次抓取失败: %s, %d秒后重试...", i + 1, e, (i + 1) * 5)
            time.sleep((i + 1) * 5)


def _clean(s):
    return " ".join((s or "").split())


def _parse_remaining(s):
    s = _clean(s)
    units = {"天": 24 * 60, "小时": 60, "分钟": 1, "分": 1}
    for unit, mul in units.items():
        if unit in s:
            num = "".join(ch for ch in s if ch.isdigit() or ch == ".")
            try:
                return int(float(num) * mul)
            except ValueError:
                return None
    return None


def parse_entries(html):
    soup = BeautifulSoup(html, "lxml")
    active, closed = [], []

    for li in soup.select("#div_EntryLists li.li_arealists"):
        name_el = li.select_one("label.area_lists_entryname a")
        time_el = li.select_one("label.area_lists_entrytime")
        date_el = li.select_one("label.area_lists_entrydate")
        pay_el = li.select_one("label.area_lists_paydate")
        if not name_el:
            continue
        name = _clean(name_el.get_text())
        external_url = name_el.get("href", "")

        area = ""
        parent_ul = li.find_parent("ul")
        if parent_ul:
            prev = parent_ul.find_previous_sibling("ul")
            if prev and prev.get("id"):
                area = _clean(prev["id"])

        start = end = pay_start = pay_end = ""
        if date_el:
            labels = date_el.find_all("label")
            if len(labels) >= 2:
                start = _clean(labels[0].get_text())
                end = _clean(labels[1].get_text())
        if pay_el:
            labels = pay_el.find_all("label")
            if len(labels) >= 2:
                pay_start = _clean(labels[0].get_text())
                pay_end = _clean(labels[1].get_text())

        phase = "ongoing"
        remaining = None
        if time_el:
            bs = time_el.find_all("b")
            if bs:
                txt = _clean(bs[0].get_text())
                phase = "upcoming" if "开始" in txt else "ongoing"
            if len(bs) >= 2:
                remaining = _parse_remaining(bs[1].get_text())

        signup_url = ""
        for a in li.select("a"):
            href = a.get("href", "")
            if "checkRE" in href or "去报名" in a.get_text():
                signup_url = href

        key = f"{area}|{name}|{start}|{end}"
        active.append({
            "key": key, "area": area, "name": name,
            "start": start, "end": end,
            "pay_start": pay_start, "pay_end": pay_end,
            "phase": phase, "remaining": remaining,
            "signup_url": signup_url, "external_url": external_url,
        })

    for li in soup.select("#div_EntryLists_Closed li.li_Closed"):
        a = li.select_one("a")
        if not a:
            continue
        closed.append({"name": _clean(a.get_text()), "external_url": a.get("href", "")})

    return active, closed


# --------------------------------------------------------------------------- #
# 过滤
# --------------------------------------------------------------------------- #
def filter_entry(e, cfg):
    kws = cfg.get("keyword_filter") or []
    areas = cfg.get("area_filter") or []
    if kws and not any(k in e["name"] for k in kws):
        return False
    if areas and e["area"] not in areas:
        return False
    return True


# --------------------------------------------------------------------------- #
# 构建日报消息
# --------------------------------------------------------------------------- #
def _fmt_time(remaining):
    """分钟数 -> 可读字符串"""
    if remaining is None:
        return "?"
    if remaining < 60:
        return f"{remaining}分钟"
    if remaining < 24 * 60:
        h = remaining // 60
        m = remaining % 60
        return f"{h}小时{m}分钟" if m else f"{h}小时"
    d = remaining // (24 * 60)
    h = (remaining % (24 * 60)) // 60
    return f"{d}天{h}小时" if h else f"{d}天"


def build_daily_report(curr_active, curr_closed, prev_state, cfg):
    """生成每日推送消息，标注变化。"""
    prev_entries = {e["key"]: e for e in prev_state.get("entries", [])}
    prev_closed_names = {c["name"] for c in prev_state.get("closed", [])}
    curr_keys = {e["key"] for e in curr_active}
    curr_closed_names = {c["name"] for c in curr_closed}

    is_first = not prev_entries
    lines = []
    ending_soon = []
    new_count = 0
    changed_count = 0

    # 分类每个当前条目
    for e in curr_active:
        if not filter_entry(e, cfg):
            continue
        key = e["key"]
        prev = prev_entries.get(key)

        # 确定标签
        if is_first or key not in prev_entries:
            tag = "🆕"
            new_count += 1
        elif prev["phase"] == "upcoming" and e["phase"] == "ongoing":
            tag = "▶️ 报名开始"
            changed_count += 1
        else:
            tag = ""

        # 状态描述
        if e["phase"] == "upcoming":
            status = f"⏳ 距开始 {_fmt_time(e['remaining'])}"
        else:
            status = f"🔴 报名中 · 距结束 {_fmt_time(e['remaining'])}"

        line = f"{tag}【{e['area']}】{e['name']}"
        if tag:
            line += f"\n    {status}"
        line += f"\n    📅 {e['start']} ~ {e['end']}"
        if e["pay_start"]:
            line += f"  |  💰 缴费 {e['pay_start']} ~ {e['pay_end']}"
        lines.append((e, line, tag))

        # 即将结束提醒
        thresh = int(cfg.get("alert_when_ending_within_hours", 2) or 0) * 60
        if thresh and e["phase"] == "ongoing" and e["remaining"] and e["remaining"] <= thresh:
            prev_rem = prev.get("remaining") if prev else None
            if not prev_rem or prev_rem > thresh:
                ending_soon.append(e)

    # 检测已下线的
    removed = []
    for key, p in prev_entries.items():
        if not filter_entry(p, cfg):
            continue
        if key not in curr_keys:
            if p["name"] in curr_closed_names:
                removed.append(f"🔚 已结束：【{p['area']}】{p['name']}")
            else:
                removed.append(f"❌ 已下线：【{p['area']}】{p['name']}")

    # 组装消息
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg_lines = [f"📋 考生之家报名日报", f"更新时间：{now}", ""]

    # 统计摘要
    msg_lines.append(f"📊 当前共 {len(lines)} 个项目")
    if new_count:
        msg_lines.append(f"   🆕 新增 {new_count} 个")
    if changed_count:
        msg_lines.append(f"   ▶️ 状态变化 {changed_count} 个")
    if removed:
        msg_lines.append(f"   🔚 已结束/下线 {len(removed)} 个")
    msg_lines.append("")

    # 项目列表
    msg_lines.append("━" * 20)
    for e, line, tag in lines:
        msg_lines.append(line)
        msg_lines.append("")

    # 即将结束提醒
    if ending_soon:
        msg_lines.append("━" * 20)
        msg_lines.append("⚠️ 即将截止：")
        for e in ending_soon:
            msg_lines.append(f"   【{e['area']}】{e['name']} — 还剩 {_fmt_time(e['remaining'])}")

    # 已下线
    if removed:
        msg_lines.append("━" * 20)
        for r in removed:
            msg_lines.append(r)

    # 已结束列表
    if curr_closed:
        msg_lines.append("━" * 20)
        msg_lines.append("📁 近期已结束的报名：")
        for c in curr_closed[:5]:
            msg_lines.append(f"   · {c['name']}")

    msg_lines.append("")
    msg_lines.append("— 考生之家监控 | litrash/bm-monitor")

    return "\n".join(msg_lines), bool(new_count or changed_count or removed or ending_soon)


# --------------------------------------------------------------------------- #
# 推送
# --------------------------------------------------------------------------- #
def send_serverchan(sendkey, title, msg):
    if not sendkey:
        return False
    try:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        r = requests.post(url, data={"title": title, "desp": msg}, timeout=15)
        data = r.json()
        if data.get("code") == 0:
            log.info("Server酱推送成功")
            return True
        log.warning("Server酱返回: %s", data)
        return False
    except Exception as e:
        log.warning("Server酱发送失败: %s", e)
        return False


def send_telegram(token, chat_id, title, msg):
    if not token or not chat_id:
        return False
    try:
        # 用 HTML 模式避免 Markdown 特殊字符问题
        text = f"<b>{title}</b>\n\n{msg}"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
        if r.json().get("ok"):
            log.info("Telegram 推送成功")
            return True
        log.warning("Telegram 返回: %s", r.json())
        return False
    except Exception as e:
        log.warning("Telegram 发送失败: %s", e)
        return False


# --------------------------------------------------------------------------- #
# 状态持久化
# --------------------------------------------------------------------------- #
def load_state(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"entries": [], "closed": []}


def save_state(path, active, closed):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "entries": active,
            "closed": closed,
        }, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_once(cfg, force_send=False):
    html = fetch_html(cfg["url"])
    active, closed = parse_entries(html)
    log.info("抓取到 %d 条进行中/即将开始, %d 条已结束", len(active), len(closed))

    prev_state = load_state(cfg["state_file"])
    report, has_changes = build_daily_report(active, closed, prev_state, cfg)
    save_state(cfg["state_file"], active, closed)

    is_first = not prev_state.get("entries")

    title = "考生之家报名日报"
    if has_changes:
        title += " [有变化]"

    # Telegram 每次都推送（主通道）
    send_telegram(cfg["tg_bot_token"], cfg["tg_chat_id"], title, report)

    if is_first:
        log.info("首次运行，建立基线")
    elif has_changes or force_send:
        # Server酱作为备用
        send_serverchan(cfg["sc_sendkey"], title, report)
    else:
        log.info("无变化，已通过 Telegram 推送日报")

    log.info("\n%s", report)
    return active, closed


def main():
    ap = argparse.ArgumentParser(description="考生之家报名监控")
    ap.add_argument("--once", action="store_true", help="只运行一次")
    ap.add_argument("--force", action="store_true", help="强制发送推送")
    ap.add_argument("--test-notify", action="store_true", help="测试推送")
    args = ap.parse_args()

    cfg = load_config()

    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(cfg["log_file"], encoding="utf-8")],
    )

    if args.test_notify:
        log.info("测试推送...")
        ok = send_serverchan(cfg["sc_sendkey"],
                             "🧪 考生之家监控 - 测试",
                             "推送通道正常！如果你看到这条消息，说明配置成功。\n\n— 考生之家监控 | litrash/bm-monitor")
        log.info("测试%s", "完成" if ok else "失败")
        return

    if args.once or args.force:
        run_once(cfg, force_send=args.force)
        return

    # 本地循环模式
    interval = max(1, int(cfg.get("interval_minutes", 15) or 15))
    log.info("开始监控, 每 %d 分钟检查一次", interval)
    first = True
    while True:
        try:
            run_once(cfg, force_send=not first)
            first = False
        except KeyboardInterrupt:
            log.info("已退出")
            break
        except Exception as e:
            log.error("出错: %s", e, exc_info=True)
        try:
            time.sleep(interval * 60)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()