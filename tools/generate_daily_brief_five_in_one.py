#!/usr/bin/env python3
"""
按指定日期（默认昨天）从 scraping_infos 取「审核通过」记录，调用 [ai] 生成五合一简报 Markdown。

用法:
  python tools/generate_daily_brief_five_in_one.py
  python tools/generate_daily_brief_five_in_one.py --date 2026-05-07
  python tools/generate_daily_brief_five_in_one.py --dry-run   # 仅导出 JSON 不调模型
"""
from __future__ import annotations

import argparse
import configparser
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.ini"


def load_db():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG, encoding="utf-8")
    if not cfg.has_section("database"):
        return None
    d = cfg["database"]
    return {
        "host": d.get("host", "127.0.0.1"),
        "port": int(d.get("port", 3306)),
        "database": d.get("database", ""),
        "user": d.get("user", "root"),
        "password": d.get("password", ""),
        "charset": d.get("charset", "utf8mb4"),
    }


def fetch_rows(target_date: str) -> list[dict]:
    import pymysql

    db = load_db()
    if not db or not db.get("database"):
        raise SystemExit("config.ini [database] 未配置完整")

    sql = """
SELECT id, title, sub_type, product_related, reserve2,
       project_no, project_budget, winning_amount,
       project_owner, winning_bidder, bidding_agent,
       published_at, bid_deadline, detail_url, created_at, audit_status
FROM scraping_infos
WHERE DATE(created_at) = %s
  AND COALESCE(NULLIF(TRIM(audit_status), ''), '审核通过') = %s
ORDER BY id
"""
    conn = pymysql.connect(
        host=db["host"],
        port=db["port"],
        user=db["user"],
        password=db["password"],
        database=db["database"],
        charset=db["charset"],
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (target_date, "审核通过"))
            rows = cur.fetchall()
            for row in rows:
                for k, v in list(row.items()):
                    if hasattr(v, "isoformat"):
                        row[k] = v.isoformat() if v else None
            return rows
    finally:
        conn.close()


def build_prompt(rows: list[dict], target_date: str) -> str:
    sub_counts = Counter((r.get("sub_type") or "").strip() or "(空)" for r in rows)
    stats_lines = "\n".join(
        f"- {k}: {v}条" for k, v in sorted(sub_counts.items(), key=lambda x: (-x[1], x[0]))
    )

    lines = []
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i}. [{r.get('sub_type')}] {r.get('title')}\n"
            f"   科技归属:{r.get('product_related')} | 标签:{r.get('reserve2')}\n"
            f"   业主:{r.get('project_owner')} | 中标:{r.get('winning_bidder') or '-'}"
        )
    blob = "\n".join(lines)
    n = len(rows)
    return f"""你是金融行业招投标情报分析师。以下数据均为入库日 {target_date} 已审核通过的金融机构相关招投标条目（共{n}条），请基于标题与字段生成多份「简报」，供领导选型。

【库表 sub_type 已统计（简报A 必须原样引用，勿改动数字）】
{stats_lines}

【原始条目清单】
{blob}

请严格输出以下结构（用 Markdown 大标题分隔），每份简报换一种侧重点：

# 简报A：高管一页纸（市场扫描）
- 3～5 句总览 + 本日最值得关注的 5 条（一句话+理由）
- 类型分布：必须使用上文「库表 sub_type 已统计」中的数字；流标/终止等可从标题归纳一句，勿与上表矛盾

# 简报B：产品与赛道热力（售前视角）
- 按主题聚类：如核心/信贷/数据平台/信创/运维外包/监管报送/安全等
- 每类 2～4 条代表项目（机构名+关键词）

# 简报C：竞争格局快照（厂商与价格战）
- 若中标人有重复出现，点出头部厂商
- 人力单价类（万元/人月）若条目 project_budget 等字段中出现则归纳；没有则说明「条目多数未披露」

# 简报D：区域与机构类型
- 银行/信托/消金/农信等维度的一句话分布（可用条目归纳，注明为近似）
- 区域性机会提示（若有）

# 简报E：下周行动清单（销售可执行）
- 待投标/采购中的机会 TOP（若 bid_deadline 有则写出）
- 已中标里可跟进的维保/二期线索

要求：基于给定条目，不要编造未出现的项目名；数字与条数优先引用清单中的事实；简明中文。
"""


def call_ai(prompt: str) -> str:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG, encoding="utf-8")
    if not cfg.has_section("ai"):
        raise SystemExit("缺少 [ai] 配置")
    s = cfg["ai"]
    api_key = (s.get("api_key") or "").strip()
    if not api_key:
        raise SystemExit("config.ini [ai] api_key 为空")
    base_url = (s.get("base_url") or "").strip() or None
    model = s.get("model", "deepseek-chat").strip()

    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.35,
        max_tokens=8192,
    )
    return (resp.choices[0].message.content or "").strip()


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="生成指定日五合一简报")
    ap.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="按 created_at 日期筛选；默认昨天",
    )
    ap.add_argument("--dry-run", action="store_true", help="只写出输入 JSON，不调用大模型")
    args = ap.parse_args()

    if args.date:
        target = args.date.strip()[:10]
    else:
        target = (date.today() - timedelta(days=1)).isoformat()

    rows = fetch_rows(target)
    out_dir = ROOT / "output" / "briefings"
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"briefing_input_{target}.json"
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"条数={len(rows)} 输入已写 {json_path}")

    if args.dry_run:
        return

    if not rows:
        md_path = out_dir / f"briefing_{target}_five_in_one.md"
        md_path.write_text(
            f"# 五合一简报（{target}）\n\n当日无「审核通过」入库记录，未调用模型。\n",
            encoding="utf-8",
        )
        print(f"已写 {md_path}")
        return

    prompt = build_prompt(rows, target)
    text = call_ai(prompt)
    md_path = out_dir / f"briefing_{target}_five_in_one.md"
    header = f"<!-- 入库日 {target} | 条数 {len(rows)} | 审核通过 -->\n\n"
    md_path.write_text(header + text, encoding="utf-8")
    print(f"简报已写 {md_path}")


if __name__ == "__main__":
    main()
