# -*- coding: utf-8 -*-
"""
统一监控脚本 - 同时监控考生之家和招教网，统一通过 Telegram 推送日报。

用法:
  python unified_monitor.py              # 运行一次，推送日报
  python unified_monitor.py --test       # 只测试不推送
  python unified_monitor.py --test-notify # 仅测试推送通道
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime

import requests

# 复用两个模块的核心函数
from bm_monitor import (
    fetch_html as bm_fetch,
    parse_entries as bm_parse,
    build_daily_report as bm_build_report,
    load_state as bm_load_state,
    save_state as bm_save_state,
    load_config as bm_load_config,
    filter_entry as bm_filter,
    send_telegram,
    send_serverchan,
    IS_CI,
)

from zhaojiao_monitor import (
    fetch_all_pages as zj_fetch_all,
    parse_all_pages as zj_parse_all,
    build_report as zj_build_report,
    load_state as zj_load_state,
    save_state as zj_save_state,
    load_config as zj_load_config,
    filter_entry as zj_filter,
)

log = logging.getLogger("unified_monitor")


def run_once():
    """运行一次：同时抓取两个站点，生成统一日报并推送。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ===================================================================== #
    # 第一部分：考生之家
    # ===================================================================== #
    bm_cfg = bm_load_config()
    bm_report = ""
    bm_has_changes = False
    bm_success = False

    try:
        log.info("=" * 40)
        log.info("抓取考生之家...")
        html = bm_fetch(bm_cfg["url"])
        active, closed = bm_parse(html)
        log.info("考生之家: %d 条进行中, %d 条已结束", len(active), len(closed))

        prev_state = bm_load_state(bm_cfg["state_file"])
        bm_report, bm_has_changes = bm_build_report(active, closed, prev_state, bm_cfg)
        bm_save_state(bm_cfg["state_file"], active, closed)

        is_first = not prev_state.get("entries")
        bm_success = True
        log.info("考生之家日报生成完成")
    except Exception as e:
        log.error("考生之家抓取失败: %s", e, exc_info=True)
        bm_report = f"⚠️ 考生之家抓取失败: {e}"

    # ===================================================================== #
    # 第二部分：招教网
    # ===================================================================== #
    zj_cfg = zj_load_config()
    zj_report = ""
    zj_has_changes = False
    zj_success = False

    try:
        log.info("=" * 40)
        log.info("抓取招教网...")
        all_html = zj_fetch_all(zj_cfg)
        entries = zj_parse_all(all_html)
        log.info("招教网: %d 条招聘信息", len(entries))

        prev_state = zj_load_state(zj_cfg["state_file"])
        zj_report, zj_has_changes = zj_build_report(entries, prev_state, zj_cfg)
        zj_save_state(zj_cfg["state_file"], entries)

        zj_success = True
        log.info("招教网日报生成完成")
    except Exception as e:
        log.error("招教网抓取失败: %s", e, exc_info=True)
        zj_report = f"⚠️ 招教网抓取失败: {e}"
        # 确保状态文件存在，避免 git add 失败
        zj_save_state(zj_cfg["state_file"], zj_load_state(zj_cfg["state_file"]).get("entries", []))

    # ===================================================================== #
    # 第三部分：组装统一消息
    # ===================================================================== #
    sections = []
    sections.append(f"📋 <b>每日招聘监控日报</b>")
    sections.append(f"更新时间：{now}")
    sections.append("")

    # 考生之家
    sections.append("━" * 20)
    sections.append("🏫 <b>考生之家</b> (bm.e21cn.com)")
    sections.append("━" * 20)
    sections.append(bm_report)
    sections.append("")

    # 招教网
    sections.append("━" * 20)
    sections.append("🎓 <b>招教网</b> (zhaojiao.net / 四川)")
    sections.append("━" * 20)
    sections.append(zj_report)
    sections.append("")

    sections.append("— 统一监控 | litrash/bm-monitor")

    full_report = "\n".join(sections)

    # ===================================================================== #
    # 第四部分：推送
    # ===================================================================== #
    has_changes = bm_has_changes or zj_has_changes

    title = "招聘监控日报"
    if has_changes:
        title += " [有变化]"

    # Telegram 主通道
    tg_token = bm_cfg["tg_bot_token"]
    tg_chat_id = bm_cfg["tg_chat_id"]
    tg_ok = send_telegram(tg_token, tg_chat_id, title, full_report)

    # Server酱 备用
    sc_ok = False
    if has_changes:
        sc_ok = send_serverchan(bm_cfg["sc_sendkey"], title, full_report)

    log.info("推送结果: Telegram=%s, Server酱=%s", "OK" if tg_ok else "FAIL", "OK" if sc_ok else "SKIP")
    log.info("\n%s", full_report)

    return full_report, tg_ok


def main():
    ap = argparse.ArgumentParser(description="统一招聘监控")
    ap.add_argument("--test", action="store_true", help="只测试不推送")
    ap.add_argument("--test-notify", action="store_true", help="仅测试推送通道")
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

    if args.test_notify:
        bm_cfg = bm_load_config()
        log.info("测试推送通道...")
        test_msg = (
            "🧪 <b>统一监控测试</b>\n\n"
            "推送通道正常！如果你看到这条消息，说明配置成功。\n\n"
            "监控范围：\n"
            "  🏫 考生之家 (bm.e21cn.com)\n"
            "  🎓 招教网 (zhaojiao.net / 四川)\n\n"
            "— 统一监控 | litrash/bm-monitor"
        )
        ok = send_telegram(bm_cfg["tg_bot_token"], bm_cfg["tg_chat_id"], "🧪 统一监控 - 测试", test_msg)
        log.info("测试%s", "完成" if ok else "失败")
        return

    if args.test:
        log.info("测试模式（不推送）")
        bm_cfg = bm_load_config()
        zj_cfg = zj_load_config()

        log.info("--- 考生之家 ---")
        html = bm_fetch(bm_cfg["url"])
        active, closed = bm_parse(html)
        log.info("%d 条进行中, %d 条已结束", len(active), len(closed))
        for a in active[:5]:
            log.info("  [%s] %s %s~%s", a["area"], a["name"], a["start"], a["end"])

        log.info("--- 招教网 ---")
        all_html = zj_fetch_all(zj_cfg)
        entries = zj_parse_all(all_html)
        log.info("%d 条", len(entries))
        for e in entries[:5]:
            log.info("  [%s] %s | %s", e["city"], e["title"], e["date"])
        return

    run_once()


if __name__ == "__main__":
    main()