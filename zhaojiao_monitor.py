# -*- coding: utf-8 -*-
"""
招教网(www.zhaojiao.net) 教师招聘信息爬取模块

爬取指定列表页的前N页，解析招聘公告条目，支持变化检测和状态持久化。

用法:
  python zhaojiao_monitor.py             # 本地运行一次
  python zhaojiao_monitor.py --test      # 测试解析
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup

DEFAULT_STATE = "zj_monitor_state.json"
DEFAULT_LOG = "zj_monitor.log"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# 模拟完整浏览器请求头，避免被反爬拦截
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.zhaojiao.net/",
    "Cache-Control": "max-age=0",
}

log = logging.getLogger("zj_monitor")
IS_CI = os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"


# --------------------------------------------------------------------------- #
# 代理支持
# --------------------------------------------------------------------------- #
def _get_proxy():
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("BM_PROXY") or ""
    if proxy_url:
        return {"http": proxy_url, "https": proxy_url}
    return None


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _env(v):
    return os.environ.get(v, "")


def load_config():
    cfg = {
        "base_url": _env("ZJ_URL") or "https://www.zhaojiao.net/zhaojiao/list-150.html",
        "areaid": _env("ZJ_AREAID") or "23",  # 23 = 四川
        "pages": int(_env("ZJ_PAGES") or "3"),
        "state_file": DEFAULT_STATE,
        "log_file": DEFAULT_LOG,
        "keyword_filter": [x.strip() for x in _env("ZJ_KEYWORDS").split(",") if x.strip()],
    }
    return cfg


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
def _build_page_url(base_url, areaid, page_num):
    """构造分页URL: list-150-{page}.html?areaid=23 (第1页用 list-150.html)"""
    if page_num <= 1:
        return f"{base_url}?areaid={areaid}"
    # base_url 如 list-150.html → list-150-{page}.html
    if base_url.endswith(".html"):
        paged = base_url.replace(".html", f"-{page_num}.html")
    else:
        paged = f"{base_url}-{page_num}.html"
    return f"{paged}?areaid={areaid}"


def fetch_page(base_url, areaid, page_num, retries=3):
    url = _build_page_url(base_url, areaid, page_num)
    session = requests.Session()
    proxies = _get_proxy()
    last_error = None
    for i in range(retries):
        try:
            if i == 0:
                try:
                    session.get("https://www.zhaojiao.net/", headers=HEADERS, timeout=15, proxies=proxies)
                except Exception:
                    pass
            r = session.get(url, headers=HEADERS, timeout=30, proxies=proxies)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except Exception as e:
            last_error = e
            if i == retries - 1:
                raise
            log.warning("第%d页 第%d次抓取失败: %s, %d秒后重试...", page_num, i + 1, e, (i + 1) * 5)
            time.sleep((i + 1) * 5)
    raise last_error


def fetch_all_pages(cfg, pages=None):
    if pages is None:
        pages = cfg.get("pages", 3)
    all_html = []
    for p in range(1, pages + 1):
        html = fetch_page(cfg["base_url"], cfg["areaid"], p)
        all_html.append(html)
        log.info("第%d页抓取成功, 长度=%d", p, len(html))
    return all_html


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _clean(s):
    return " ".join((s or "").split())


def parse_entries(html):
    """从HTML中解析招聘条目列表。返回 list of dicts。"""
    soup = BeautifulSoup(html, "lxml")
    entries = []
    base_href = "https://www.zhaojiao.net/zhaojiao/"

    for item in soup.select("div.content-item"):
        a = item.select_one("a.item-con")
        if not a:
            continue
        href = a.get("href", "")
        full_url = href if href.startswith("http") else base_href.rstrip("/") + "/" + href.lstrip("/")

        area_el = item.select_one("span.item-zp")
        title_el = item.select_one("span.item-text")
        date_el = item.select_one("span.item-time")

        area = _clean(area_el.get_text()) if area_el else ""
        title = _clean(title_el.get_text()) if title_el else ""
        date_str = _clean(date_el.get_text()) if date_el else ""

        # 提取人数
        count = None
        m = re.search(r'(\d+)人', title)
        if m:
            count = int(m.group(1))

        # 提取城市（从area标签中：如 [成都市教师招聘] → 成都市）
        city = ""
        m = re.search(r'\[(.+?)教师招聘\]', area)
        if m:
            city = m.group(1)

        key = f"{title}|{date_str}"
        entries.append({
            "key": key,
            "area": area,
            "city": city,
            "title": title,
            "date": date_str,
            "count": count,
            "url": full_url,
        })

    return entries


def parse_all_pages(all_html):
    """解析多页HTML，去重合并。"""
    all_entries = []
    seen_keys = set()
    for html in all_html:
        entries = parse_entries(html)
        for e in entries:
            if e["key"] not in seen_keys:
                seen_keys.add(e["key"])
                all_entries.append(e)
    return all_entries


# --------------------------------------------------------------------------- #
# 过滤
# --------------------------------------------------------------------------- #
def filter_entry(e, cfg):
    kws = cfg.get("keyword_filter") or []
    if kws and not any(k in e["title"] for k in kws):
        return False
    return True


# --------------------------------------------------------------------------- #
# 构建日报
# --------------------------------------------------------------------------- #
def build_report(entries, prev_state, cfg):
    """生成招教网日报消息，标注变化。"""
    prev_map = {e["key"]: e for e in prev_state.get("entries", [])}
    curr_keys = {e["key"] for e in entries}
    is_first = not prev_map

    lines = []
    new_count = 0

    for e in entries:
        if not filter_entry(e, cfg):
            continue
        key = e["key"]
        prev = prev_map.get(key)

        if is_first or key not in prev_map:
            tag = "🆕"
            new_count += 1
        else:
            tag = ""

        count_str = f"（{e['count']}人）" if e.get("count") else ""
        line = f"{tag}{e['title']}{count_str}"
        if e.get("date"):
            line += f"\n    📅 {e['date']}"
        line += f"\n    🔗 {e['url']}"
        lines.append((e, line, tag))

    # 检测已下线的
    removed = []
    for key in prev_map:
        if key not in curr_keys:
            pe = prev_map[key]
            removed.append(f"❌ 已下线：{pe['title']}")

    # 组装消息
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg_lines = [f"📋 招教网招聘日报（四川）", f"更新时间：{now}", ""]

    msg_lines.append(f"📊 当前共 {len(lines)} 条招聘信息")
    if new_count:
        msg_lines.append(f"   🆕 新增 {new_count} 条")
    if removed:
        msg_lines.append(f"   ❌ 已下线 {len(removed)} 条")
    msg_lines.append("")

    msg_lines.append("━" * 20)
    for e, line, tag in lines:
        msg_lines.append(line)
        msg_lines.append("")

    if removed:
        msg_lines.append("━" * 20)
        for r in removed:
            msg_lines.append(r)

    msg_lines.append("")
    msg_lines.append("— 招教网监控 | litrash/bm-monitor")

    return "\n".join(msg_lines), bool(new_count or removed)


# --------------------------------------------------------------------------- #
# 状态持久化
# --------------------------------------------------------------------------- #
def load_state(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"entries": []}


def save_state(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "entries": entries,
        }, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_once(cfg):
    all_html = fetch_all_pages(cfg)
    entries = parse_all_pages(all_html)
    log.info("共解析到 %d 条招聘信息", len(entries))

    prev_state = load_state(cfg["state_file"])
    report, has_changes = build_report(entries, prev_state, cfg)
    save_state(cfg["state_file"], entries)

    log.info("\n%s", report)
    return report, has_changes, entries


def main():
    ap = argparse.ArgumentParser(description="招教网招聘信息监控")
    ap.add_argument("--test", action="store_true", help="测试解析")
    args = ap.parse_args()

    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    cfg = load_config()

    if args.test:
        log.info("测试解析招教网...")
        all_html = fetch_all_pages(cfg)
        entries = parse_all_pages(all_html)
        log.info("共 %d 条", len(entries))
        for e in entries[:10]:
            print(f"  [{e['city']}] {e['title']} | {e['date']} | {e['url']}")
        return

    run_once(cfg)


if __name__ == "__main__":
    main()