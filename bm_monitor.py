# -*- coding: utf-8 -*-
"""
考生之家(bm.e21cn.com) 报名信息监控程序

功能:
  * 定时抓取首页 HTML, 解析"正在进行/即将开始"与"已结束"的报名项目
  * 检测变化: 新增报名、报名开始(未开始->报名中)、报名即将结束、报名结束/下线
  * 多通道通知: Windows 桌面弹窗 / Server酱(微信) / 邮件 / Telegram Bot
  * 首次运行记录基线, 不触发通知; 之后每次运行对比上一份快照
  * 支持本地常驻与 GitHub Actions 定时运行

用法:
  python bm_monitor.py              # 按配置的间隔循环监控
  python bm_monitor.py --once       # 只抓取一次(测试解析/基线)
  python bm_monitor.py --test-notify # 测试通知通道
  python bm_monitor.py --config path/to/config.json

环境变量覆盖 (GitHub Actions 等 CI 场景):
  BM_URL          - 监控 URL
  BM_KEYWORDS     - 逗号分隔的关键词过滤
  BM_AREAS        - 逗号分隔的地区过滤
  BM_ENDING_HOURS - 即将结束提醒阈值(小时)
  BM_SC_SENDKEY   - Server酱 SendKey
  BM_TG_BOT_TOKEN - Telegram Bot Token
  BM_TG_CHAT_ID   - Telegram Chat ID
  BM_EMAIL_HOST   - SMTP 服务器
  BM_EMAIL_PORT   - SMTP 端口
  BM_EMAIL_USER   - SMTP 用户名
  BM_EMAIL_PASS   - SMTP 密码
  BM_EMAIL_FROM   - 发件人
  BM_EMAIL_TO     - 收件人
"""

import argparse
import json
import logging
import os
import smtplib
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import requests
from bs4 import BeautifulSoup

DEFAULT_CONFIG = "config.json"
DEFAULT_STATE = "bm_monitor_state.json"
DEFAULT_LOG = "bm_monitor.log"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

log = logging.getLogger("bm_monitor")

# 是否在 CI 环境 (GitHub Actions / 无桌面)
IS_CI = os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"


# --------------------------------------------------------------------------- #
# 配置加载
# --------------------------------------------------------------------------- #
def _env(varname, default=""):
    val = os.environ.get(varname, "")
    return val if val else default


def _env_list(varname):
    val = os.environ.get(varname, "")
    return [x.strip() for x in val.split(",") if x.strip()]


def load_config(path):
    """加载 JSON 配置 + 环境变量覆盖。"""
    defaults = {
        "url": "https://bm.e21cn.com/",
        "interval_minutes": 15,
        "state_file": DEFAULT_STATE,
        "log_file": DEFAULT_LOG,
        "alert_when_ending_within_hours": 2,
        "keyword_filter": [],
        "area_filter": [],
        "notify": {
            "windows_toast": not IS_CI,
            "serverchan": {"enabled": False, "sendkey": ""},
            "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
            "email": {
                "enabled": False, "smtp_host": "", "smtp_port": 465,
                "use_ssl": True, "username": "", "password": "",
                "from": "", "to": "",
            },
        },
    }
    cfg = dict(defaults)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        cfg.update(user)
        for k, v in defaults["notify"].items():
            if isinstance(v, dict) and isinstance(cfg["notify"].get(k), dict):
                merged = dict(v)
                merged.update(cfg["notify"][k])
                cfg["notify"][k] = merged

    # ---- 环境变量覆盖 ----
    if _env("BM_URL"):
        cfg["url"] = _env("BM_URL")
    if _env("BM_KEYWORDS"):
        cfg["keyword_filter"] = _env_list("BM_KEYWORDS")
    if _env("BM_AREAS"):
        cfg["area_filter"] = _env_list("BM_AREAS")
    if _env("BM_ENDING_HOURS"):
        cfg["alert_when_ending_within_hours"] = int(_env("BM_ENDING_HOURS"))

    # Server酱
    if _env("BM_SC_SENDKEY"):
        cfg["notify"]["serverchan"]["enabled"] = True
        cfg["notify"]["serverchan"]["sendkey"] = _env("BM_SC_SENDKEY")

    # Telegram
    if _env("BM_TG_BOT_TOKEN"):
        cfg["notify"]["telegram"]["enabled"] = True
        cfg["notify"]["telegram"]["bot_token"] = _env("BM_TG_BOT_TOKEN")
        cfg["notify"]["telegram"]["chat_id"] = _env("BM_TG_CHAT_ID")

    # Email
    if _env("BM_EMAIL_HOST"):
        cfg["notify"]["email"]["enabled"] = True
        cfg["notify"]["email"]["smtp_host"] = _env("BM_EMAIL_HOST")
        cfg["notify"]["email"]["smtp_port"] = int(_env("BM_EMAIL_PORT", "465"))
        cfg["notify"]["email"]["username"] = _env("BM_EMAIL_USER")
        cfg["notify"]["email"]["password"] = _env("BM_EMAIL_PASS")
        cfg["notify"]["email"]["from"] = _env("BM_EMAIL_FROM")
        cfg["notify"]["email"]["to"] = _env("BM_EMAIL_TO")

    return cfg


# --------------------------------------------------------------------------- #
# 抓取与解析
# --------------------------------------------------------------------------- #
def fetch_html(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _clean(s):
    return " ".join((s or "").split())


def _parse_remaining(s):
    """'2小时'/'17小时'/'3天'/'30分钟' -> 分钟数(int); 无法解析返回 None。"""
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
    """解析首页, 返回 (active_entries, closed_entries)。"""
    soup = BeautifulSoup(html, "lxml")
    active, closed = [], []

    # ---- 正在进行及7天内即将开始 ----
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
        if parent_ul is not None:
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

        signup_url = info_url = ""
        for a in li.select("a"):
            href = a.get("href", "")
            if "checkRE" in href or "去报名" in a.get_text():
                signup_url = href
            if "user" in href and "打印" in a.get_text():
                info_url = href

        key = f"{area}|{name}|{start}|{end}"
        active.append({
            "key": key, "area": area, "name": name,
            "start": start, "end": end,
            "pay_start": pay_start, "pay_end": pay_end,
            "phase": phase, "remaining": remaining,
            "signup_url": signup_url, "info_url": info_url,
            "external_url": external_url,
        })

    # ---- 近期已结束 ----
    for li in soup.select("#div_EntryLists_Closed li.li_Closed"):
        a = li.select_one("a")
        if not a:
            continue
        name = _clean(a.get_text())
        closed.append({"name": name, "external_url": a.get("href", "")})

    return active, closed


# --------------------------------------------------------------------------- #
# 变化检测
# --------------------------------------------------------------------------- #
def filter_entry(e, cfg):
    kws = cfg.get("keyword_filter") or []
    areas = cfg.get("area_filter") or []
    if kws and not any(k in e["name"] for k in kws):
        return False
    if areas and e["area"] not in areas:
        return False
    return True


def diff(prev_active, curr_active, curr_closed, cfg):
    """对比上一份快照, 返回事件消息列表。"""
    events = []
    prev = {e["key"]: e for e in prev_active}
    curr = {e["key"]: e for e in curr_active}
    closed_names = {c["name"] for c in curr_closed}

    for key, e in curr.items():
        if not filter_entry(e, cfg):
            continue
        if key not in prev:
            events.append(f"[新增] {e['area']} - {e['name']} "
                          f"(报名 {e['start']} ~ {e['end']})")
            if e["phase"] == "ongoing" and e["remaining"] is not None:
                events.append(f"    ↳ 该报名已开始, 距结束约 {e['remaining']} 分钟")

    for key, e in curr.items():
        if key not in prev or not filter_entry(e, cfg):
            continue
        p = prev[key]
        if p["phase"] == "upcoming" and e["phase"] == "ongoing":
            events.append(f"[报名开始] {e['area']} - {e['name']} "
                          f"(截止 {e['end']})")

    for key, p in prev.items():
        if key in curr or not filter_entry(p, cfg):
            continue
        if p["name"] in closed_names:
            events.append(f"[报名结束] {p['area']} - {p['name']}")
        else:
            events.append(f"[下线] {p['area']} - {p['name']}")

    thresh = cfg.get("alert_when_ending_within_hours", 2)
    if thresh and thresh > 0:
        limit = int(thresh * 60)
        for e in curr.values():
            if not filter_entry(e, cfg):
                continue
            if e["phase"] == "ongoing" and e["remaining"] is not None \
                    and e["remaining"] <= limit:
                p = prev.get(e["key"])
                was_below = p and p.get("remaining") is not None \
                    and p["remaining"] <= limit
                if not was_below:
                    events.append(f"[即将结束] {e['area']} - {e['name']} "
                                  f"距报名结束约 {e['remaining']} 分钟")

    return events


# --------------------------------------------------------------------------- #
# 通知通道
# --------------------------------------------------------------------------- #
def _ps_escape(s):
    return (s or "").replace("'", "''")


def send_windows_toast(title, msg):
    """Windows 原生 Toast, 失败回退到托盘气泡。"""
    if IS_CI:
        return
    ps = r'''
$ErrorActionPreference = 'SilentlyContinue'
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $nodes = $template.GetElementsByTagName('text')
    $nodes.Item(0).AppendChild($template.CreateTextNode('__TITLE__')) > $null
    $nodes.Item(1).AppendChild($template.CreateTextNode('__MSG__')) > $null
    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('考生之家监控').Show($toast)
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    $n = New-Object System.Windows.Forms.NotifyIcon
    $n.Icon = [System.Drawing.SystemIcons]::Information
    $n.Visible = $true
    $n.ShowBalloonTip(10000, '__TITLE__', '__MSG__', [System.Windows.Forms.ToolTipIcon]::Info)
    Start-Sleep -Seconds 11
    $n.Visible = $false
}
'''
    ps = ps.replace("__TITLE__", _ps_escape(title)).replace("__MSG__", _ps_escape(msg))
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1",
                                         encoding="utf-8-sig",
                                         delete=False) as f:
            f.write(ps)
            tmp = f.name
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", tmp],
            capture_output=True, timeout=30,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
    except Exception as e:
        log.warning("Windows 弹窗发送失败: %s", e)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def send_serverchan(cfg, title, msg):
    sc = cfg["notify"]["serverchan"]
    key = sc.get("sendkey", "").strip()
    if not key:
        log.warning("Server酱未配置 sendkey")
        return False
    url = sc.get("send_url") or f"https://sctapi.ftqq.com/{key}.send"
    try:
        r = requests.post(url, data={"title": title[:32], "desp": msg}, timeout=15)
        data = r.json()
        if data.get("code") == 0:
            log.info("Server酱推送成功")
            return True
        log.warning("Server酱返回异常: %s", data)
        return False
    except Exception as e:
        log.warning("Server酱发送失败: %s", e)
        return False


def send_telegram(cfg, title, msg):
    tg = cfg["notify"]["telegram"]
    token = tg.get("bot_token", "").strip()
    chat_id = tg.get("chat_id", "").strip()
    if not token or not chat_id:
        log.warning("Telegram 未配置")
        return False
    try:
        text = f"*{title}*\n\n{msg}"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={
            "chat_id": chat_id, "text": text, "parse_mode": "Markdown"
        }, timeout=15)
        if r.json().get("ok"):
            log.info("Telegram 推送成功")
            return True
        log.warning("Telegram 返回异常: %s", r.json())
        return False
    except Exception as e:
        log.warning("Telegram 发送失败: %s", e)
        return False


def send_email(cfg, title, msg):
    em = cfg["notify"]["email"]
    if not em.get("smtp_host") or not em.get("to"):
        log.warning("邮件未配置 smtp_host / to")
        return False
    try:
        mime = MIMEText(msg, "plain", "utf-8")
        mime["Subject"] = Header(title, "utf-8")
        mime["From"] = formataddr((str(Header("考生之家监控", "utf-8")), em.get("from")))
        mime["To"] = em.get("to")

        host, port = em["smtp_host"], int(em.get("smtp_port", 465))
        if em.get("use_ssl", True):
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()
        if em.get("username"):
            server.login(em["username"], em.get("password", ""))
        server.sendmail(em.get("from"), em.get("to").split(","), mime.as_string())
        server.quit()
        log.info("邮件发送成功")
        return True
    except Exception as e:
        log.warning("邮件发送失败: %s", e)
        return False


def notify(cfg, title, msg):
    n = cfg["notify"]
    ok = False
    if n.get("windows_toast") and not IS_CI:
        send_windows_toast(title, msg)
        ok = True
    if n.get("serverchan", {}).get("enabled"):
        ok = send_serverchan(cfg, title, msg) or ok
    if n.get("telegram", {}).get("enabled"):
        ok = send_telegram(cfg, title, msg) or ok
    if n.get("email", {}).get("enabled"):
        ok = send_email(cfg, title, msg) or ok
    return ok


# --------------------------------------------------------------------------- #
# 状态持久化
# --------------------------------------------------------------------------- #
def load_state(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("状态文件损坏, 重新建立基线")
    return {"entries": []}


def save_state(path, active):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"updated_at": datetime.now().isoformat(timespec="seconds"),
                   "entries": active}, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_once(cfg, first_run=False):
    html = fetch_html(cfg["url"])
    active, closed = parse_entries(html)
    log.info("抓取到 %d 条进行中/即将开始, %d 条已结束",
             len(active), len(closed))

    state = load_state(cfg["state_file"])
    events = diff(state.get("entries", []), active, closed, cfg)
    save_state(cfg["state_file"], active)

    if first_run or state.get("entries") is None or not state.get("entries"):
        log.info("首次运行, 已建立基线(%d 条), 不触发通知", len(active))
        return active, closed, []

    if events:
        title = f"考生之家监控 - {len(events)} 条变化"
        msg = "\n".join(events)
        log.info("检测到变化:\n%s", msg)
        notify(cfg, title, msg)
    else:
        log.info("无变化")
    return active, closed, events


def main():
    ap = argparse.ArgumentParser(description="考生之家报名监控")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--once", action="store_true", help="只运行一次")
    ap.add_argument("--test-notify", action="store_true",
                    help="测试通知通道后退出")
    args = ap.parse_args()

    cfg = load_config(args.config)

    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(cfg["log_file"], encoding="utf-8"),
        ],
    )

    if args.test_notify:
        log.info("测试通知通道...")
        notify(cfg, "考生之家监控 - 测试",
               "这是一条测试消息, 如果你看到它, 说明通知通道正常。")
        log.info("测试完成")
        return

    if args.once:
        run_once(cfg)
        return

    interval = max(1, int(cfg.get("interval_minutes", 15)))
    log.info("开始监控 %s, 每 %d 分钟检查一次 (Ctrl+C 退出)",
             cfg["url"], interval)
    first = True
    while True:
        try:
            run_once(cfg, first_run=first)
            first = False
        except KeyboardInterrupt:
            log.info("已退出")
            break
        except Exception as e:
            log.error("本轮出错: %s", e, exc_info=True)
        try:
            time.sleep(interval * 60)
        except KeyboardInterrupt:
            log.info("已退出")
            break


if __name__ == "__main__":
    main()