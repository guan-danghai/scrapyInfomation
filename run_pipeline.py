#!/usr/bin/env python3
"""
串联链路：爬取 → 分析入库 → 生成摘要快照包并写入 digest_pending_send.json；
企业微信「今日摘要」卡片改在审核工作台手动选择收件人后发送（不再自动推送）。

执行：python run_pipeline.py
      python run_pipeline.py --t-minus-1   # 爬取与统计区间为昨天（T-1），适合清晨定时跑前一日数据
      python run_pipeline.py --always-digest   # 入库为 0 时仍强制生成摘要包（默认会跳过，避免重复 token）
依赖：config.ini 中 [scraper] / [database] / [ai] 已配置；[wecom].disclose_page_url 可选（卡片跳转基址）。
"""

import argparse
import asyncio
import configparser
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.ini"


def _resolve_date(v: str) -> str:
    """today / yesterday / t-1 解析为 YYYY-MM-DD，否则返回原值。"""
    if not v or not (v := v.strip()):
        return ""
    low = v.lower()
    if low == "today":
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d")
    if low in ("yesterday", "t-1", "t_minus_1"):
        from datetime import datetime, timedelta

        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    return v


def _load_full_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg


def main():
    if sys.platform == "win32" and (not sys.stdout.encoding or "utf" not in sys.stdout.encoding.lower()):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="爬取 → 入库 → 摘要快照（企微在审核台发送）")
    parser.add_argument(
        "--t-minus-1",
        "--yesterday",
        action="store_true",
        dest="t_minus_1",
        help="时间区间固定为昨天（T-1），覆盖 config.ini 中 start_date / end_date",
    )
    parser.add_argument(
        "--always-digest",
        action="store_true",
        help="即使本轮入库新增与待审核 JSON 更新均为 0，仍强制生成摘要包；默认跳过以免同日重复多 token",
    )
    args = parser.parse_args()

    cfg = _load_full_config()
    if not cfg.has_section("scraper"):
        print("错误：config.ini 缺少 [scraper] 配置。")
        sys.exit(1)

    s = cfg["scraper"]
    raw_kws = [k.strip() for k in (s.get("keywords") or "").replace("，", ",").split(",") if k.strip()]
    keywords = raw_kws if raw_kws else ["招标公告"]
    max_pages = s.getint("max_pages", 3)
    raw_start = s.get("start_date", "") or ""
    raw_end = s.get("end_date", "") or ""
    if args.t_minus_1:
        from datetime import datetime, timedelta

        y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        start_date = end_date = y
        raw_start = raw_end = f"T-1 ({y})"
    else:
        start_date = _resolve_date(raw_start)
        end_date = _resolve_date(raw_end)

    print("=" * 52)
    print("  招投标采集链路：爬取 → 入库 → 生成摘要快照（企微在审核台发送）")
    print("  配置来源：", CONFIG_FILE.resolve())
    print("  读取的日期：start_date=%r  end_date=%r" % (raw_start, raw_end))
    print("  关键词：", keywords)
    print("  时间范围：", f"{start_date} ~ {end_date}" if (start_date or end_date) else "不筛选")
    print("=" * 52)

    # 0. --t-minus-1 时：昨天（T-1）入库且仍为「待审核」的 URL，登录后直开详情补抓，再跑正常关键词列表
    pending_reurls = None
    if args.t_minus_1:
        from datetime import datetime, timedelta

        yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            from pending_reaudit_db import fetch_pending_urls_for_reaudit

            pending_reurls = fetch_pending_urls_for_reaudit(CONFIG_FILE, yday)
            if pending_reurls:
                print(
                    f"\n  [昨天待审核补爬] 将按库查询补抓 {len(pending_reurls)} 条（入库日 DATE(created_at)={yday}，audit_status=待审核）"
                )
            else:
                pending_reurls = None
        except Exception as e:
            print(f"\n  [WARN] 昨天待审核补爬查询失败，跳过: {e}")
            pending_reurls = None

    # 1. 爬取（先执行待审核 URL 直开，再关键词列表）
    import scraper

    result = asyncio.run(
        scraper.scrape(
            keywords,
            max_pages,
            start_date,
            end_date,
            pending_reurls=pending_reurls,
            pending_reaudit_config=CONFIG_FILE,
        )
    )

    if result is None:
        print("\n爬取阶段异常结束，跳过入库与推送。")
        sys.exit(1)

    date_str = result["date_str"]
    total_saved = result["total_saved"]
    total_failed = result["total_failed"]
    base_out = result["base_out"]

    # 2. 分析入库
    sys.path.insert(0, str(ROOT))
    import ingest_bank_to_db
    base_dir = ROOT / base_out
    if not base_dir.is_dir():
        print(f"\n输出目录不存在，跳过入库: {base_dir}")
        ingest_stats = {
            "inserted": 0,
            "updated_pending": 0,
            "skipped_tech": 0,
            "skipped_non_financial_owner": 0,
            "skipped_dup": 0,
            "errors": 0,
            "keywords_processed": [],
        }
    else:
        ingest_config = ingest_bank_to_db.load_config()
        ingest_stats = ingest_bank_to_db.run_ingest(base_dir, ingest_config)

    inserted = ingest_stats["inserted"]
    updated_pending = ingest_stats.get("updated_pending", 0)
    print(f"\n入库小结：新增 {inserted} 条；待审核记录经当日 JSON 重跑更新 {updated_pending} 条。")

    skip_digest = (
        not args.always_digest and inserted == 0 and updated_pending == 0
    )

    # 3. 生成摘要快照包 + 写入审核台「待发送」JSON（企微改为审核工作台手动确认收件人后发送）
    #    说明：摘要包是按「摘要日」从库里读审核通过数据打包；与本轮是否写入新库记录无关。
    #    若入库为 0 仍每次生成，会新建随机 token，内容与昨日同摘要日一致 → 审核台出现多条同日期。
    if skip_digest:
        print(
            "\n[摘要快照] 已跳过：本轮入库新增与待审核 JSON 更新均为 0。"
            "（摘要仍可按库重算，但默认不再新建 digest_packs token，避免与既有摘要日重复。）"
            "\n  若需按当前库强制重新生成 manifest / digest_pending_send.json：python run_pipeline.py --always-digest"
        )
    else:
        disclose_url_base = "https://work.weixin.qq.com"
        if cfg.has_section("wecom"):
            disclose_url_base = (
                (cfg["wecom"].get("disclose_page_url") or "").strip() or disclose_url_base
            )

        from datetime import datetime as _dt
        import json as _json

        from digest_message import (
            build_digest_pack_card_url,
            get_digest_payload,
            materialize_digest_pack,
        )

        pending_path = ROOT / "digest_pending_send.json"
        digest_link_date = ""
        title = ""
        description = ""
        ds_q = (start_date or "").strip() or None
        de_q = (end_date or "").strip() or None
        try:
            title, description, digest_link_date = get_digest_payload(CONFIG_FILE, ds_q, de_q)
        except Exception as e:
            print(f"\n[WARN] 摘要读库失败，使用降级文案: {e}")
            from digest_message import build_digest_title

            ds_fb = (start_date or "").strip()
            de_fb = (end_date or "").strip()
            if not ds_fb and not de_fb:
                ds_fb = de_fb = _dt.now().strftime("%Y-%m-%d")
            elif not de_fb:
                de_fb = ds_fb
            elif not ds_fb:
                ds_fb = de_fb
            title = build_digest_title(ds_fb, de_fb)
            description = "\n".join(
                [
                    "📌 今日概览",
                    "新增招标公告：— 条",
                    "新增中标公示：— 条",
                ]
            )
            digest_link_date = (de_fb or ds_fb) or _dt.now().strftime("%Y-%m-%d")

        if digest_link_date:
            try:
                pack_token = materialize_digest_pack(CONFIG_FILE, digest_link_date)
                disclose_url = build_digest_pack_card_url(
                    disclose_url_base.rstrip("/"), pack_token
                )
                pending_obj = {
                    "token": pack_token,
                    "digest_date": digest_link_date,
                    "title": title,
                    "description": description,
                    "card_url": disclose_url,
                    "disclose_page_url": disclose_url_base.rstrip("/"),
                    "generated_at": _dt.now().replace(microsecond=0).isoformat(),
                    "pipeline_note": f"start_date={raw_start!r} end_date={raw_end!r}",
                }
                pending_path.write_text(
                    _json.dumps(pending_obj, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(
                    f"\n[摘要快照] 已生成 digest_packs/{pack_token}/ ，待发送: {pending_path.name}"
                )
                print("  请到审核工作台「今日摘要推送」预览并选择成员后发送企微（已不再自动推送）。")
            except Exception as ex:
                print(f"\n[WARN] 摘要快照包生成失败: {ex}")
        else:
            print("\n[WARN] 无摘要日期，未生成 digest_pending_send.json")

    print("\n链路执行完毕。")


if __name__ == "__main__":
    main()
