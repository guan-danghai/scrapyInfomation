#!/usr/bin/env python3
"""
企业微信推送模块：将爬取信息发送到企业微信群（机器人 Webhook）或个人/群（应用消息）。

支持两种方式：
1. 群机器人 Webhook：仅需 Webhook URL，消息发到该群
2. 应用消息：需 CorpId/AgentId/Secret，可发指定成员(userid)或群(chatid)

消息长度：企业微信单条文本建议不超过 2048 字节，本模块会自动截断并追加「…」。
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Union


# 单条消息内容最大长度（字节，UTF-8），企业微信文本消息限制 2048
MAX_TEXT_BYTES = 2048


def _truncate_text(text: str, max_bytes: int = MAX_TEXT_BYTES) -> str:
    """按 UTF-8 字节截断，避免超长导致 API 报错。"""
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # 从 max_bytes 往前找完整字符边界
    cut = encoded[:max_bytes]
    while cut and (cut[-1] & 0x80) and not (cut[-1] & 0x40):
        cut = cut[:-1]
    return cut.decode("utf-8", errors="ignore").rstrip() + "…"


def split_text_by_bytes(text: str, max_bytes: int = MAX_TEXT_BYTES, max_chunks: int = 20) -> list[str]:
    """
    按 UTF-8 字节将长文本切成多段，每段不超过 max_bytes，不拆断字符。
    最多返回 max_chunks 段，超出部分舍弃（最后一段带「…」）。
    """
    if not text or not text.strip():
        return []
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return [text.strip()]
    chunks: list[str] = []
    start = 0
    while start < len(encoded) and len(chunks) < max_chunks:
        end = min(start + max_bytes, len(encoded))
        cut = encoded[start:end]
        while len(cut) > 0 and (cut[-1] & 0x80) and not (cut[-1] & 0x40):
            cut = cut[:-1]
        chunk = cut.decode("utf-8", errors="ignore").strip()
        if chunk:
            chunks.append(chunk)
        start += len(cut)
    if start < len(encoded) and chunks:
        chunks[-1] = chunks[-1].rstrip() + "…"
    return chunks


def _http_post_json(url: str, data: dict, timeout: int = 10) -> dict:
    """POST JSON 到 URL，返回解析后的 JSON。"""
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_webhook(url: str, content: str, msg_type: str = "text", timeout: int = 10) -> bool:
    """
    通过群机器人 Webhook 发送消息（仅群）。

    :param url: Webhook 地址
    :param content: 文本内容（过长会自动截断）
    :param msg_type: "text" 或 "markdown"
    :param timeout: 请求超时秒数
    :return: 是否发送成功
    """
    if not url or not content:
        return False
    content = _truncate_text(content)
    if msg_type == "markdown":
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
    else:
        payload = {"msgtype": "text", "text": {"content": content}}

    try:
        ret = _http_post_json(url, payload, timeout=timeout)
        ok = ret.get("errcode") == 0
        if not ok:
            print(f"    [企业微信 Webhook] 发送失败: {ret.get('errmsg', ret)}")
        return ok
    except urllib.error.HTTPError as e:
        print(f"    [企业微信 Webhook] HTTP 错误: {e.code} {e.reason}")
        return False
    except Exception as e:
        print(f"    [企业微信 Webhook] 异常: {e}")
        return False


def get_access_token(corp_id: str, secret: str) -> Optional[str]:
    """获取企业微信应用 access_token。"""
    url = (
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        f"?corpid={urllib.parse.quote(corp_id)}&corpsecret={urllib.parse.quote(secret)}"
    )
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("errcode") == 0:
            return data.get("access_token")
        print(f"    [企业微信] 获取 token 失败: {data.get('errmsg', data)}")
        return None
    except Exception as e:
        print(f"    [企业微信] 获取 token 异常: {e}")
        return None


def send_app_textcard_result(
    access_token: str,
    agent_id: Union[int, str],
    title: str,
    description: str,
    url: str,
    btntxt: str = "详情",
    *,
    touser: Optional[str] = None,
    chatid: Optional[str] = None,
    timeout: int = 10,
) -> dict:
    """
    发送文本卡片，返回企业微信 API 结果。
    返回字段：ok、errcode、errmsg、raw（完整 JSON）。
    """
    if not access_token or not url:
        return {"ok": False, "errcode": -1, "errmsg": "缺少 access_token 或 url", "raw": {}}
    title = _truncate_text(title, max_bytes=128)
    description = _truncate_text(description, max_bytes=512)
    if len(btntxt.encode("utf-8")) > 4 * 4:  # 约 4 个汉字
        btntxt = "详情"
    payload = {
        "msgtype": "textcard",
        "agentid": agent_id,
        "textcard": {
            "title": title,
            "description": description,
            "url": url,
            "btntxt": btntxt[:4],
        },
        "safe": 0,
    }
    if chatid:
        payload["chatid"] = chatid
    if touser:
        payload["touser"] = touser
    if not chatid and not touser:
        print("    [企业微信应用] 未指定 touser 或 chatid")
        return {"ok": False, "errcode": -1, "errmsg": "未指定 touser 或 chatid", "raw": {}}

    api_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
    try:
        ret = _http_post_json(api_url, payload, timeout=timeout)
        ok = ret.get("errcode") == 0
        if not ok:
            print(f"    [企业微信应用 textcard] 发送失败: {ret.get('errmsg', ret)}")
        return {
            "ok": ok,
            "errcode": ret.get("errcode"),
            "errmsg": str(ret.get("errmsg") or ""),
            "raw": ret,
        }
    except Exception as e:
        print(f"    [企业微信应用 textcard] 异常: {e}")
        return {"ok": False, "errcode": -1, "errmsg": str(e), "raw": {}}


def send_app_textcard(
    access_token: str,
    agent_id: Union[int, str],
    title: str,
    description: str,
    url: str,
    btntxt: str = "详情",
    *,
    touser: Optional[str] = None,
    chatid: Optional[str] = None,
    timeout: int = 10,
) -> bool:
    """
    通过企业微信应用发送文本卡片（类似公众号一条卡片，点击跳转链接）。

    :param access_token: 来自 get_access_token
    :param agent_id: 应用 AgentId
    :param title: 卡片标题（建议不超过 128 字节）
    :param description: 卡片描述（建议不超过 512 字节），支持 <br> 换行
    :param url: 点击卡片跳转的链接（需可被企业微信客户端访问）
    :param btntxt: 按钮文字，默认「详情」，不超过 4 字
    :param touser: 成员 userid
    :param chatid: 群 chatid
    :return: 是否发送成功
    """
    return bool(
        send_app_textcard_result(
            access_token,
            agent_id,
            title,
            description,
            url,
            btntxt,
            touser=touser,
            chatid=chatid,
            timeout=timeout,
        ).get("ok")
    )


def send_app_message(
    access_token: str,
    agent_id: Union[int, str],
    content: str,
    *,
    touser: Optional[str] = None,
    chatid: Optional[str] = None,
    timeout: int = 10,
) -> bool:
    """
    通过企业微信应用发送文本消息给个人或群。

    :param access_token: 来自 get_access_token
    :param agent_id: 应用 AgentId
    :param content: 文本内容
    :param touser: 成员 userid；与 chatid 二选一或同时填
    :param chatid: 群 chatid
    :param timeout: 请求超时秒数
    :return: 是否发送成功
    """
    if not access_token or not content:
        return False
    content = _truncate_text(content)
    payload = {
        "msgtype": "text",
        "agentid": agent_id,
        "text": {"content": content},
        "safe": 0,
    }
    if chatid:
        payload["chatid"] = chatid
    if touser:
        payload["touser"] = touser
    if not chatid and not touser:
        print("    [企业微信应用] 未指定 touser 或 chatid")
        return False

    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
    try:
        ret = _http_post_json(url, payload, timeout=timeout)
        ok = ret.get("errcode") == 0
        if not ok:
            print(f"    [企业微信应用] 发送失败: {ret.get('errmsg', ret)}")
        return ok
    except Exception as e:
        print(f"    [企业微信应用] 异常: {e}")
        return False


def format_scrape_header(info_type: str, title: str, url: str) -> str:
    """仅格式化为标题+链接（用于第一条消息）。"""
    lines = [
        f"【{info_type}】",
        title,
        "",
        f"链接：{url}",
    ]
    return "\n".join(lines).strip()


def format_scrape_message(info_type: str, title: str, url: str, body_preview: str) -> str:
    """将一条爬取结果格式化为适合推送的文本（标题 + 链接 + 正文摘要，单条时用）。"""
    header = format_scrape_header(info_type, title, url)
    if not body_preview:
        return header
    return header + "\n\n" + body_preview.strip()


def notify_scrape_result(
    info_type: str,
    title: str,
    url: str,
    body_preview: str,
    *,
    webhook_url: Optional[str] = None,
    app_corp_id: Optional[str] = None,
    app_agent_id: Optional[int] = None,
    app_secret: Optional[str] = None,
    app_touser: Optional[str] = None,
    app_chatid: Optional[str] = None,
) -> bool:
    """
    根据配置发送一条爬取结果到企业微信（Webhook 或应用二选一）。

    若同时配置了 webhook 和应用，则两边都会发送。
    """
    text = format_scrape_message(info_type, title, url, body_preview or "")
    sent = False

    if webhook_url and webhook_url.strip():
        if send_webhook(webhook_url.strip(), text):
            sent = True

    if app_corp_id and app_agent_id and app_secret and (app_touser or app_chatid):
        token = get_access_token(app_corp_id, app_secret)
        if token and send_app_message(
            token, app_agent_id, text, touser=app_touser or None, chatid=app_chatid or None
        ):
            sent = True

    return sent


def notify_digest(
    title: str,
    description: str,
    url: str,
    *,
    webhook_url: Optional[str] = None,
    app_corp_id: Optional[str] = None,
    app_agent_id: Optional[Union[int, str]] = None,
    app_secret: Optional[str] = None,
    app_touser: Optional[str] = None,
    app_chatid: Optional[str] = None,
    btntxt: str = "查看",
    timeout: int = 10,
) -> bool:
    """
    发送一条「今日摘要」类公众号式文本卡片到企业微信（仅发一条，不逐条）。
    - 应用消息：textcard，点击跳转 url
    - Webhook：文本消息，内容为 title + description
    url 若为空，应用 textcard 会使用占位链接（企业微信要求必填）。
    """
    if not url or not url.strip():
        url = "https://work.weixin.qq.com"
    url = url.strip()
    sent = False

    if webhook_url and webhook_url.strip():
        # 与业务约定一致：标题换行后直接接「📌 今日概览」，中间不留空行
        text = f"{title}\n{description}"
        if send_webhook(webhook_url.strip(), text, timeout=timeout):
            sent = True

    if app_corp_id and app_agent_id and app_secret and (app_touser or app_chatid):
        token = get_access_token(app_corp_id, app_secret)
        if token and send_app_textcard(
            token,
            app_agent_id,
            title,
            description.replace("\n", "<br>"),
            url,
            btntxt=btntxt,
            touser=app_touser or None,
            chatid=app_chatid or None,
            timeout=timeout,
        ):
            sent = True

    return sent
