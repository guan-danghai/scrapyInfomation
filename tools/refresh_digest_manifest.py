#!/usr/bin/env python3
"""
从库重写指定 digest_date 的 manifest，并写入 digest_item_presale_route（@售前）。
默认读取 digest_pending_send.json 的 token + digest_date。

售前匹配默认遵循 config.ini [dispatch] enable_ai_customer_match（一般会调用大模型做机构对齐）。
  --with-ai   本次强制开启 AI 匹配（覆盖 config）
  --no-ai     本次强制关闭 AI，仅用规则/归一化（大批量更快）

用法（项目根）:
  python tools/refresh_digest_manifest.py
  python tools/refresh_digest_manifest.py --date 2026-05-07 --token <32hex>
  python tools/refresh_digest_manifest.py --with-ai -v   # -v 详细日志（每条首次匹配耗时）
  python tools/refresh_digest_manifest.py --presale-only   # 不重写 manifest，仅重算 @售前 路由表
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import configparser

from digest_message import (
    build_digest_pack_card_url,
    get_digest_payload_for_ingest_date,
    write_manifest_for_existing_token,
)


def _persist_routes_only(
    cfg_path: Path,
    tok: str,
    *,
    force_ai: bool | None,
    verbose: bool = False,
) -> tuple[int, bool]:
    """仅写入 digest_item_presale_route；force_ai True/False 覆盖配置，None 表示读配置。"""
    import pymysql
    from dispatch_router import load_dispatch_config, persist_digest_item_routes_for_token

    mf = ROOT / "digest_packs" / tok / "manifest.json"
    items = (json.loads(mf.read_text(encoding="utf-8")) or {}).get("items") or []
    dcfg = load_dispatch_config(cfg_path)
    if force_ai is True:
        dcfg.enable_ai_customer_match = True
    elif force_ai is False:
        dcfg.enable_ai_customer_match = False
    ai_on = bool(dcfg.enable_ai_customer_match)

    cp = configparser.ConfigParser()
    cp.read(cfg_path, encoding="utf-8")
    db = cp["database"]
    conn = pymysql.connect(
        host=db["host"],
        port=int(db["port"]),
        user=db["user"],
        password=db["password"],
        database=db["database"],
        charset=db.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    try:
        pr = persist_digest_item_routes_for_token(
            conn, tok, items, dcfg, config_path=cfg_path, verbose=verbose
        )
        return pr, ai_on
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="按库刷新摘要 manifest + 售前路由表；加 --presale-only 则只重算 @售前（digest_item_presale_route）"
    )
    ap.add_argument("--date", dest="day", default="", help="入库日 YYYY-MM-DD；默认读 digest_pending_send.json")
    ap.add_argument("--token", dest="tok", default="", help="32 位 hex；默认读 digest_pending_send.json")
    ap.add_argument(
        "--no-pending-json",
        action="store_true",
        help="不更新 digest_pending_send.json 的标题/概览",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument(
        "--no-ai",
        action="store_true",
        help="售前匹配强制不用 AI（仅规则/归一化，较快）",
    )
    g.add_argument(
        "--with-ai",
        action="store_true",
        help="售前匹配强制开启 AI（覆盖 config.ini enable_ai_customer_match）",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="售前路由写入时打印详细进度（每条首次匹配耗时等）",
    )
    ap.add_argument(
        "--presale-only",
        action="store_true",
        help="不重写 digest_packs/<token>/manifest.json，仅按现有 manifest 重算 digest_item_presale_route（@售前）",
    )
    args = ap.parse_args()

    cfg_path = ROOT / "config.ini"
    pending_path = ROOT / "digest_pending_send.json"

    day = (args.day or "").strip()
    tok = (args.tok or "").strip().lower()

    if not day or not tok:
        if not pending_path.is_file():
            print("缺少 digest_pending_send.json，请指定 --date 与 --token", file=sys.stderr)
            return 1
        pen = json.loads(pending_path.read_text(encoding="utf-8"))
        day = day or str(pen.get("digest_date") or "").strip()
        tok = tok or str(pen.get("token") or "").strip().lower()
    if not day:
        day = date.today().isoformat()

    if len(tok) != 32:
        print("token 须为 32 位 hex", file=sys.stderr)
        return 1

    force_ai: bool | None
    if args.with_ai:
        force_ai = True
    elif args.no_ai:
        force_ai = False
    else:
        force_ai = None

    mode = "config"
    if args.with_ai:
        mode = "--with-ai"
    elif args.no_ai:
        mode = "--no-ai"

    print(
        f"[start] digest_date={day} token={tok[:8]}… presale_match={mode} "
        f"verbose={args.verbose} presale_only={args.presale_only}",
        flush=True,
    )

    if args.verbose:
        os.environ["DISPATCH_PERSIST_VERBOSE"] = "1"

    if args.presale_only:
        mf_path = ROOT / "digest_packs" / tok / "manifest.json"
        if not mf_path.is_file():
            print(f"[presale-only] 缺少 manifest，无法匹配售前: {mf_path}", file=sys.stderr)
            os.environ.pop("DISPATCH_PERSIST_VERBOSE", None)
            return 1
        pr, ai_on = _persist_routes_only(
            cfg_path, tok, force_ai=force_ai, verbose=args.verbose
        )
        print(
            f"[presale-only] digest_item_presale_route rows={pr}（AI={'开' if ai_on else '关'}，来源={mode}）",
            flush=True,
        )
        os.environ.pop("DISPATCH_PERSIST_VERBOSE", None)
        if args.no_pending_json:
            return 0
        title, description, _ = get_digest_payload_for_ingest_date(cfg_path, day)
        cp = configparser.ConfigParser()
        cp.read(cfg_path, encoding="utf-8")
        base = ""
        if cp.has_section("wecom"):
            base = (cp["wecom"].get("disclose_page_url") or "").strip().rstrip("/")
        card_url = build_digest_pack_card_url(base, tok) if base else ""
        note = "refresh_digest_manifest.py（仅 @售前 路由，未重写 manifest"
        if force_ai is True:
            note += "，售前匹配强制 AI"
        elif force_ai is False:
            note += "，售前匹配无 AI"
        else:
            note += "，售前匹配随 config"
        note += "）"
        pending_obj = {
            "token": tok,
            "digest_date": day,
            "title": title,
            "description": description,
            "card_url": card_url,
            "disclose_page_url": base,
            "generated_at": datetime.now().replace(microsecond=0).isoformat(),
            "pipeline_note": note,
        }
        pending_path.write_text(
            json.dumps(pending_obj, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[pending] 已更新 {pending_path.name}", flush=True)
        return 0

    # 需要覆盖 AI 开关时：先只写 manifest，再单独 persist（否则 write_manifest 内部读配置无法传参）
    if force_ai is not None:
        os.environ["DIGEST_SKIP_PERSIST_ROUTES"] = "1"
        try:
            n = write_manifest_for_existing_token(cfg_path, day, tok)
        finally:
            os.environ.pop("DIGEST_SKIP_PERSIST_ROUTES", None)
        print(f"[manifest] items={n}", flush=True)
        pr, ai_on = _persist_routes_only(
            cfg_path, tok, force_ai=force_ai, verbose=args.verbose
        )
        print(
            f"[presale] digest_item_presale_route rows={pr}（AI={'开' if ai_on else '关'}，来源={mode}）",
            flush=True,
        )
    else:
        n = write_manifest_for_existing_token(cfg_path, day, tok)
        print(f"[manifest] items={n}", flush=True)
        cp = configparser.ConfigParser()
        cp.read(cfg_path, encoding="utf-8")
        ai_cfg = True
        if cp.has_section("dispatch"):
            ai_cfg = cp["dispatch"].getboolean("enable_ai_customer_match", fallback=True)
        print(
            f"[presale] 已在 write_manifest 内写入 digest_item_presale_route（AI 随 config.ini：{'开' if ai_cfg else '关'}）",
            flush=True,
        )

    os.environ.pop("DISPATCH_PERSIST_VERBOSE", None)

    if args.no_pending_json:
        return 0

    title, description, _ = get_digest_payload_for_ingest_date(cfg_path, day)
    cp = configparser.ConfigParser()
    cp.read(cfg_path, encoding="utf-8")
    base = ""
    if cp.has_section("wecom"):
        base = (cp["wecom"].get("disclose_page_url") or "").strip().rstrip("/")
    card_url = build_digest_pack_card_url(base, tok) if base else ""
    note = "refresh_digest_manifest.py（manifest + digest_item_presale_route"
    if force_ai is True:
        note += "，售前匹配强制 AI"
    elif force_ai is False:
        note += "，售前匹配无 AI"
    else:
        note += "，售前匹配随 config"
    note += "）"
    pending_obj = {
        "token": tok,
        "digest_date": day,
        "title": title,
        "description": description,
        "card_url": card_url,
        "disclose_page_url": base,
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "pipeline_note": note,
    }
    pending_path.write_text(
        json.dumps(pending_obj, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[pending] 已更新 {pending_path.name}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
