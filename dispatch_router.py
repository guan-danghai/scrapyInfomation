#!/usr/bin/env python3
"""按采购人匹配客户并路由到售前成员（数据源可为 crm_lead 或 sales_lead_confirm）。"""
from __future__ import annotations

import configparser
import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Optional

# sales_lead_confirm：「短名包含于长名」匹配的最短长度，防止「商业银行」等 4 字命中排序靠前的一条长 customer_name 而绑错售前
SALES_CONFIRM_SUBSTRING_MIN_LEN = 8


@dataclass
class DispatchConfig:
    lead_table: str = "crm_lead"
    lead_customer_col: str = "customer_name"
    lead_presale_names_col: str = "presale"
    lead_presale_userids_col: str = "presale_userids"
    lead_enabled_col: str = "enabled"
    """若为空则自动选 updated_at > created_at > id"""
    lead_time_order_col: str = ""
    """线索业务条线列名；存在且配置了 lead_exclude_business_lines 时，匹配售前会排除这些取值"""
    lead_business_line_col: str = "business_line"
    """逗号分隔，如：售后维护条线"""
    lead_exclude_business_lines: str = "售后维护条线"
    org_user_table: str = "org_user"
    org_user_name_col: str = "user_name"
    org_user_account_col: str = "user_account"
    org_user_deleted_col: str = "is_deleted"
    enable_ai_customer_match: bool = True
    ai_shortlist_size: int = 20
    #: 售前路由主数据：crm_lead | sales_lead_confirm（Excel 导入表，不按线索时间排序）
    presale_route_source: str = "crm_lead"
    sales_confirm_table: str = "sales_lead_confirm"
    sales_confirm_customer_col: str = "customer_name"
    sales_confirm_sales_col: str = "sales"
    sales_confirm_uid_col: str = ""
    sales_confirm_enabled_col: str = "enabled"
    #: True：允许子串/池解析等模糊匹配（易误绑）；False：仅 TRIM 全文一致或归一化完全一致才有售前
    sales_confirm_fuzzy_match: bool = False
    #: 关键词 → 固定企微 userid 列表（跳过 CRM 匹配），如 {"金融委员会办公室": ["lixiguo", "duanwangdong"]}
    keyword_presale_userids: dict[str, list[str]] = field(default_factory=dict)


def _split_multi(v: str) -> list[str]:
    if not v:
        return []
    return [x.strip() for x in re.split(r"[|,，;；/\s]+", v) if x.strip()]


def _parse_keyword_presale_userids(raw: str) -> dict[str, list[str]]:
    """格式：关键词=userid1|userid2，多项英文逗号分隔。"""
    out: dict[str, list[str]] = {}
    s = (raw or "").strip()
    if not s or s in ("-", "NONE", "none", "OFF", "off"):
        return out
    for part in re.split(r"[,，\n]+", s):
        part = part.strip()
        if not part or "=" not in part:
            continue
        kw, uids_raw = part.split("=", 1)
        kw = kw.strip()
        uids = _dedupe(_split_multi(uids_raw))
        if kw and uids:
            out[kw] = uids
    return out


def _normalize_owner_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return ""
    s = re.sub(r"[（(].*?[）)]", "", s)
    s = re.sub(r"[\s,，。、“”\"'`·\-_/\\]+", "", s)
    for token in (
        "股份有限公司",
        "有限责任公司",
        "有限公司",
        "分公司",
        "子公司",
        "营业部",
        "支行",
        "分行",
        "中心",
    ):
        s = s.replace(token, "")
    s = re.sub(r"(省|市|自治区|特别行政区)$", "", s)
    return s


def _name_to_pinyin_candidates(name_cn: str) -> list[str]:
    raw = (name_cn or "").strip()
    if not raw:
        return []
    try:
        from pypinyin import Style, lazy_pinyin

        pinyin = "".join(lazy_pinyin(raw, style=Style.NORMAL))
        compact = re.sub(r"[^a-zA-Z0-9]", "", pinyin).lower()
        if compact:
            return [compact]
    except Exception:
        pass
    fallback = re.sub(r"[^a-zA-Z0-9]", "", raw).lower()
    return [fallback] if fallback else []


def load_dispatch_config(config_file: Path) -> DispatchConfig:
    cfg = configparser.ConfigParser()
    cfg.read(config_file, encoding="utf-8")
    d = cfg["dispatch"] if cfg.has_section("dispatch") else {}
    def _to_bool(v: str, default: bool) -> bool:
        if v is None:
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

    def _presale_uid_col(raw: object) -> str:
        """`-` / NONE / 空：不使用单独 userid 列，仅用售前姓名列解析 org_user。"""
        if raw is None:
            return "presale_userids"
        s = str(raw).strip()
        if s in ("-", "NONE", "none", "OFF", "off", ""):
            return ""
        return s

    _names_default = "presale"
    _names_raw = (d.get("lead_presale_names_col") or "").strip()
    lead_presale_names_col = (_names_raw or _names_default).strip()

    def _sales_confirm_uid_col(raw: object) -> str:
        if raw is None:
            return ""
        s = str(raw).strip()
        if s in ("-", "NONE", "none", "OFF", "off", ""):
            return ""
        return s

    return DispatchConfig(
        lead_table=(d.get("lead_table") or "crm_lead").strip(),
        lead_customer_col=(d.get("lead_customer_col") or "customer_name").strip(),
        lead_presale_names_col=lead_presale_names_col,
        lead_presale_userids_col=_presale_uid_col(d.get("lead_presale_userids_col")),
        lead_enabled_col=(d.get("lead_enabled_col") or "enabled").strip(),
        lead_time_order_col=(d.get("lead_time_order_col") or "").strip(),
        lead_business_line_col=(d.get("lead_business_line_col") or "business_line").strip(),
        lead_exclude_business_lines=(d.get("lead_exclude_business_lines") or "售后维护条线").strip(),
        org_user_table=(d.get("org_user_table") or "org_user").strip(),
        org_user_name_col=(d.get("org_user_name_col") or "user_name").strip(),
        org_user_account_col=(d.get("org_user_account_col") or "user_account").strip(),
        org_user_deleted_col=(d.get("org_user_deleted_col") or "is_deleted").strip(),
        enable_ai_customer_match=_to_bool(d.get("enable_ai_customer_match"), True),
        ai_shortlist_size=max(5, int((d.get("ai_shortlist_size") or "20").strip() or "20")),
        presale_route_source=(d.get("presale_route_source") or "crm_lead").strip(),
        sales_confirm_table=(d.get("sales_confirm_table") or "sales_lead_confirm").strip(),
        sales_confirm_customer_col=(d.get("sales_confirm_customer_col") or "customer_name").strip(),
        sales_confirm_sales_col=(d.get("sales_confirm_sales_col") or "sales").strip(),
        sales_confirm_uid_col=_sales_confirm_uid_col(d.get("sales_confirm_uid_col")),
        sales_confirm_enabled_col=(d.get("sales_confirm_enabled_col") or "enabled").strip(),
        sales_confirm_fuzzy_match=_to_bool(d.get("sales_confirm_fuzzy_match"), False),
        keyword_presale_userids=_parse_keyword_presale_userids(d.get("keyword_presale_userids") or ""),
    )


def _excluded_business_line_tokens(dcfg: DispatchConfig) -> list[str]:
    raw = (dcfg.lead_exclude_business_lines or "").strip()
    if not raw or raw in ("-", "NONE", "none", "OFF", "off", "0"):
        return []
    return [x.strip() for x in re.split(r"[,，;；]", raw) if x.strip()]


def _lead_business_line_sql_condition(cols: set[str], dcfg: DispatchConfig) -> str:
    col = (dcfg.lead_business_line_col or "").strip()
    excl = _excluded_business_line_tokens(dcfg)
    if not col or col not in cols or not excl:
        return ""
    inn = ",".join(["'" + x.replace("'", "''") + "'" for x in excl])
    return f"TRIM(IFNULL(`{col}`,'')) NOT IN ({inn})"


def _fetch_crm_customers(conn, dcfg: DispatchConfig) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {dcfg.lead_table}")
        cols = {str(r.get("Field") or "").strip() for r in (cur.fetchall() or [])}
    parts: list[str] = []
    if dcfg.lead_enabled_col in cols:
        parts.append(f"(`{dcfg.lead_enabled_col}`=1 OR `{dcfg.lead_enabled_col}` IS NULL)")
    parts.append(f"`{dcfg.lead_customer_col}` IS NOT NULL AND TRIM(`{dcfg.lead_customer_col}`)<>''")
    bc = _lead_business_line_sql_condition(cols, dcfg)
    if bc:
        parts.append(bc)
    where_sql = "WHERE " + " AND ".join(parts)
    sql = f"SELECT DISTINCT `{dcfg.lead_customer_col}` AS c FROM `{dcfg.lead_table}` {where_sql}"
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []
    out = []
    for r in rows:
        c = (r.get("c") or "").strip()
        if c:
            out.append(c)
    return out


def _presale_ai_log_enabled() -> bool:
    """打印售前 AI 请求/响应：独立开关 DISPATCH_AI_VERBOSE，或与批量 verbose（DISPATCH_PERSIST_VERBOSE）一并开启。"""
    if os.environ.get("DISPATCH_AI_VERBOSE", "").strip().lower() in ("1", "true", "yes", "y", "on"):
        return True
    return os.environ.get("DISPATCH_PERSIST_VERBOSE", "").strip().lower() in ("1", "true", "yes")


def _truncate_for_log(s: str, limit: int = 8000) -> str:
    s = s or ""
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 3)] + "..."


def _ai_match_customer(owner_norm: str, candidates: list[str], config_path: Path) -> str:
    if not owner_norm or not candidates:
        return ""
    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    if not cfg.has_section("ai"):
        return ""
    api_key = (cfg["ai"].get("api_key") or "").strip()
    base_url = (cfg["ai"].get("base_url") or "").strip() or None
    model = (cfg["ai"].get("model") or "").strip()
    if not api_key or not model:
        return ""
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
        prompt = (
            "你是客户名称归并助手。给定采购人名称和CRM客户候选，请只返回JSON："
            '{"customer":"候选中的名称或空字符串","confidence":0到1}。'
            "如果无法确定，customer返回空字符串。"
        )
        user_content = {
            "owner_name": owner_norm,
            "candidate_customers": candidates,
        }
        user_body = json.dumps(user_content, ensure_ascii=False)
        msg_payload = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_body},
        ]
        log_ai = _presale_ai_log_enabled()
        if log_ai:
            print(
                "[presale_ai] kind=resolve_customer_pool "
                f"model={model} candidates_n={len(candidates)}\n"
                f"  send.system={_truncate_for_log(prompt)}\n"
                f"  send.user={_truncate_for_log(user_body)}",
                flush=True,
            )
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=msg_payload,
        )
        txt = (resp.choices[0].message.content or "").strip()
        choice0 = resp.choices[0]
        finish = getattr(choice0, "finish_reason", None)
        rid = getattr(resp, "id", None)
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            if log_ai:
                print(
                    "[presale_ai] kind=resolve_customer_pool "
                    f"resp.id={rid} finish_reason={finish}\n"
                    f"  recv.raw={_truncate_for_log(txt)}\n"
                    "  recv.parsed=(无JSON) accepted=False",
                    flush=True,
                )
            return ""
        obj = json.loads(m.group(0))
        customer = (obj.get("customer") or "").strip()
        conf = float(obj.get("confidence") or 0)
        accepted = customer in candidates and conf >= 0.7
        if log_ai:
            print(
                "[presale_ai] kind=resolve_customer_pool "
                f"resp.id={rid} finish_reason={finish}\n"
                f"  recv.raw={_truncate_for_log(txt)}\n"
                f"  recv.parsed={{customer={customer!r}, confidence={conf}}} "
                f"accepted={accepted}",
                flush=True,
            )
        if accepted:
            return customer
    except Exception as ex:
        if _presale_ai_log_enabled():
            print(
                f"[presale_ai] kind=resolve_customer_pool ERROR {type(ex).__name__}: {ex}",
                flush=True,
            )
        return ""
    return ""


def _ai_same_institution(purchaser: str, lead_customer: str, config_path: Path) -> bool:
    """采购人 vs 线索客户名称：简称/全称是否同一机构（与候选列表无关的单次判定）。"""
    a = (purchaser or "").strip()
    b = (lead_customer or "").strip()
    if not a or not b:
        return False
    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    if not cfg.has_section("ai"):
        return False
    api_key = (cfg["ai"].get("api_key") or "").strip()
    base_url = (cfg["ai"].get("base_url") or "").strip() or None
    model = (cfg["ai"].get("model") or "").strip()
    if not api_key or not model:
        return False
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=base_url)
        prompt = (
            "你是金融机构名称对齐助手。判断「采购人」与「CRM线索客户名称」是否指向同一法人主体或同一可认领客户"
            "（含总行/分行/简称与全称对应）。只返回JSON："
            '{"same":true或false,"confidence":0到1}。不确定则 same=false。'
        )
        user_content = {"purchaser": a, "crm_lead_customer_name": b}
        user_body = json.dumps(user_content, ensure_ascii=False)
        msg_payload = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_body},
        ]
        log_ai = _presale_ai_log_enabled()
        if log_ai:
            print(
                "[presale_ai] kind=same_institution "
                f"model={model}\n"
                f"  send.system={_truncate_for_log(prompt)}\n"
                f"  send.user={_truncate_for_log(user_body)}",
                flush=True,
            )
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=msg_payload,
        )
        txt = (resp.choices[0].message.content or "").strip()
        choice0 = resp.choices[0]
        finish = getattr(choice0, "finish_reason", None)
        rid = getattr(resp, "id", None)
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            if log_ai:
                print(
                    "[presale_ai] kind=same_institution "
                    f"resp.id={rid} finish_reason={finish}\n"
                    f"  recv.raw={_truncate_for_log(txt)}\n"
                    "  recv.parsed=(无JSON) accepted=False",
                    flush=True,
                )
            return False
        obj = json.loads(m.group(0))
        same_v = bool(obj.get("same"))
        conf = float(obj.get("confidence") or 0)
        accepted = same_v and conf >= 0.7
        if log_ai:
            print(
                "[presale_ai] kind=same_institution "
                f"resp.id={rid} finish_reason={finish}\n"
                f"  recv.raw={_truncate_for_log(txt)}\n"
                f"  recv.parsed={{same={same_v}, confidence={conf}}} accepted={accepted}",
                flush=True,
            )
        return accepted
    except Exception as ex:
        if _presale_ai_log_enabled():
            print(
                f"[presale_ai] kind=same_institution ERROR {type(ex).__name__}: {ex}",
                flush=True,
            )
        return False


def _lead_columns(conn, dcfg: DispatchConfig) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM `{dcfg.lead_table}`")
        return {str(r.get("Field") or "").strip() for r in (cur.fetchall() or [])}


def _lead_order_expression(cols: set[str], dcfg: DispatchConfig) -> str:
    """
    多条 crm 线索命中同一采购人时：ORDER BY 从新到旧取第一条带售前且匹配的。
    优先顺序：config lead_time_order_col → 表字段 register_date → updated_at/created_at → id。
    """
    if dcfg.lead_time_order_col and dcfg.lead_time_order_col in cols:
        return f"`{dcfg.lead_time_order_col}` DESC"
    if "register_date" in cols:
        return "`register_date` DESC"
    if "updated_at" in cols and "created_at" in cols:
        return "COALESCE(`updated_at`, `created_at`) DESC"
    if "updated_at" in cols:
        return "`updated_at` DESC"
    if "created_at" in cols:
        return "`created_at` DESC"
    if "id" in cols:
        return "`id` DESC"
    return "`id` DESC"


def _rules_match_owner_to_customer(
    po: str,
    lc: str,
    po_canon: str,
    *,
    substring_min_len: int = 4,
) -> bool:
    """
    仅用归一化相等 / 长短包含 / 与采购人已解析 canonical 对比，不调用 resolve_canonical_customer、不调用 AI。
    用于在逐行扫描线索表时先短路，避免每行都对 lc 做池解析（内部可能触发 AI）。

    substring_min_len：短串被包含时的最短长度；sales_lead_confirm 建议 8，
    避免「商业银行」等 4 字串命中排序靠前的一条很长 customer_name 而绑错售前。
    """
    po = (po or "").strip()
    lc = (lc or "").strip()
    if not po or not lc:
        return False
    smin = max(4, int(substring_min_len))
    if po_canon and po_canon.strip() == lc:
        return True
    pn = _normalize_owner_name(po)
    ln = _normalize_owner_name(lc)
    if pn and ln and pn == ln:
        return True
    if pn and ln:
        shorter, longer = (pn, ln) if len(pn) <= len(ln) else (ln, pn)
        if len(shorter) >= smin and (shorter in longer):
            return True
    if po_canon:
        cn = _normalize_owner_name(po_canon)
        if cn and ln:
            if cn == ln:
                return True
            shorter, longer = (cn, ln) if len(cn) <= len(ln) else (ln, cn)
            if len(shorter) >= smin and (shorter in longer):
                return True
        if cn and pn:
            if cn == pn:
                return True
            shorter, longer = (cn, pn) if len(cn) <= len(pn) else (pn, cn)
            if len(shorter) >= smin and (shorter in longer):
                return True
    return False


def _owner_matches_lead_customer(
    conn,
    project_owner: str,
    lead_customer: str,
    dcfg: DispatchConfig,
    *,
    config_path: Path,
    po_canon: str,
    lc_canon: str,
    ai_pair_cache: dict[tuple[str, str], bool],
    substring_min_len: int = 4,
) -> bool:
    po = (project_owner or "").strip()
    lc = (lead_customer or "").strip()
    if not po or not lc:
        return False
    if _rules_match_owner_to_customer(
        po, lc, po_canon, substring_min_len=substring_min_len
    ):
        return True
    if po_canon and lc_canon and po_canon == lc_canon:
        return True
    key = (po, lc)
    if key in ai_pair_cache:
        return ai_pair_cache[key]
    if dcfg.enable_ai_customer_match and config_path:
        same = _ai_same_institution(po, lc, config_path)
        ai_pair_cache[key] = same
        return same
    ai_pair_cache[key] = False
    return False


def _userids_from_presale_cells(conn, names_cell: str, uids_cell: str, dcfg: DispatchConfig) -> tuple[list[str], list[str]]:
    """
    返回 (企微 userid 列表, 售前中文名列表用于展示)。
    优先使用 presale_userids；否则用售前姓名查 org_user，未命中则拼音兜底。
    """
    raw_uids = _dedupe(_split_multi(uids_cell))
    if raw_uids:
        display_names: list[str] = []
        for u in raw_uids:
            dn = org_display_name_for_uid(conn, u, dcfg)
            if dn:
                display_names.append(dn)
        return raw_uids, _dedupe(display_names)

    presale_names = _dedupe(_split_multi(names_cell))
    if not presale_names:
        return [], []
    name_to_accounts = _lookup_org_user_accounts(conn, presale_names, dcfg)
    userids: list[str] = []
    for name in presale_names:
        mapped = name_to_accounts.get(name) or []
        if mapped:
            userids.extend(mapped)
        else:
            userids.extend(_name_to_pinyin_candidates(name))
    return _dedupe(userids), presale_names


def _pick_from_crm_lead_table(
    conn,
    project_owner: str,
    dcfg: DispatchConfig,
    *,
    config_path: Path,
    resolve_session: dict | None = None,
) -> dict | None:
    """
    crm_lead：线索按时间从新到旧扫描，第一条「采购人匹配该线索客户」且售前非空的记录生效。
    匹配：归并后一致 / 归一化一致 / 包含 /（可选）AI 同一机构。
    返回 {"canonical_customer","presale_display_names","userids"} 或 None。
    """
    po = (project_owner or "").strip()
    if not po:
        return None
    cols = _lead_columns(conn, dcfg)
    if dcfg.lead_customer_col not in cols:
        return None
    if dcfg.lead_presale_names_col not in cols and dcfg.lead_presale_userids_col not in cols:
        return None
    cfg_path = config_path
    po_canon = resolve_canonical_customer(conn, po, dcfg, config_path=cfg_path, _session=resolve_session) or ""
    order_expr = _lead_order_expression(cols, dcfg)
    wparts: list[str] = []
    if dcfg.lead_enabled_col in cols:
        wparts.append(f"(`{dcfg.lead_enabled_col}`=1 OR `{dcfg.lead_enabled_col}` IS NULL)")
    wparts.append(f"`{dcfg.lead_customer_col}` IS NOT NULL AND TRIM(`{dcfg.lead_customer_col}`)<>''")
    blc = _lead_business_line_sql_condition(cols, dcfg)
    if blc:
        wparts.append(blc)
    where_sql = "WHERE " + " AND ".join(wparts)
    select_parts = [f"`{dcfg.lead_customer_col}` AS `cust`"]
    if dcfg.lead_presale_names_col in cols:
        select_parts.append(f"`{dcfg.lead_presale_names_col}` AS `names`")
    if dcfg.lead_presale_userids_col in cols:
        select_parts.append(f"`{dcfg.lead_presale_userids_col}` AS `uids`")
    sql = (
        f"SELECT {', '.join(select_parts)} FROM `{dcfg.lead_table}` "
        f"{where_sql} "
        f"ORDER BY {order_expr}"
    )
    ai_cache: dict[tuple[str, str], bool] = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []
    for r in rows:
        lc = (r.get("cust") or "").strip()
        if not lc:
            continue
        names_cell = (r.get("names") or "").strip()
        uids_cell = (r.get("uids") or "").strip()
        if _rules_match_owner_to_customer(po, lc, po_canon):
            if not names_cell.strip() and not uids_cell.strip():
                continue
            uids, disp = _userids_from_presale_cells(conn, names_cell, uids_cell, dcfg)
            if not uids:
                continue
            canon_out = po_canon or lc
            return {
                "canonical_customer": canon_out,
                "presale_display_names": disp,
                "userids": uids,
            }
        lc_canon = resolve_canonical_customer(conn, lc, dcfg, config_path=cfg_path, _session=resolve_session) or ""
        if not _owner_matches_lead_customer(
            conn, po, lc, dcfg, config_path=cfg_path, po_canon=po_canon, lc_canon=lc_canon, ai_pair_cache=ai_cache
        ):
            continue
        if not names_cell.strip() and not uids_cell.strip():
            continue
        uids, disp = _userids_from_presale_cells(conn, names_cell, uids_cell, dcfg)
        if not uids:
            continue
        canon_out = lc_canon or po_canon or lc
        return {
            "canonical_customer": canon_out,
            "presale_display_names": disp,
            "userids": uids,
        }
    return None


def _sales_confirm_columns(conn, dcfg: DispatchConfig) -> set[str]:
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM `{dcfg.sales_confirm_table}`")
            return {str(r.get("Field") or "").strip() for r in (cur.fetchall() or [])}
    except Exception:
        return set()


def _fetch_sales_confirm_customer_pool(conn, dcfg: DispatchConfig) -> list[str]:
    cols = _sales_confirm_columns(conn, dcfg)
    cc = dcfg.sales_confirm_customer_col
    sc = dcfg.sales_confirm_sales_col
    if cc not in cols or sc not in cols:
        return []
    en = (dcfg.sales_confirm_enabled_col or "").strip()
    parts = [
        f"TRIM(`{cc}`)<>''",
        f"(`{sc}` IS NOT NULL AND TRIM(`{sc}`)<>'')",
    ]
    if en and en in cols:
        parts.insert(0, f"(`{en}`=1 OR `{en}` IS NULL)")
    wh = "WHERE " + " AND ".join(parts)
    sql = f"SELECT DISTINCT TRIM(`{cc}`) AS c FROM `{dcfg.sales_confirm_table}` {wh}"
    out: list[str] = []
    with conn.cursor() as cur:
        cur.execute(sql)
        for r in cur.fetchall() or []:
            v = (r.get("c") or "").strip()
            if v:
                out.append(v)
    return out


def _pick_sales_confirm_exact_trim_match(
    conn,
    po: str,
    dcfg: DispatchConfig,
    cols: set[str],
    cust_col: str,
    sales_col: str,
    uid_col: str,
    en_col: str,
) -> dict | None:
    """
    采购人字段与表里 customer_name 全文一致（TRIM）时直接用该行 sales，
    避免按名称长度降序扫描时先被错误的一条「长名称 + 模糊包含」抢走。
    """
    po = (po or "").strip()
    if not po:
        return None
    wparts = [
        f"TRIM(`{cust_col}`)<>''",
        f"(`{sales_col}` IS NOT NULL AND TRIM(`{sales_col}`)<>'')",
        f"TRIM(`{cust_col}`)=%s",
    ]
    if en_col and en_col in cols:
        wparts.insert(0, f"(`{en_col}`=1 OR `{en_col}` IS NULL)")
    where_sql = "WHERE " + " AND ".join(wparts)
    select_parts = [f"`{cust_col}` AS `cust`", f"`{sales_col}` AS `names`"]
    if uid_col and uid_col in cols:
        select_parts.append(f"`{uid_col}` AS `uids`")
    sql = (
        f"SELECT {', '.join(select_parts)} FROM `{dcfg.sales_confirm_table}` "
        f"{where_sql} ORDER BY `id` ASC LIMIT 1"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (po,))
        r = cur.fetchone()
    if not r:
        return None
    lc = (r.get("cust") or "").strip()
    names_cell = (r.get("names") or "").strip()
    uids_cell = (r.get("uids") or "").strip()
    if not names_cell and not uids_cell:
        return None
    uids, disp = _userids_from_presale_cells(conn, names_cell, uids_cell, dcfg)
    if not uids:
        return None
    return {
        "canonical_customer": lc or po,
        "presale_display_names": disp,
        "userids": uids,
    }


def _pick_sales_confirm_normalized_identity_match(
    conn,
    po: str,
    dcfg: DispatchConfig,
    cols: set[str],
    cust_col: str,
    sales_col: str,
    uid_col: str,
    en_col: str,
) -> dict | None:
    """
    采购人与 customer_name 经 _normalize_owner_name 后完全一致时命中（不做子串、不含糊等同）。
    用于 fuzzy_match 关闭时的第二条通路。
    """
    po_n = _normalize_owner_name(po)
    if not po_n:
        return None
    wparts = [
        f"TRIM(`{cust_col}`)<>''",
        f"(`{sales_col}` IS NOT NULL AND TRIM(`{sales_col}`)<>'')",
    ]
    if en_col and en_col in cols:
        wparts.insert(0, f"(`{en_col}`=1 OR `{en_col}` IS NULL)")
    where_sql = "WHERE " + " AND ".join(wparts)
    select_parts = [f"`{cust_col}` AS `cust`", f"`{sales_col}` AS `names`"]
    if uid_col and uid_col in cols:
        select_parts.append(f"`{uid_col}` AS `uids`")
    sql = (
        f"SELECT {', '.join(select_parts)} FROM `{dcfg.sales_confirm_table}` "
        f"{where_sql} ORDER BY `id` ASC"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []
    for r in rows:
        lc = (r.get("cust") or "").strip()
        if not lc:
            continue
        if _normalize_owner_name(lc) != po_n:
            continue
        names_cell = (r.get("names") or "").strip()
        uids_cell = (r.get("uids") or "").strip()
        if not names_cell and not uids_cell:
            continue
        uids, disp = _userids_from_presale_cells(conn, names_cell, uids_cell, dcfg)
        if not uids:
            continue
        return {
            "canonical_customer": lc or po,
            "presale_display_names": disp,
            "userids": uids,
        }
    return None


def pick_best_sales_confirm_for_owner(
    conn,
    project_owner: str,
    dcfg: DispatchConfig,
    *,
    config_path: Path,
    resolve_session: dict | None = None,
) -> dict | None:
    """
    仅从 sales_lead_confirm：
    1) TRIM(customer_name)=采购人；
    2) 归一化后与表中一行完全一致（无子串）；
    3) 若 config sales_confirm_fuzzy_match=true，再走池解析/子串等模糊逻辑（易误绑，默认关闭）。

    本路径不使用大模型判断「是否同一机构」：确认表路径内 enable_ai 已强制关闭。
    """
    po = (project_owner or "").strip()
    if not po:
        return None
    cols = _sales_confirm_columns(conn, dcfg)
    cust_col = dcfg.sales_confirm_customer_col
    sales_col = dcfg.sales_confirm_sales_col
    if cust_col not in cols or sales_col not in cols:
        return None
    uid_col = (dcfg.sales_confirm_uid_col or "").strip()
    en_col = (dcfg.sales_confirm_enabled_col or "").strip()
    exact = _pick_sales_confirm_exact_trim_match(
        conn, po, dcfg, cols, cust_col, sales_col, uid_col, en_col
    )
    if exact:
        return exact
    norm_hit = _pick_sales_confirm_normalized_identity_match(
        conn, po, dcfg, cols, cust_col, sales_col, uid_col, en_col
    )
    if norm_hit:
        return norm_hit
    if not dcfg.sales_confirm_fuzzy_match:
        return None
    cfg_path = config_path
    smin = SALES_CONFIRM_SUBSTRING_MIN_LEN
    # 与 crm_lead 不同：确认表是唯一依据，禁止 AI 把「表外采购人」绑到表内某条客户上
    dcfg_sc = replace(dcfg, enable_ai_customer_match=False)
    if resolve_session is not None:
        if "sales_confirm_pool" not in resolve_session:
            resolve_session["sales_confirm_pool"] = _fetch_sales_confirm_customer_pool(conn, dcfg)
        pool = resolve_session["sales_confirm_pool"]
    else:
        pool = _fetch_sales_confirm_customer_pool(conn, dcfg)
    po_canon = resolve_canonical_customer(
        conn,
        po,
        dcfg_sc,
        config_path=cfg_path,
        _session=resolve_session,
        customer_pool=pool,
    ) or ""

    wparts = [
        f"TRIM(`{cust_col}`)<>''",
        f"(`{sales_col}` IS NOT NULL AND TRIM(`{sales_col}`)<>'')",
    ]
    if en_col and en_col in cols:
        wparts.insert(0, f"(`{en_col}`=1 OR `{en_col}` IS NULL)")
    where_sql = "WHERE " + " AND ".join(wparts)
    select_parts = [f"`{cust_col}` AS `cust`", f"`{sales_col}` AS `names`"]
    if uid_col and uid_col in cols:
        select_parts.append(f"`{uid_col}` AS `uids`")
    order_sql = f"ORDER BY CHAR_LENGTH(TRIM(`{cust_col}`)) DESC, `id` ASC"
    sql = (
        f"SELECT {', '.join(select_parts)} FROM `{dcfg.sales_confirm_table}` "
        f"{where_sql} {order_sql}"
    )
    ai_cache: dict[tuple[str, str], bool] = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []
    for r in rows:
        lc = (r.get("cust") or "").strip()
        if not lc:
            continue
        names_cell = (r.get("names") or "").strip()
        uids_cell = (r.get("uids") or "").strip()
        if _rules_match_owner_to_customer(
            po, lc, po_canon, substring_min_len=smin
        ):
            if not names_cell.strip() and not uids_cell.strip():
                continue
            uids, disp = _userids_from_presale_cells(conn, names_cell, uids_cell, dcfg)
            if not uids:
                continue
            canon_out = po_canon or lc
            return {
                "canonical_customer": canon_out,
                "presale_display_names": disp,
                "userids": uids,
            }
        lc_canon = resolve_canonical_customer(
            conn,
            lc,
            dcfg_sc,
            config_path=cfg_path,
            _session=resolve_session,
            customer_pool=pool,
        ) or ""
        if not _owner_matches_lead_customer(
            conn,
            po,
            lc,
            dcfg_sc,
            config_path=cfg_path,
            po_canon=po_canon,
            lc_canon=lc_canon,
            ai_pair_cache=ai_cache,
            substring_min_len=smin,
        ):
            continue
        if not names_cell.strip() and not uids_cell.strip():
            continue
        uids, disp = _userids_from_presale_cells(conn, names_cell, uids_cell, dcfg)
        if not uids:
            continue
        canon_out = lc_canon or po_canon or lc
        return {
            "canonical_customer": canon_out,
            "presale_display_names": disp,
            "userids": uids,
        }
    return None


def pick_presale_for_owner(
    conn,
    project_owner: str,
    dcfg: DispatchConfig,
    *,
    config_path: Path,
    resolve_session: dict | None = None,
) -> dict | None:
    """按 config presale_route_source 选择 crm_lead 或 sales_lead_confirm。"""
    src = (dcfg.presale_route_source or "crm_lead").strip().lower()
    if src in ("sales_lead_confirm", "sales_confirm", "confirm", "xlsx"):
        return pick_best_sales_confirm_for_owner(
            conn, project_owner, dcfg, config_path=config_path, resolve_session=resolve_session
        )
    return _pick_from_crm_lead_table(
        conn, project_owner, dcfg, config_path=config_path, resolve_session=resolve_session
    )


def pick_best_crm_lead_for_owner(
    conn,
    project_owner: str,
    dcfg: DispatchConfig,
    *,
    config_path: Path,
    resolve_session: dict | None = None,
) -> dict | None:
    """兼容旧名：实际走 pick_presale_for_owner（见 presale_route_source）。"""
    return pick_presale_for_owner(
        conn, project_owner, dcfg, config_path=config_path, resolve_session=resolve_session
    )


def pick_presale_for_digest_item(
    conn,
    item: dict,
    dcfg: DispatchConfig,
    *,
    config_path: Path,
    resolve_session: dict | None = None,
) -> dict | None:
    """摘要条目售前：keyword 固定路由优先，否则按 project_owner 匹配线索。"""
    kw = str((item or {}).get("keyword") or "").strip()
    fixed_uids = (dcfg.keyword_presale_userids or {}).get(kw)
    if fixed_uids:
        uids = _dedupe(fixed_uids)
        names = [(org_display_name_for_uid(conn, u, dcfg) or u) for u in uids]
        return {
            "canonical_customer": kw,
            "presale_display_names": names,
            "userids": uids,
        }
    owner = str((item or {}).get("project_owner") or "").strip()
    if not owner:
        return None
    return pick_presale_for_owner(
        conn, owner, dcfg, config_path=config_path, resolve_session=resolve_session or {}
    )


def _digest_item_route_cache_key(item: dict, dcfg: DispatchConfig) -> str:
    kw = str((item or {}).get("keyword") or "").strip()
    if kw and (dcfg.keyword_presale_userids or {}).get(kw):
        return f"kw:{kw}"
    owner = str((item or {}).get("project_owner") or "").strip()
    if owner:
        return f"owner:{owner}"
    return ""


def resolve_canonical_customer(
    conn,
    owner_name: str,
    dcfg: DispatchConfig,
    *,
    config_path: Path | None = None,
    _session: dict | None = None,
    customer_pool: Optional[list[str]] = None,
) -> str:
    """
    _session: 单次批量路由时传入，缓存全量客户列表与各名称解析结果，避免重复查库与 AI。
    customer_pool: 若传入则用作候选客户列表（如 sales_lead_confirm 表）；否则从 crm_lead 拉取。
    """
    cache_key = (owner_name or "").strip()
    cache_ns = "resolve_pool" if customer_pool is not None else "resolve_crm"
    if _session is not None:
        rmap = _session.setdefault(cache_ns, {})
        if cache_key in rmap:
            return rmap[cache_key]
    owner_norm = _normalize_owner_name(owner_name)
    if not owner_norm:
        return ""
    if customer_pool is not None:
        crm_customers = list(customer_pool)
    elif _session is not None:
        if "crm_distinct" not in _session:
            _session["crm_distinct"] = _fetch_crm_customers(conn, dcfg)
        crm_customers = _session["crm_distinct"]
    else:
        crm_customers = _fetch_crm_customers(conn, dcfg)
    if not crm_customers:
        out = owner_norm
        if _session is not None:
            _session.setdefault(cache_ns, {})[cache_key] = out
        return out
    norm_map: dict[str, list[str]] = {}
    for c in crm_customers:
        n = _normalize_owner_name(c)
        if not n:
            continue
        norm_map.setdefault(n, []).append(c)
    if owner_norm in norm_map:
        out = norm_map[owner_norm][0]
        if _session is not None:
            _session.setdefault(cache_ns, {})[cache_key] = out
        return out
    contains = [c for c in crm_customers if _normalize_owner_name(c) in owner_norm or owner_norm in _normalize_owner_name(c)]
    if len(contains) == 1:
        out = contains[0]
        if _session is not None:
            _session.setdefault(cache_ns, {})[cache_key] = out
        return out
    if contains:
        shortlist = contains[: dcfg.ai_shortlist_size]
    else:
        # 没有显式包含关系时，用前缀交集做一个粗 shortlist
        shortlist = []
        for c in crm_customers:
            n = _normalize_owner_name(c)
            score = sum(1 for ch in set(owner_norm) if ch in n)
            if score > 1:
                shortlist.append((score, c))
        shortlist = [x[1] for x in sorted(shortlist, key=lambda t: t[0], reverse=True)[: dcfg.ai_shortlist_size]]
    if dcfg.enable_ai_customer_match and config_path:
        ai_customer = _ai_match_customer(owner_norm, shortlist, config_path)
        if ai_customer:
            if _session is not None:
                _session.setdefault(cache_ns, {})[cache_key] = ai_customer
            return ai_customer
    if _session is not None:
        _session.setdefault(cache_ns, {})[cache_key] = owner_norm
    return owner_norm


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for x in items:
        v = (x or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _lookup_org_user_accounts(conn, names: list[str], dcfg: DispatchConfig) -> dict[str, list[str]]:
    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        return {}
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {dcfg.org_user_table}")
        cols = {str(r.get("Field") or "").strip() for r in (cur.fetchall() or [])}
    required = {dcfg.org_user_name_col, dcfg.org_user_account_col}
    if not required.issubset(cols):
        return {}
    where_deleted = (
        f" AND ({dcfg.org_user_deleted_col}=0 OR {dcfg.org_user_deleted_col} IS NULL)"
        if dcfg.org_user_deleted_col in cols
        else ""
    )
    placeholders = ",".join(["%s"] * len(names))
    sql = (
        f"SELECT {dcfg.org_user_name_col} AS n, {dcfg.org_user_account_col} AS a "
        f"FROM {dcfg.org_user_table} "
        f"WHERE {dcfg.org_user_name_col} IN ({placeholders}){where_deleted}"
    )
    mapping: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        cur.execute(sql, tuple(names))
        rows = cur.fetchall() or []
    for r in rows:
        n = (r.get("n") or "").strip()
        a = (r.get("a") or "").strip()
        if not n or not a:
            continue
        mapping.setdefault(n, [])
        if a not in mapping[n]:
            mapping[n].append(a)
    return mapping


def org_display_name_for_uid(conn, userid: str, dcfg: DispatchConfig) -> str:
    """org_user 表：userid → 中文显示名（用于企微卡片兜底）。"""
    u = (userid or "").strip()
    if not u:
        return ""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT `{dcfg.org_user_name_col}` AS n FROM `{dcfg.org_user_table}` "
                f"WHERE `{dcfg.org_user_account_col}`=%s "
                f"AND (`{dcfg.org_user_deleted_col}`=0 OR `{dcfg.org_user_deleted_col}` IS NULL) "
                f"LIMIT 1",
                (u,),
            )
            row = cur.fetchone() or {}
        return (row.get("n") or "").strip()
    except Exception:
        return ""


def query_presale_userids(conn, canonical_customer: str, dcfg: DispatchConfig) -> list[str]:
    if not canonical_customer:
        return []
    cfg_path = Path(__file__).resolve().parent / "config.ini"
    src = (dcfg.presale_route_source or "crm_lead").strip().lower()
    if src in ("sales_lead_confirm", "sales_confirm", "confirm", "xlsx"):
        picked = pick_presale_for_owner(
            conn, canonical_customer.strip(), dcfg, config_path=cfg_path
        )
        if picked and picked.get("userids"):
            return _dedupe(list(picked.get("userids") or []))
        return []
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {dcfg.lead_table}")
        cols = {str(r.get('Field') or '').strip() for r in (cur.fetchall() or [])}

    has_names = dcfg.lead_presale_names_col in cols
    if not has_names:
        return []

    wparts2: list[str] = [f"{dcfg.lead_customer_col} IS NOT NULL"]
    if dcfg.lead_enabled_col in cols:
        wparts2.insert(0, f"({dcfg.lead_enabled_col} = 1 OR {dcfg.lead_enabled_col} IS NULL)")
    blc2 = _lead_business_line_sql_condition(cols, dcfg)
    if blc2:
        wparts2.append(blc2)
    wh2 = "WHERE " + " AND ".join(wparts2)
    sql = f"""
        SELECT {dcfg.lead_customer_col} AS customer, {dcfg.lead_presale_names_col} AS names
        FROM {dcfg.lead_table}
        {wh2}
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []
    target_norm = _normalize_owner_name(canonical_customer)
    userids: list[str] = []
    presale_names: list[str] = []
    for r in rows:
        customer_norm = _normalize_owner_name((r.get("customer") or "").strip())
        if customer_norm != target_norm:
            continue
        presale_names.extend(_split_multi((r.get("names") or "").strip()))
    presale_names = _dedupe(presale_names)
    name_to_accounts = _lookup_org_user_accounts(conn, presale_names, dcfg)
    for name in presale_names:
        mapped_accounts = name_to_accounts.get(name) or []
        if mapped_accounts:
            userids.extend(mapped_accounts)
        else:
            userids.extend(_name_to_pinyin_candidates(name))
    return _dedupe(userids)


def presale_display_names_for_customers(conn, canonical_customers: list[str], dcfg: DispatchConfig) -> list[str]:
    """
    按归并后的客户主品牌（与路由里 owners 一致），从线索或销售线索确认表汇总售前中文名，用于企微卡片文案。
    须与 build_user_routing 同一套 resolve：客户名常为全称，不能只靠 normalize 字符串相等。
    """
    cc = _dedupe([str(x).strip() for x in (canonical_customers or []) if x and str(x).strip()])
    if not cc:
        return []
    cfg_path = Path(__file__).resolve().parent / "config.ini"
    src = (dcfg.presale_route_source or "crm_lead").strip().lower()
    if src in ("sales_lead_confirm", "sales_confirm", "confirm", "xlsx"):
        out2: list[str] = []
        seen2: set[str] = set()
        for c in cc:
            picked = pick_presale_for_owner(conn, c, dcfg, config_path=cfg_path)
            if not picked:
                continue
            for nm in picked.get("presale_display_names") or []:
                if nm and nm not in seen2:
                    seen2.add(nm)
                    out2.append(nm)
        return out2
    cc_set = set(cc)
    cc_norms = {_normalize_owner_name(c) for c in cc}
    out: list[str] = []
    seen: set[str] = set()
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {dcfg.lead_table}")
        cols = {str(r.get("Field") or "").strip() for r in (cur.fetchall() or [])}
    if dcfg.lead_presale_names_col not in cols:
        return []
    wparts3: list[str] = [f"{dcfg.lead_customer_col} IS NOT NULL"]
    if dcfg.lead_enabled_col in cols:
        wparts3.insert(0, f"({dcfg.lead_enabled_col} = 1 OR {dcfg.lead_enabled_col} IS NULL)")
    blc3 = _lead_business_line_sql_condition(cols, dcfg)
    if blc3:
        wparts3.append(blc3)
    wh3 = "WHERE " + " AND ".join(wparts3)
    sql = (
        f"SELECT {dcfg.lead_customer_col} AS customer, {dcfg.lead_presale_names_col} AS names "
        f"FROM {dcfg.lead_table} {wh3}"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []
    for r in rows:
        raw = (r.get("customer") or "").strip()
        if not raw:
            continue
        canon = (resolve_canonical_customer(conn, raw, dcfg, config_path=cfg_path) or "").strip()
        if not canon:
            continue
        if canon not in cc_set and _normalize_owner_name(canon) not in cc_norms:
            continue
        for nm in _split_multi((r.get("names") or "").strip()):
            if nm and nm not in seen:
                seen.add(nm)
                out.append(nm)
    return out


def build_user_routing_from_items(
    conn,
    items: list[dict],
    dcfg: DispatchConfig,
    *,
    config_path: Path | None = None,
) -> tuple[dict[str, dict], dict[str, object]]:
    """
    返回 (per_user 路由, 本批聚合):
    {
      "useridA": {"owners": [...], "item_count": 3, "presale_display_names": [...]},
    }
    以及 digest_stats: matched_item_count, aggregate_owners, aggregate_presale_names
    每条 manifest 按 keyword 固定路由或采购人选客户（crm_lead 时按线索时间新→旧；sales_lead_confirm 时按 Excel 表名字长优先），再路由到 userid。
    """
    cfg = config_path or (Path(__file__).resolve().parent / "config.ini")
    routing: dict[str, dict] = {}
    item_pick_cache: dict[str, dict | None] = {}
    resolve_session: dict = {}
    matched_item_count = 0
    for it in items or []:
        cache_key = _digest_item_route_cache_key(it, dcfg)
        if not cache_key:
            continue
        if cache_key not in item_pick_cache:
            item_pick_cache[cache_key] = pick_presale_for_digest_item(
                conn, it, dcfg, config_path=cfg, resolve_session=resolve_session
            )
        picked = item_pick_cache[cache_key]
        if not picked:
            continue
        matched_item_count += 1
        customer = (picked.get("canonical_customer") or "").strip()
        presale_names = list(picked.get("presale_display_names") or [])
        uids = picked.get("userids") or []
        for uid in uids:
            if uid not in routing:
                routing[uid] = {"owners": set(), "item_count": 0, "presale_display_names": set()}
            if customer:
                routing[uid]["owners"].add(customer)
            routing[uid]["item_count"] += 1
            for nm in presale_names:
                if nm:
                    routing[uid]["presale_display_names"].add(nm)
    for uid in list(routing.keys()):
        routing[uid]["owners"] = sorted(list(routing[uid]["owners"]))
        ps = routing[uid].get("presale_display_names") or set()
        routing[uid]["presale_display_names"] = sorted(list(ps))
    agg_own = sorted({o for m in routing.values() for o in (m.get("owners") or [])})
    agg_pre = sorted({p for m in routing.values() for p in (m.get("presale_display_names") or [])})
    digest_stats: dict[str, object] = {
        "matched_item_count": matched_item_count,
        "aggregate_owners": agg_own,
        "aggregate_presale_names": agg_pre,
    }
    return routing, digest_stats


def ensure_digest_item_presale_route_table(conn) -> None:
    """摘要页点击时用：digest_token + record_id → 售前 userid，生成 manifest 时写入。"""
    sql = """
    CREATE TABLE IF NOT EXISTS digest_item_presale_route (
      digest_token CHAR(32) NOT NULL COMMENT '摘要包目录名',
      record_id BIGINT NOT NULL COMMENT 'scraping_infos.id',
      presale_userid VARCHAR(128) NOT NULL COMMENT '企业微信 userid',
      presale_display_name VARCHAR(500) DEFAULT NULL COMMENT '售前中文名展示',
      canonical_customer VARCHAR(500) DEFAULT NULL COMMENT '归并客户名',
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (digest_token, record_id, presale_userid),
      KEY idx_dt_uid (digest_token, presale_userid)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='摘要条目售前映射（生成摘要时预计算）'
    """
    with conn.cursor() as cur:
        cur.execute(sql)


def persist_digest_item_routes_for_token(
    conn,
    digest_token: str,
    items: list[dict],
    dcfg: DispatchConfig,
    *,
    config_path: Path,
    verbose: bool = False,
) -> int:
    """
    跑批生成 manifest 后调用：按采购人 pick_best，展开为 (digest_token, record_id, presale_userid) 行。
    整 token 先删后插。返回插入行数。
    verbose=True 或环境变量 DISPATCH_PERSIST_VERBOSE=1：打印每条进度与首次匹配耗时；
    同时会开启售前 AI 请求/响应日志（亦可单独设 DISPATCH_AI_VERBOSE=1）。
    """
    tok = (digest_token or "").strip().lower()
    if not re.match(r"^[a-f0-9]{32}$", tok):
        return 0
    raw_items = items or []
    n_manifest = len(raw_items)
    skipped_no_route = 0
    for _it in raw_items:
        try:
            _rid = int((_it or {}).get("id") or 0)
        except (TypeError, ValueError):
            _rid = 0
        if _rid < 1 or not _digest_item_route_cache_key(_it, dcfg):
            skipped_no_route += 1
    v = bool(verbose) or (
        os.environ.get("DISPATCH_PERSIST_VERBOSE", "").strip().lower() in ("1", "true", "yes")
    )
    # 显式 verbose=True 时同步打开售前 AI 日志（与 DISPATCH_PERSIST_VERBOSE 共用开关）
    if bool(verbose):
        os.environ["DISPATCH_PERSIST_VERBOSE"] = "1"
    ensure_digest_item_presale_route_table(conn)
    resolve_session: dict = {}
    item_pick_cache: dict[str, dict | None] = {}
    batch: list[tuple] = []
    if v:
        src = (dcfg.presale_route_source or "crm_lead").strip()
        ai_on = "开" if dcfg.enable_ai_customer_match else "关"
        print(
            f"[persist] digest_token={tok[:8]}… manifest条数={n_manifest} "
            f"无路由条件≈{skipped_no_route} 数据源={src} AI匹配={ai_on}",
            flush=True,
        )
    t_wall = time.perf_counter()
    for idx, it in enumerate(raw_items, 1):
        try:
            rid = int((it or {}).get("id") or 0)
        except (TypeError, ValueError):
            rid = 0
        cache_key = _digest_item_route_cache_key(it, dcfg)
        if rid < 1 or not cache_key:
            continue
        cache_hit = cache_key in item_pick_cache
        if not cache_hit:
            t0 = time.perf_counter()
            item_pick_cache[cache_key] = pick_presale_for_digest_item(
                conn, it, dcfg, config_path=config_path, resolve_session=resolve_session
            )
            elapsed = time.perf_counter() - t0
            picked = item_pick_cache[cache_key]
            if v:
                label = cache_key[3:] if cache_key.startswith(("kw:", "owner:")) else cache_key
                label_short = label if len(label) <= 72 else label[:69] + "…"
                if picked:
                    pnames = "、".join((picked.get("presale_display_names") or [])[:6])
                    nu = len(picked.get("userids") or [])
                    print(
                        f"[persist] [{idx}/{n_manifest}] id={rid} 路由(首次)「{label_short}」 "
                        f"{elapsed:.2f}s → 命中 售前={pnames} userid数={nu}",
                        flush=True,
                    )
                else:
                    print(
                        f"[persist] [{idx}/{n_manifest}] id={rid} 路由(首次)「{label_short}」 "
                        f"{elapsed:.2f}s → 未命中线索",
                        flush=True,
                    )
        picked = item_pick_cache[cache_key]
        if v and cache_hit and idx % 25 == 0:
            print(
                f"[persist] …进度 manifest行 {idx}/{n_manifest} id={rid}（路由已缓存，跳过重复匹配）",
                flush=True,
            )
        if not picked:
            continue
        canon = (picked.get("canonical_customer") or "").strip() or None
        uids = list(picked.get("userids") or [])
        names = list(picked.get("presale_display_names") or [])
        for i, uid in enumerate(uids):
            u = (uid or "").strip()
            if not u:
                continue
            dn = (names[i] if i < len(names) else "") or ""
            dn = dn.strip() or (org_display_name_for_uid(conn, u, dcfg) or "").strip() or None
            batch.append((tok, rid, u, dn, canon))
    if v:
        matched_keys = sum(1 for x in item_pick_cache.values() if x)
        print(
            f"[persist] 完成 唯一路由键={len(item_pick_cache)} 匹配成功={matched_keys} "
            f"路由INSERT行数={len(batch)} 耗时 {time.perf_counter() - t_wall:.1f}s",
            flush=True,
        )
    with conn.cursor() as cur:
        cur.execute("DELETE FROM `digest_item_presale_route` WHERE `digest_token`=%s", (tok,))
        if batch:
            cur.executemany(
                "INSERT INTO `digest_item_presale_route` "
                "(`digest_token`,`record_id`,`presale_userid`,`presale_display_name`,`canonical_customer`) "
                "VALUES (%s,%s,%s,%s,%s)",
                batch,
            )
    try:
        conn.commit()
    except Exception:
        pass
    return len(batch)
