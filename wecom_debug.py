#!/usr/bin/env python3
"""
企业微信推送调试程序：单独运行，发送一条带固定链接的测试消息。

- 链接固定为：http://hb676400ly2.vicp.fun（文档文本提取器）
- 从 config.ini 的 [wecom] 读取 corp_id / agent_id / secret / to_user
- 先发一条「图文消息」（公众号风格），再发一条「文本消息」（含同链接），便于对比调试

用法：
  python wecom_debug.py
"""

import configparser
import json
import urllib.parse
import urllib.request
import os

# 调试用的固定链接（文档文本提取器）
DEBUG_URL = "http://hb676400ly2.vicp.fun"


def load_wecom_config() -> dict:
    """从 config.ini 读取 [wecom] 配置。"""
    cfg = configparser.ConfigParser()
    path = os.path.join(os.path.dirname(__file__), "config.ini")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"未找到配置文件: {path}")
    cfg.read(path, encoding="utf-8")
    if not cfg.has_section("wecom"):
        raise ValueError("config.ini 中缺少 [wecom] 段")
    w = cfg["wecom"]
    corp_id = w.get("corp_id", "").strip()
    agent_id_raw = w.get("agent_id", "").strip()
    secret = w.get("secret", "").strip()
    to_user = w.get("to_user", "").strip()
    if not corp_id or not agent_id_raw or not secret:
        raise ValueError("请在 [wecom] 中配置 corp_id、agent_id、secret")
    if not to_user:
        raise ValueError("请配置 to_user（接收人 UserID）")
    try:
        agent_id = int(agent_id_raw)
    except ValueError:
        agent_id = agent_id_raw
    return {
        "corp_id": corp_id,
        "agent_id": agent_id,
        "secret": secret,
        "to_user": to_user,
    }


def get_access_token(corp_id: str, secret: str) -> str | None:
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
        print(f"[调试] 获取 token 失败: {data.get('errmsg', data)}")
        return None
    except Exception as e:
        print(f"[调试] 获取 token 异常: {e}")
        return None


def _http_post_json(url: str, data: dict, timeout: int = 10) -> dict:
    """POST JSON，返回解析后的 JSON。"""
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_app_news(
    access_token: str,
    agent_id: int | str,
    to_user: str,
    title: str,
    description: str,
    url: str,
    picurl: str = "",
    timeout: int = 10,
) -> bool:
    """
    发送应用「图文消息」（类似公众号一条卡片，点击跳转 url）。
    """
    payload = {
        "touser": to_user,
        "msgtype": "news",
        "agentid": agent_id,
        "news": {
            "articles": [
                {
                    "title": title,
                    "description": description,
                    "url": url,
                }
            ]
        },
    }
    if picurl:
        payload["news"]["articles"][0]["picurl"] = picurl

    api_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
    try:
        ret = _http_post_json(api_url, payload, timeout=timeout)
        ok = ret.get("errcode") == 0
        if not ok:
            print(f"[调试] 图文消息发送失败: {ret.get('errmsg', ret)}")
        return ok
    except Exception as e:
        print(f"[调试] 图文消息请求异常: {e}")
        return False


def send_app_text(
    access_token: str,
    agent_id: int | str,
    to_user: str,
    content: str,
    timeout: int = 10,
) -> bool:
    """发送应用文本消息。"""
    payload = {
        "touser": to_user,
        "msgtype": "text",
        "agentid": agent_id,
        "text": {"content": content},
        "safe": 0,
    }
    api_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
    try:
        ret = _http_post_json(api_url, payload, timeout=timeout)
        ok = ret.get("errcode") == 0
        if not ok:
            print(f"[调试] 文本消息发送失败: {ret.get('errmsg', ret)}")
        return ok
    except Exception as e:
        print(f"[调试] 文本消息请求异常: {e}")
        return False


def main() -> None:
    print("=== 企业微信推送调试（链接: " + DEBUG_URL + "）===\n")

    try:
        cfg = load_wecom_config()
    except (FileNotFoundError, ValueError) as e:
        print(f"配置错误: {e}")
        return

    print(f"接收人 to_user: {cfg['to_user']}")
    print(f"应用 agent_id: {cfg['agent_id']}\n")

    token = get_access_token(cfg["corp_id"], cfg["secret"])
    if not token:
        print("无法继续：未获取到 access_token")
        return

    print("1. 发送「图文消息」（公众号风格，点击跳转上述链接）...")
    ok_news = send_app_news(
        token,
        cfg["agent_id"],
        cfg["to_user"],
        title="文档文本提取器 - 调试",
        description="点击打开：文档文本提取器 Web 版",
        url=DEBUG_URL,
    )
    print("   结果:", "成功" if ok_news else "失败")

    print("\n2. 发送「文本消息」（内容含同链接）...")
    text_content = f"【调试】文档文本提取器\n\n链接：{DEBUG_URL}"
    ok_text = send_app_text(token, cfg["agent_id"], cfg["to_user"], text_content)
    print("   结果:", "成功" if ok_text else "失败")

    print("\n=== 调试结束 ===")
    if ok_news or ok_text:
        print("请在企业微信中查看「应用」消息（接收人: " + cfg["to_user"] + "）。")
    else:
        print("两条均未发送成功，请检查应用可见范围是否包含该成员、网络与配置。")


if __name__ == "__main__":
    main()
