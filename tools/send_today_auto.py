#!/usr/bin/env python3
import json
from datetime import datetime
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from digest_message import (
    build_digest_pack_card_url,
    get_digest_payload_for_ingest_date,
    materialize_digest_pack,
)


def main() -> None:
    cfg = ROOT / "config.ini"
    today = datetime.now().strftime("%Y-%m-%d")
    title, description, digest_date = get_digest_payload_for_ingest_date(cfg, today)
    token = materialize_digest_pack(cfg, digest_date)

    import configparser

    cp = configparser.ConfigParser()
    cp.read(cfg, encoding="utf-8")
    base = "https://work.weixin.qq.com"
    if cp.has_section("wecom"):
        base = (cp["wecom"].get("disclose_page_url") or "").strip() or base
    card_url = build_digest_pack_card_url(base.rstrip("/"), token)

    pending = {
        "token": token,
        "digest_date": digest_date,
        "title": title,
        "description": description,
        "card_url": card_url,
        "disclose_page_url": base.rstrip("/"),
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "pipeline_note": "auto_send_today",
    }
    (ROOT / "digest_pending_send.json").write_text(
        json.dumps(pending, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    payload = json.dumps({"digest_token": token, "auto_route": True}, ensure_ascii=False)
    r = subprocess.run(
        [sys.executable, str(ROOT / "send_digest_wecom.py")],
        input=payload.encode("utf-8"),
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print(f"TOKEN={token}")
    print(r.stdout.decode("utf-8", errors="replace"))
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
