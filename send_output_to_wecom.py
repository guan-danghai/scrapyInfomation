#!/usr/bin/env python3
"""
将指定 output 子目录下已爬取的 txt 文件，按条发送到企业微信群（仅 Webhook）。

用法：
  python send_output_to_wecom.py
  python send_output_to_wecom.py "output/国开证券反洗钱-人行受益人系统接入改造项目（验签服务器）(GXTC-C-25050204)招标公告"
"""
import os
import re
import sys
import time
import configparser
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.ini"

# 默认发送目录（与 config 里关键词对应的子目录名）
DEFAULT_DIR = "国开证券反洗钱-人行受益人系统接入改造项目（验签服务器）(GXTC-C-25050204)招标公告"


def load_config() -> configparser.ConfigParser:
    """读取 config.ini。"""
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg


def load_wecom_webhook(cfg: configparser.ConfigParser) -> str:
    """从已读配置中取 [wecom] webhook_url。"""
    if not cfg.has_section("wecom"):
        return ""
    return cfg["wecom"].get("webhook_url", "").strip()


def get_default_subdir(cfg: configparser.ConfigParser) -> str:
    """从 [scraper] keywords 取第一个关键词作为默认发送子目录名。"""
    if cfg.has_section("scraper") and cfg["scraper"].get("keywords"):
        first = cfg["scraper"].get("keywords", "").strip().split(",")[0].strip()
        if first:
            return first
    return DEFAULT_DIR


def parse_saved_txt(filepath: Path) -> tuple[str, str, str, str] | None:
    """
    解析 scraper 保存的 txt 格式，返回 (info_type, title, url, body)。
    若解析失败返回 None。
    """
    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception as e:
        print(f"  跳过（读文件失败）: {filepath.name} -> {e}")
        return None

    title = ""
    info_type = ""
    url = ""
    lines = text.split("\n")
    for line in lines:
        line_stripped = line.strip()
        if line_stripped.startswith("标题："):
            title = line_stripped[3:].strip()
        elif line_stripped.startswith("类型："):
            info_type = line_stripped[3:].strip()
        elif line_stripped.startswith("来源："):
            url = line_stripped[3:].strip()
        elif line_stripped.startswith("=" * 20):
            break

    # 正文：优先用 "换行+一行等号+换行" 分隔符之后的内容；若无则用「仅等号的一行」之后；再否则用「类型：」行之后
    body = ""
    parts = re.split(r"\n=+\n", text, maxsplit=1)
    if len(parts) > 1:
        body = parts[1].strip()
    if not body:
        for i, ln in enumerate(lines):
            if re.match(r"^=+$", ln.strip()):
                body = "\n".join(lines[i + 1 :]).strip()
                break
    if not body and info_type:
        for i, ln in enumerate(lines):
            if ln.strip().startswith("类型："):
                body = "\n".join(lines[i + 1 :]).strip()
                break

    if not title and not url:
        print(f"  跳过（无法解析标题/来源）: {filepath.name}")
        return None
    if not info_type:
        info_type = "采招信息"
    return info_type, title, url, body


def main():
    try:
        import wecom_notify
    except ImportError:
        print("错误：未找到 wecom_notify 模块，请确保 wecom_notify.py 在项目根目录。")
        sys.exit(1)

    cfg = load_config()
    webhook_url = load_wecom_webhook(cfg).strip()

    # 应用消息配置
    w = cfg["wecom"] if cfg.has_section("wecom") else {}
    corp_id = w.get("corp_id", "").strip()
    agent_id = w.get("agent_id", "").strip()
    secret = w.get("secret", "").strip()
    to_user = w.get("to_user", "").strip()
    to_chatid = w.get("to_chatid", "").strip()

    use_webhook = bool(webhook_url)
    use_app = bool(corp_id and agent_id and secret and (to_user or to_chatid))

    if not use_webhook and not use_app:
        print("错误：config.ini 中 [wecom] 未配置有效发送方式。请填写 webhook_url，或 corp_id/agent_id/secret 及 to_user 或 to_chatid。")
        sys.exit(1)

    if len(sys.argv) > 1:
        subdir = sys.argv[1].strip().strip('"')
    else:
        subdir = get_default_subdir(cfg)

    output_dir = cfg["scraper"].get("output_dir", "output").strip() if cfg.has_section("scraper") else "output"
    out_dir = ROOT / output_dir / subdir
    if not out_dir.is_dir():
        print(f"错误：目录不存在 -> {out_dir}")
        sys.exit(1)

    txt_files = sorted(out_dir.glob("*.txt"))
    if not txt_files:
        print(f"该目录下没有 .txt 文件 -> {out_dir}")
        sys.exit(0)

    mode = "Webhook" if use_webhook else "应用消息"
    print(f"企业微信推送（{mode}）")
    print(f"目录：{out_dir}")
    print(f"共 {len(txt_files)} 条，开始发送…\n")

    # 若用应用消息，预先取 token
    access_token = None
    if use_app:
        access_token = wecom_notify.get_access_token(corp_id, secret)
        if not access_token:
            print("错误：获取企业微信 access_token 失败。")
            sys.exit(1)

    sent = 0
    for f in txt_files:
        parsed = parse_saved_txt(f)
        if not parsed:
            continue
        info_type, title, url, body = parsed
        body_raw = (body or "").strip()
        body_len_char = len(body_raw)
        body_len_bytes = len(body_raw.encode("utf-8")) if body_raw else 0
        print(f"  [日志] 文件: {f.name}")
        print(f"  [日志] 正文: {body_len_char} 字 / {body_len_bytes} 字节")

        # 第一条：类型+标题+链接
        msg_header = wecom_notify.format_scrape_header(info_type, title, url)
        ok1 = False
        if use_webhook and wecom_notify.send_webhook(webhook_url, msg_header):
            ok1 = True
        if use_app and access_token and wecom_notify.send_app_message(
            access_token, agent_id, msg_header, touser=to_user or None, chatid=to_chatid or None
        ):
            ok1 = True
        print(f"  [日志] 标题条: {'成功' if ok1 else '失败'}", flush=True)

        # 正文：按 2048 字节分多条发送（最多 20 条），单次请求超时 5 秒
        body_chunks = wecom_notify.split_text_by_bytes(body_raw, max_chunks=20)
        print(f"  [日志] 正文块数: {len(body_chunks)}", flush=True)
        if body_chunks:
            print(f"  [日志] 准备发送正文（超时 5 秒/条）…", flush=True)
        ok2 = True
        for i, chunk in enumerate(body_chunks):
            print(f"  [日志] 正在发送正文块 {i + 1}/{len(body_chunks)}…", flush=True)
            sys.stdout.flush()
            if i > 0:
                time.sleep(1)
            chunk_ok = True
            try:
                if use_webhook:
                    chunk_ok = wecom_notify.send_webhook(webhook_url, chunk, timeout=5) and chunk_ok
                if use_app and access_token:
                    chunk_ok = wecom_notify.send_app_message(
                        access_token, agent_id, chunk,
                        touser=to_user or None, chatid=to_chatid or None, timeout=5
                    ) and chunk_ok
            except Exception as e:
                print(f"  [日志] 正文块 {i + 1} 异常: {e}", flush=True)
                chunk_ok = False
            ok2 = chunk_ok and ok2
            status = "成功" if chunk_ok else "失败"
            print(f"  [日志] 正文块 {i + 1}/{len(body_chunks)}: {status} ({len(chunk)} 字)", flush=True)
        total_msgs = 1 + len(body_chunks)
        if ok1:
            sent += 1
            print(f"  [已发送] {title[:50]}…（共 {total_msgs} 条消息）")
        else:
            print(f"  [发送失败] {title[:50]}…")
        print()

    print(f"\n完成：成功 {sent}/{len(txt_files)} 条。")


if __name__ == "__main__":
    main()
