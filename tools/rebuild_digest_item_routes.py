#!/usr/bin/env python3
"""
按当前 config（含 business_line 排除「售后维护条线」等）重算并覆盖 digest_item_presale_route。

用法（项目根）:
  python tools/rebuild_digest_item_routes.py
  python tools/rebuild_digest_item_routes.py 718d5b1583af6d7acd4b7f6c6d246e7f
  python tools/rebuild_digest_item_routes.py --all
未传 token 时默认扫描 digest_packs/* 下所有 manifest.json。

  python tools/rebuild_digest_item_routes.py 718d... --no-ai
  加 --no-ai 时关闭 AI 机构匹配，只走规则/归一化，适合大批量重算（与 routing_skip_ai 发信类似）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import configparser
import pymysql

from dispatch_router import load_dispatch_config, persist_digest_item_routes_for_token


def _connect():
    cfgp = configparser.ConfigParser()
    cfgp.read(ROOT / "config.ini", encoding="utf-8")
    if not cfgp.has_section("database"):
        raise RuntimeError("config.ini 缺少 [database]")
    db = cfgp["database"]
    return pymysql.connect(
        host=db.get("host", "127.0.0.1"),
        port=int(db.get("port", "3306")),
        user=db.get("user"),
        password=db.get("password", ""),
        database=db.get("database", ""),
        charset=db.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def tokens_from_disk() -> list[str]:
    pack_root = ROOT / "digest_packs"
    if not pack_root.is_dir():
        return []
    out: list[str] = []
    for d in pack_root.iterdir():
        if not d.is_dir():
            continue
        name = d.name.strip().lower()
        if len(name) != 32:
            continue
        mf = d / "manifest.json"
        if mf.is_file():
            out.append(name)
    return sorted(out)


def rebuild_one(conn, token: str, dcfg, ini: Path, *, verbose: bool = False) -> int:
    mf = ROOT / "digest_packs" / token / "manifest.json"
    if not mf.is_file():
        print(f"[skip] 无 manifest: {mf}")
        return -1
    items = (json.loads(mf.read_text(encoding="utf-8")) or {}).get("items") or []
    n = persist_digest_item_routes_for_token(
        conn, token, items, dcfg, config_path=ini, verbose=verbose
    )
    print(f"[ok] token={token[:8]}… items={len(items)} routes_rows={n}")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="重算 digest_item_presale_route（先删后插该 token）")
    ap.add_argument("tokens", nargs="*", help="32 位 hex，可多个；留空则处理 digest_packs 下全部")
    ap.add_argument("--all", action="store_true", help="同默认：扫描全部 digest 包")
    ap.add_argument(
        "--no-ai",
        action="store_true",
        help="禁用 AI 客户/机构匹配（大幅提速，适合刷新路由表）",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="打印每条售前匹配进度，并输出售前 AI 发送/接收（同 DISPATCH_PERSIST_VERBOSE / DISPATCH_AI_VERBOSE）",
    )
    args = ap.parse_args()

    ini = ROOT / "config.ini"
    dcfg = load_dispatch_config(ini)
    if args.no_ai:
        dcfg.enable_ai_customer_match = False

    if args.tokens:
        toks = [t.strip().lower() for t in args.tokens if t.strip()]
    else:
        toks = tokens_from_disk()

    if not toks:
        print("未找到可处理的 token（digest_packs/<token>/manifest.json）")
        sys.exit(1)

    conn = _connect()
    try:
        total = 0
        for t in toks:
            n = rebuild_one(conn, t, dcfg, ini, verbose=args.verbose)
            if n >= 0:
                total += n
        print(f"--- 完成，共处理 {len(toks)} 个摘要包，写入 route 行约 {total}（多售前多行）")
    finally:
        conn.close()
    if args.verbose:
        os.environ.pop("DISPATCH_PERSIST_VERBOSE", None)


if __name__ == "__main__":
    main()
