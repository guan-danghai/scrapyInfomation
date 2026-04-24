#!/usr/bin/env python3
"""
招投标信息 AI 分析模块：
1. product_related：监管报送 / IT系统建设 / 软件相关 / 其它科技相关
2. sub_type：招标、中标、流标、公示、征集、磋商、谈判、询价等
3. 从正文抽取与 scraping_infos 表对应的字段（项目编号、预算、中标金额、业主、联系方式等）
"""

import json
import re
from pathlib import Path
from typing import Any, Optional

# 入库审核状态（scraping_infos.audit_status）
AUDIT_STATUS_PENDING = "待审核"
AUDIT_STATUS_APPROVED = "审核通过"

# 与 scraper.PDF_SCAN_ONLY_MSG 一致：PDF 无文字层或 OCR 未识别时的正文占位
PDF_SCAN_NO_TEXT_PLACEHOLDER = "[PDF 无可提取文本，可能是扫描版图片]"

# 与 scraper 详情「正文过短」阈值一致：过短视为无实质公告正文，入库待审核
DETAIL_MIN_LEN_FOR_AUDIT = 80


def is_short_detail_for_pending(content: Optional[str]) -> bool:
    """detail 正文 strip 后长度 < DETAIL_MIN_LEN_FOR_AUDIT → 待审核（与爬虫 <80 触发 PDF 补抓对齐）。"""
    if content is None:
        return True
    if not isinstance(content, str):
        content = str(content)
    return len(content.strip()) < DETAIL_MIN_LEN_FOR_AUDIT


def is_placeholder_detail_content(text: Optional[str]) -> bool:
    """
    详情页仅有跳转外链、无实质正文时（如「点击查看>>(信息来自中国招标投标公共服务平台)」），
    返回 True，入库标记为待审核，供次日重爬或人工补全。
    较长正文若仅页脚含来源句，不判为占位。
    """
    if not text or not isinstance(text, str):
        return False
    raw = text.strip()
    if not raw:
        return False
    compact = re.sub(r"\s+", "", raw)
    if len(compact) > 2000:
        return False
    # 已含中标/评审等实质段落时，不因正文引用「中国招标投标公共服务平台」等媒体名误判为占位
    if len(compact) >= 120 and any(
        m in compact
        for m in (
            "中标人名称",
            "中标人：",
            "评标委员会",
            "评审日期",
            "评审地点",
            "中标金额",
            "公示期限",
            "成交结果",
        )
    ):
        return False
    jump = any(
        x in compact
        for x in ("点击查看", "点击查阅", "点击打开", "请点击", "查看详情>>", "查阅详情")
    )
    # 勿用「中国招标投标公共服务」裸匹配：合法正文常见「中国招标投标公共服务平台」，子串命中会造成误标待审核
    source = any(
        x in compact
        for x in (
            "信息来自中国招标投标",
            "信息来自全国招标投标",
            "信息来自中国招标投标公共服务平台",
            "(信息来自中国招标投标公共服务平台)",
            "全国公共资源交易平台",
        )
    )
    platform = any(
        x in compact
        for x in ("服务平台", "公共服务平台", "招标投标网", "公共资源交易")
    )
    if not platform:
        return False
    if jump or source:
        if len(compact) > 900:
            return False
        return True
    return False


def is_pdf_scan_placeholder_content(text: Optional[str]) -> bool:
    """
    详情正文为扫描版 PDF、无法抽取文字时的占位（或与 scraper 追加的 OCR 安装说明拼接）。
    返回 True 时入库 audit_status=待审核，与外链占位、附件壳正文一致，供次日补爬或人工补全。
    """
    if not text or not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False
    return s == PDF_SCAN_NO_TEXT_PLACEHOLDER or s.startswith(
        PDF_SCAN_NO_TEXT_PLACEHOLDER
    )


def is_attachment_only_shell_content(text: Optional[str]) -> bool:
    """
    采招网等：正文实际在 PDF，页面上只剩「内容详见附件」+ 附件下载 / 点击下载等壳。
    与「具体内容详见附件《xxx》」的长技术标书区分：首行壳句 + 去空白后总长 ≤200，
    或 200～400 字且含「招标进展」与多条附件入口文案（采招网壳+时间线）。
    返回 True 时入库 audit_status=待审核，供补爬或人工补全。
    """
    if not text or not isinstance(text, str):
        return False
    raw = text.strip()
    if not raw:
        return False
    compact = re.sub(r"\s+", "", raw)
    if len(compact) > 2000:
        return False
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    first = lines[0] if lines else ""
    starts_shell = first.startswith(
        ("内容详见附件", "详情请见附件", "详情见附件", "详见附件")
    )
    ui_hits = sum(
        1
        for u in ("附件下载", "点击下载", "条附件信息可下载", "附件信息可下载")
        if u in compact
    )
    if not starts_shell or ui_hits < 1:
        return False
    if len(compact) <= 200:
        return True
    if 200 < len(compact) <= 400 and "招标进展" in compact and ui_hits >= 2:
        return True
    return False


# 默认配置文件与提示词路径
_CONFIG_FILE = Path(__file__).resolve().parent / "config.ini"
_PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "bid_analyze.txt"


def _load_ai_config(config_path: Path) -> dict:
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    section = "ai"
    if not cfg.has_section(section):
        return {"api_key": "", "base_url": "", "model": "deepseek-chat"}
    s = cfg[section]
    return {
        "api_key": s.get("api_key", "").strip(),
        "base_url": (s.get("base_url", "").strip() or None),
        "model": s.get("model", "deepseek-chat").strip(),
    }


def _load_prompt_template() -> str:
    """从 prompts/bid_analyze.txt 读取提示词，占位符为 {title}、{content}；文件不存在时使用内置默认。"""
    if _PROMPT_FILE.is_file():
        return _PROMPT_FILE.read_text(encoding="utf-8")
    return (
        "你是一个招投标信息分析助手。根据下面给出的【标题】和【正文】，完成两件事：\n"
        "1) 判断该条信息是否与以下类别相关（可多选，用逗号分隔；若都不相关则留空）：\n"
        "   - 监管报送\n   - IT系统建设\n   - 软件相关\n   - 其它科技相关\n"
        "2) 判断细分类型 sub_type，只能选一个：招标、中标、流标、公示、中标候选人公示、成交结果、征集、磋商、谈判、询价、邀请、采购、其他\n"
        "3) 从正文中尽量抽取以下字段（没有的留空字符串）：\n"
        "   project_no, project_budget, winning_amount, bidding_method, project_owner, owner_contact, owner_phone,\n"
        "   winning_bidder, winning_bidder_contact, winning_bidder_phone, bidding_agent, bid_deadline, published_at,\n"
        "   province, city, district\n"
        "请只输出一个 JSON 对象，不要其他说明，字段名用英文，日期格式 YYYY-MM-DD 或 YYYY-MM-DD HH:mm。\n"
        "【标题】\n{title}\n\n【正文】\n{content}"
    )


# 规则：从标题推断 sub_type（细分类型）
# 注意：「结果/成交」类必须排在单纯的采购方式词前面，
# 否则「谈判结果公示」会被「谈判」先命中，误判为采购方式。
SUB_TYPE_KEYWORDS = [
    # ── 结果类（优先）──────────────────────────────────────
    ("中标结果", "中标结果"),       # 平台标签「中标结果」
    ("成交结果", "成交结果"),
    ("中标候选人公示", "中标候选人公示"),
    ("候选人公示", "中标候选人公示"),
    ("谈判结果", "中标结果"),       # 竞争性谈判结果 → 中标结果
    ("询价结果", "中标结果"),       # 询价结果 → 中标结果
    ("磋商结果", "中标结果"),       # 竞争性磋商结果 → 中标结果
    ("邀请结果", "中标结果"),
    ("结果公示", "中标结果"),       # 兜底：XX结果公示
    ("结果公告", "中标结果"),       # 兜底：XX结果公告
    # ── 中标/流标 ─────────────────────────────────────────
    ("中标公告", "中标结果"),
    ("中标", "中标结果"),
    ("流标", "流标"),
    ("废标", "流标"),
    # ── 公示类 ────────────────────────────────────────────
    ("公示", "公示"),
    # ── 采购方式（招标/征集/磋商/谈判/询价/邀请/采购）────────
    ("征集", "征集"),
    ("磋商", "磋商"),
    ("谈判", "谈判"),
    ("询价", "询价"),
    ("招标", "招标"),
    ("邀请", "邀请"),
    ("采购", "采购"),
]


def infer_sub_type_from_title(title: str) -> str:
    """从标题推断 sub_type，按优先级匹配第一个关键词。"""
    if not title:
        return ""
    for keyword, sub_type in SUB_TYPE_KEYWORDS:
        if keyword in title:
            return sub_type
    return "其他"


# 规则：从标题+正文推断是否与科技相关，并给出 product_related 标签
# 优先级：软件相关 > 硬件相关（先命中先返回，每条记录只取一个分类）
TECH_CATEGORIES = [
    ("软件相关", [
        # 系统/平台/开发类
        "软件", "系统开发", "系统建设", "系统升级", "系统改造", "系统迁移", "系统集成",
        "信息系统", "应用系统", "业务系统", "核心系统", "中台",
        "平台建设", "平台开发", "数据平台", "信息平台", "业务平台",
        # ↓ 补充：裸"平台"（各类业务平台、服务平台均属软件范畴）
        "平台",
        # 维保类（系统/平台/数据库的维保属于软件运维）
        "系统维保", "平台维保", "数据库维保", "软件维保",
        # 数据库
        "数据库",
        # AI / 大模型（算力「平台/服务」侧；算力设备见硬件类与规则覆盖）
        "大模型", "算力平台", "算力服务",
        # 服务/运维类
        "软件开发", "开发项目", "运维", "信息化", "数字化", "电子化",
        # 监管报送专项
        "监管报送", "报送系统", "监管报表", "监管数据",
        # 数据/安全类（主要为服务/系统形态）
        "数据治理", "数据仓库", "数据安全", "网络安全", "安全运营",
    ]),
    ("硬件相关", [
        # 服务器/存储/计算
        "服务器", "算力设备", "存储设备", "存储系统", "工作站", "计算节点",
        # 网络/安全设备
        "交换机", "路由器", "防火墙", "负载均衡", "网络设备", "安全设备",
        # 机房/基础设施
        "机房", "数据中心", "UPS", "基础硬件", "硬件设备",
        # 终端/外设
        "PC机", "电脑采购", "打印机", "终端设备", "网络改造", "机房改造",
    ]),
]

# 仅凭标题判断时：命中下列任一则视为「非科技」（与 bid_analyze 二、product_related 必须留空 对应）
TECH_TITLE_EXCLUDE = [
    "联名卡", "信用卡场景", "获客活客", "场景获客", "生日祝福", "权益兑换",
    "物资供应商", "物资采购", "物资供应", "盒抽", "办公用品", "礼品", "服装",
    "招租", "公开招租", "场地租赁", "停车位", "绿植", "物业管理", "保洁", "食堂食材",
    "餐厅食材", "食材采购", "食材供应", "食材框架", "食堂承包",
    "客户经理外包", "营销团队外包", "驻点服务外包",
    "补充医疗保险", "医疗保险", "保险采购", "医疗采购", "健康险", "保险代理",
    "品牌广告", "广告采购", "公用媒体库", "影院广告", "媒体库采购", "广告媒体",
    "监理", "设计供应商入围", "税务咨询", "档案整理", "档案数字化", "档案扫描",
    "呼叫中心", "客服外包", "催收", "不良资产", "审计", "评估", "法律咨询",
    "基金代销", "理财销售", "培训", "团建", "会议服务", "押运", "物料", "用车",
    "车辆租赁", "维修", "印刷品", "宣传品", "安防工程", "安防系统建设", "监控设备", "消防工程",
    "基建工程", "电梯", "空调", "发电机", "柜员机", "ATM", "清分机", "点钞机",
    "现金清分", "现金押运", "后勤与保障", "绿化养护", "续租", "装修", "驾驶员",
    # 建筑/工程类（科技中心/园区的建设施工，非IT）
    "施工总承包", "施工承包", "（施工）", "(施工)", "园区项目", "二期工程", "一期工程", "工程施工",
    "科技大厦", "科技园", "科技楼", "大厦",
    # 资产转让/拍卖（非采购）
    "资产转让", "拍卖",
    # 硬件设备（非IT系统）
    "大堂引导", "引导分流", "分流设备", "叫号", "排队机",
    # 学校/政府非IT资产管理
    "小学", "中学", "幼儿园", "学校资产", "行政事业资产",
    # 教育机构名称中含「科技」但项目本身为非IT金融/服务类
    "职业技术学院", "职业学院", "学院",
    # 金融机构间合作/服务，非IT采购
    "金融业务合作", "银行合作", "业务合作服务", "合作服务项目",
    # 公告类型（非IT采购公告）
    "暂停公告",
    # 营销类平台（非IT系统建设）
    "餐饮", "立减", "云闪付",
]

# 绝对排除词：命中后即使招标人是金融机构也不放行（描述的是项目类型，不是机构类型）
HARD_TITLE_EXCLUDE = {
    "餐饮", "立减", "云闪付",
    "食材", "食堂", "物资", "礼品", "服装", "绿植", "保洁", "广告", "印刷品",
    # 土建/机电施工类：标题含「数据中心」也会被硬件词命中，须硬排除避免误判为硬件采购
    "（施工）", "(施工)",
    # 弱电/实体安防施工：标题常含「系统建设」子串，易误判为软件；金融机构招标亦须排除
    "安防系统建设",
}

# 标题级硬件强制词：标题中出现则直接归为硬件相关，优先于软件关键词
# 用于解决"XX平台相关设备采购"被误判为软件的情况
HARDWARE_TITLE_OVERRIDE = {
    "设备采购", "硬件采购", "设备购置", "硬件购置",
    "设备维保", "硬件维保",   # 硬件设备维保属于硬件
}


def is_tech_related_by_title(title: str) -> bool:
    """
    仅根据标题判断是否与科技相关（与 prompts/bid_analyze.txt 二、product_related 一致）。
    - 监管报送、IT系统建设、软件/信息系统、其它科技 → 视为科技相关
    - 营销/物资/场地/人力外包/保险/广告/监理/档案等 → 视为非科技，返回 False
    """
    if not (title or "").strip():
        return False
    t = title.strip()
    # 先排除：命中「必须留空」类关键词则直接判为非科技
    for kw in TECH_TITLE_EXCLUDE:
        if kw in t:
            return False
    # 再判断：是否命中任一科技类关键词
    for _label, keywords in TECH_CATEGORIES:
        if any(kw in t for kw in keywords):
            return True
    return False


# 工程测绘类（多测合一、竣工测量等）：非信息系统/非软硬件采购；正文中「竣工指标数据库转换」易误命中软件词「数据库」
_SURVEY_NON_IT_MARKERS = (
    "多测合一",
    "规划监督测量",
    "验测高程",
    "验测平面位置",
    "不动产测绘",
    "规划核实测量",
    "人防核实测量",
    "竣工指标数据库转换",
    "地下管线探测",
    "环境竣工测量",
)


def _winning_amount_paren_numeric(content: str) -> Optional[str]:
    """正文「中标金额」等标签后、括号内的 ¥/￥ 阿拉伯数字（与大写金额并列时优先）。"""
    if not (content or "").strip():
        return None
    m = re.search(
        r"(?:中标金额|中标价|成交金额|成交价格)[：:][\s\S]{0,800}?[（(]\s*[￥¥]\s*([\d,]+\.?\d*)\s*[）)]",
        content,
    )
    if not m:
        return None
    return m.group(1).strip() + "元"


def _is_engineering_survey_non_it(title: str, content: str) -> bool:
    """标题或正文体现工程测绘/多测合一时，不按 IT 软硬件归类。"""
    tw = ((title or "") + "\n" + (content or "")).strip()
    if not tw:
        return False
    return any(m in tw for m in _SURVEY_NON_IT_MARKERS)


def _rules_override_product_related(title: str, content: str) -> Optional[str]:
    """强规则覆盖 AI/关键词扫描。返回 None 表示不干预；'' 或 '硬件相关' 为最终结果。"""
    if _is_engineering_survey_non_it(title, content):
        return ""
    tw = ((title or "") + "\n" + (content or "")).strip()
    if not tw:
        return None
    # 机房/互联网专线、线路租用 — 电信服务，非软硬件采购
    if any(
        k in tw
        for k in (
            "线路租用",
            "专线租用",
            "互联网线路租用",
            "网络线路租用",
            "数据专线租用",
            "光纤线路租用",
        )
    ):
        return ""
    # 算力设备、大模型/算力场景下的设备扩容 → 硬件
    if "算力设备" in tw:
        return "硬件相关"
    if "设备扩容" in tw and ("大模型" in tw or "算力" in tw):
        return "硬件相关"
    # 「XX软件服务器及配件采购」：克服裸词「软件」先于「服务器」命中
    if re.search(r"服务器.{0,20}(及配件|配件采购|及配件采购)", tw):
        return "硬件相关"
    return None


# 金融机构关键词：招标人/采购人包含这些词时，视为金融机构发起的项目
FINANCIAL_OWNER_KEYWORDS = [
    "银行", "信托", "证券", "基金", "保险", "金融", "理财", "资产管理",
    "财务公司", "期货", "租赁", "农商", "农信", "农村信用", "消费金融",
    "汽车金融", "金融控股", "金融租赁", "信用社", "联合社",
]


def _has_financial_owner(content: str, title: str = "") -> bool:
    """从正文提取招标人/采购人，判断是否为金融机构。

    优先级：
    1. 正文中找到招标人字段 → 看是否含金融机构词
    2. 正文中找不到招标人 → 用标题里的金融机构词兜底
    3. 都没有 → 保守处理，返回 False（沿用标题排除）
    """
    if content:
        m = re.search(r'(?:招标人|采购人|委托单位|项目业主|建设单位)[：:]\s*([^\n]{2,40})', content)
        if m:
            owner = m.group(1).strip()
            return any(kw in owner for kw in FINANCIAL_OWNER_KEYWORDS)
    # 正文中提取不到招标人 → 从标题判断
    if title and any(kw in title for kw in FINANCIAL_OWNER_KEYWORDS):
        return True
    # 都无法判断 → 保守处理
    return False


def infer_product_related_from_text(title: str, content: str) -> str:
    """从标题和正文推断 product_related，只返回一个分类（软件相关 / 硬件相关）。

    优先级：软件相关 > 硬件相关（TECH_CATEGORIES 顺序即优先级）。
    逻辑：
    1. 标题命中排除词 → 非科技，BUT 若正文中招标人是金融机构则放行继续分析
    2. 标题未命中排除词 → 扫描标题+正文关键词，返回第一个命中的分类
    """
    rule_ov = _rules_override_product_related(title, content)
    if rule_ov is not None:
        return rule_ov
    t = (title or "").strip()
    if t:
        for kw in TECH_TITLE_EXCLUDE:
            if kw in t:
                # 绝对排除词（项目类型级别）：无论招标人是否为金融机构都不放行
                if kw in HARD_TITLE_EXCLUDE:
                    return ""
                # 普通排除词（机构类型级别）：招标人是金融机构时放行
                if not _has_financial_owner(content, t):
                    return ""
                break  # 金融机构发起 → 跳出排除检查，继续扫关键词
        # 标题明确是"设备采购/硬件采购"时，直接归硬件，不被软件关键词覆盖
        if any(kw in t for kw in HARDWARE_TITLE_OVERRIDE):
            return "硬件相关"
        # 标题含"维保"且软件相关词出现在"维保"之前 → 说明维保对象是软件系统
        # 反例：「硬件设备维保-系统第三方」里"系统"在"维保"之后，语义无关，不触发
        _SW_MAINT = {"系统", "平台", "软件", "数据库", "应用", "模块"}
        if "维保" in t:
            weibao_pos = t.index("维保")
            if any(0 <= t.find(ind) < weibao_pos for ind in _SW_MAINT):
                return "软件相关"
    text = t + "\n" + (content or "")
    if not text.strip():
        return ""
    # 标题优先扫描：标题比正文更可靠，避免正文噪音（如机房搬迁项目正文提到"系统迁移"导致误判）
    for label, keywords in TECH_CATEGORIES:
        if any(kw in t for kw in keywords):
            return label
    # 标题无法判断时，再扫正文内容（兜底）
    for label, keywords in TECH_CATEGORIES:
        if any(kw in (content or "") for kw in keywords):
            return label
    return ""


def _extract_simple_fields(content: str) -> dict:
    """简单规则抽取：项目编号、预算、中标金额、业主、代理、联系人等，供无 API 时兜底。"""
    out = {}
    if not content:
        return out
    # 项目编号
    m = re.search(r"项目编号[：:]\s*([^\s\n]+)", content)
    if not m:
        m = re.search(r"招标编号[：:]\s*([^\s\n]+)", content)
    if m:
        out["project_no"] = m.group(1).strip()
    # 预算：只匹配明确标签后的金额，不做裸扫避免误命中
    _budget_patterns = [
        r"项目预算[：:]\s*([^\n]+?)(?:\n|$)",
        r"预算金额[：:]\s*([^\n]+?)(?:\n|$)",
        r"采购预算[：:]\s*([^\n]+?)(?:\n|$)",
        r"控制价[：:]\s*([^\n]+?)(?:\n|$)",
        r"最高限价[：:]\s*([^\n]+?)(?:\n|$)",
        r"预算[：:]\s*([^\n]+?)(?:\n|$)",
    ]
    for pat in _budget_patterns:
        m = re.search(pat, content)
        if m:
            out["project_budget"] = m.group(1).strip()
            break
    # 中标金额：优先取「中标金额/价」附近括号内的 ¥/￥ 阿拉伯数字（常见：大写金额后接（¥ 3,320,000.00））
    _win_label = r"(?:中标金额|中标价|成交金额|成交价格)"
    m = re.search(
        _win_label + r"[：:][\s\S]{0,800}?[（(]\s*[￥¥]\s*([\d,]+\.?\d*)\s*[）)]",
        content,
    )
    if not m:
        m = re.search(
            _win_label + r"[：:]\s*[￥¥]\s*([\d,\.]+)\s*元?",
            content,
        )
    if not m:
        m = re.search(r"中标金额[：:]\s*[￥¥]?\s*([\d,\.]+)\s*元?", content)
    if not m:
        m = re.search(r"中标价[：:]\s*[￥¥]?\s*([\d,\.]+)\s*元?", content)
    if m:
        out["winning_amount"] = m.group(1).strip() + "元"
    # 招标方式
    for way in ["公开招标", "竞争性磋商", "竞争性谈判", "询价", "邀请招标"]:
        if way in content:
            out["bidding_method"] = way
            break
    # 项目业主/委托单位
    m = re.search(r"(?:委托单位|采购人|项目业主)[：:]\s*([^\n]+)", content)
    if m:
        out["project_owner"] = m.group(1).strip()
    # 中标人
    m = re.search(r"中标人(?:名称)?[：:]\s*([^\n]+)", content)
    if m:
        out["winning_bidder"] = re.sub(r"\s*企业联系方式\s*$", "", m.group(1).strip())
    # 招标代理
    m = re.search(r"(?:代理机构|招标代理)[：:]\s*([^\n]+)", content)
    if m:
        out["bidding_agent"] = m.group(1).strip()
    # 联系人、电话（取前两组常见表述）
    m = re.search(r"(?:项目联系人|联系人)[：:]\s*([^\n]+)", content)
    if m:
        out["owner_contact"] = m.group(1).strip()
    m = re.search(r"联系电话[：:]\s*([^\n]+)", content)
    if m:
        out["owner_phone"] = m.group(1).strip()
    return out


def _normalize_ai_result(data: Any) -> Optional[dict]:
    """
    模型有时返回 JSON 数组（如 [{...}]），统一成单条 dict 供后续 .get 使用。
    取列表中第一个 dict；无可用 dict 则返回 None。
    """
    if data is None:
        return None
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                return item
        return None
    return None


def analyze_with_ai(title: str, content: str, config_path: Optional[Path] = None) -> Optional[dict]:
    """
    调用大模型分析标题和正文，返回与 scraping_infos 表字段对应的 JSON 结构。
    若未配置 api_key 或调用失败，返回 None，调用方用规则兜底。
    """
    config_path = config_path or _CONFIG_FILE
    cfg = _load_ai_config(config_path)
    api_key = cfg.get("api_key")
    if not api_key:
        return None

    prompt_template = _load_prompt_template()
    body = prompt_template.format(
        title=title or "",
        content=(content or "")[:12000],
    )
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=cfg.get("base_url"))
        resp = client.chat.completions.create(
            model=cfg.get("model", "deepseek-chat"),
            messages=[{"role": "user", "content": body}],
            temperature=0.1,
        )
        text = (resp.choices[0].message.content or "").strip()
        # 取第一个 ```json ... ``` 或整段当作 JSON
        if "```json" in text:
            text = re.sub(r"^.*?```json\s*", "", text)
            text = re.sub(r"\s*```.*$", "", text)
        elif "```" in text:
            text = re.sub(r"^.*?```\s*", "", text)
            text = re.sub(r"\s*```.*$", "", text)
        return _normalize_ai_result(json.loads(text))
    except Exception:
        return None


def _parse_owner_party_is_financial_institution(ai_result: Optional[dict]) -> Optional[bool]:
    """解析 AI 返回的采购/业主主体是否金融机构。缺字段返回 None（兼容未含该字段的旧提示词）。"""
    ai_result = _normalize_ai_result(ai_result)
    if not ai_result or "owner_party_is_financial_institution" not in ai_result:
        return None
    v = ai_result.get("owner_party_is_financial_institution")
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v) and v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1", "是"):
            return True
        if s in ("false", "no", "0", "否"):
            return False
    return None


# 采购主体含以下子串则视为「可能金融机构」，规则兜底时不按机关关键词跳过（避免误伤）
_FINANCIAL_OWNER_MARKERS = (
    "银行",
    "农商行",
    "农商银行",
    "农村商业银行",
    "农信",
    "信用社",
    "信用联社",
    "农村信用社",
    "省联社",
    "村镇银行",
    "保险",
    "人寿",
    "财险",
    "证券",
    "信托",
    "金融租赁",
    "融资租赁",
    "消费金融",
    "财务公司",
    "资产管理公司",
    "理财有限责任公司",
    "理财公司",
    "金控",
    "基金管理",
    "期货",
    "小额贷款",
    "融资担保",
    "汽车金融",
    "中国人民银行",
    "人民银行",
)

# 典型非金融行政机关/基层单位（招标人常为「XX民政局」「XX人民政府」等）
_GOVERNMENT_NON_FINANCIAL_MARKERS = (
    "民政局",
    "财政局",
    "教育局",
    "体育局",
    "司法局",
    "审计局",
    "统计局",
    "信访局",
    "人力资源和社会保障局",
    "人社局",
    "社会保障局",
    "医疗保障局",
    "医保局",
    "卫生健康委员会",
    "卫生健康局",
    "卫健委",
    "疾病预防控制中心",
    "疾控中心",
    "自然资源局",
    "自然资源和规划局",
    "规划和自然资源局",
    "生态环境局",
    "住房和城乡建设局",
    "住房城乡建设局",
    "住建局",
    "交通运输局",
    "交通局",
    "水利局",
    "农业农村局",
    "商务局",
    "市场监督管理局",
    "市场监管局",
    "文化和旅游局",
    "文物局",
    "广播电视局",
    "退役军人事务局",
    "应急管理局",
    "外事办公室",
    "政务服务",
    "政务服务和数据",
    "数据局",
    "人民政府",
    "政府办公室",
    "区政府",
    "县政府",
    "市政府",
    "省政府",
    "街道办事处",
    "街道办",
    "村民委员会",
    "居民委员会",
    "机关事务管理局",
    "城市管理",
    "综合执法局",
    "公安局",
    "公安厅",
    "民政厅",
    "财政厅",
    "教育厅",
    "消防救援",
    "消防支队",
    "公积金管理中心",
    "住房公积金管理中心",
    "博物馆",
    "图书馆",
    "文化馆",
    "档案馆",
)


def extract_owner_text_for_financial_check(content: str, project_owner: str) -> str:
    """优先用已抽取的 project_owner，否则从正文常见字段解析招标人/采购人一行。"""
    o = (project_owner or "").strip()
    if o:
        return o
    if not content:
        return ""
    m = re.search(
        r"(?:招标人|采购人|项目业主|建设单位|委托单位)\s*[：:]\s*([^\n\r]+)",
        content[:12000],
    )
    return m.group(1).strip() if m else ""


def should_skip_ingest_non_financial_owner(
    ai_result: Optional[dict],
    title: str,
    content: str,
    project_owner: str,
) -> tuple[bool, str]:
    """
    用于「非金融采购主体不入库」：返回 (是否跳过, 原因简述)。
    规则优先：标题+采购人描述中命中典型机关且不含银行/保险等金融关键词 → 跳过（防止 AI 误判 true）。
    其次：AI 明确 false → 跳过。
    """
    owner_text = extract_owner_text_for_financial_check(content, project_owner)
    blob = f"{owner_text}\n{title or ''}"
    if not blob.strip():
        fi_only = _parse_owner_party_is_financial_institution(ai_result)
        if fi_only is False:
            return True, "AI判定采购主体非金融机构"
        return False, ""
    if any(m in blob for m in _FINANCIAL_OWNER_MARKERS):
        fi = _parse_owner_party_is_financial_institution(ai_result)
        if fi is False:
            return True, "AI判定采购主体非金融机构"
        return False, ""
    if any(g in blob for g in _GOVERNMENT_NON_FINANCIAL_MARKERS):
        return True, "规则:采购人/标题含行政机关等非金融主体"
    fi = _parse_owner_party_is_financial_institution(ai_result)
    if fi is False:
        return True, "AI判定采购主体非金融机构"
    return False, ""


def build_scraping_info_row(
    raw: dict,
    keyword: str = "银行",
    info_type: str = "采招信息",
    ai_result: Optional[dict] = None,
) -> dict[str, Any]:
    """
    将一条爬取 JSON 与 AI 分析结果合并，得到一条 scraping_infos 表行（字段名为 snake_case，与表一致）。
    raw 需包含 title, content, url, crawl_time 等。
    """
    ai_result = _normalize_ai_result(ai_result)
    title = raw.get("title") or ""
    content = raw.get("content") or ""
    url = raw.get("url") or ""
    crawl_time = raw.get("crawl_time") or ""

    # 1) sub_type：完全来自列表页 .ssjg-leixing 标签，不做任何标题推断降级
    #    若爬取时未拿到平台标签（info_type 为空或"采招信息"），保持原值，不猜测
    sub_type = (info_type or "").strip() or "采招信息"

    # 2) product_related
    if ai_result and ai_result.get("product_related") is not None:
        product_related = (ai_result.get("product_related") or "").strip()
    else:
        product_related = infer_product_related_from_text(title, content)
    # 测绘/线路租用/算力设备与服务器配件等强规则，覆盖 AI 与关键词扫描
    rule_ov = _rules_override_product_related(title, content)
    if rule_ov is not None:
        product_related = rule_ov

    # 3) 其它抽取字段：优先 AI，否则规则
    simple = _extract_simple_fields(content)
    if ai_result:
        for k in ["project_no", "project_budget", "winning_amount", "bidding_method",
                  "project_owner", "owner_contact", "owner_phone",
                  "winning_bidder", "winning_bidder_contact", "winning_bidder_phone",
                  "bidding_agent", "bid_deadline", "published_at", "province", "city", "district"]:
            v = ai_result.get(k)
            if v is not None and str(v).strip():
                simple[k] = str(v).strip()
    # 中标金额：正文若含（¥ 3,320,000.00）等形式，统一取括号内数字（覆盖中文大写）
    wa_num = _winning_amount_paren_numeric(content)
    if wa_num:
        simple["winning_amount"] = wa_num
    # 日期字符串转标准格式
    def norm_date(s: str) -> Optional[str]:
        if not s or not isinstance(s, str):
            return None
        s = s.strip()
        m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
        if m:
            return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
        return s if re.match(r"\d{4}-\d{2}-\d{2}", s) else None

    def validate_published_at(date_str: Optional[str], raw_content: str) -> Optional[str]:
        """校验 published_at：
        1. 日期藏在流水号/连续数字串中（如 202602251746591...）→ 丢弃
        2. 超过 180 天前 → 丢弃（防止其他异常抽取）
        """
        if not date_str:
            return None
        # 检查日期的紧凑形式（YYYYMMDD）是否嵌在更长的数字串中
        compact = date_str[:10].replace("-", "")  # "2026-02-25" → "20260225"
        if re.search(r'\d' + compact + r'\d', raw_content):
            return None  # 是流水号的一部分，丢弃
        try:
            from datetime import datetime, timedelta
            d = datetime.strptime(date_str[:10], "%Y-%m-%d")
            if d < datetime.now() - timedelta(days=180):
                return None
        except Exception:
            return None
        return date_str

    # published_at：仅 AI 提取，规则不兜底；中标结果类型不记录发布时间
    ZHONGBIAO_TYPES = {"中标结果", "中标公告", "中标公示", "中标结果公示", "中标候选人公示", "成交结果"}
    if sub_type in ZHONGBIAO_TYPES:
        published_at = None
    else:
        published_at = validate_published_at(norm_date(simple.get("published_at") or ""), content)
    bid_deadline = norm_date(simple.get("bid_deadline") or "")

    def normalize_bidding_method(v: Optional[str]) -> Optional[str]:
        """采购方式标准化：
        - 其他/其它/空 → 公开招标
        - 资格预审* → 公开招标（资格预审是流程环节，库内统一按公开招标归类）
        - 单一来源* → 单一来源
        """
        s = (v or "").strip()
        if not s or s in ("其他", "其它"):
            return "公开招标"
        if s == "资格预审" or s.startswith("资格预审"):
            return "公开招标"
        if s.startswith("单一来源"):
            return "单一来源"
        return s

    raw_method = simple.get("bidding_method") or None
    bidding_method = normalize_bidding_method(raw_method)

    row = {
        "keyword": keyword,
        "type": info_type,
        "sub_type": sub_type or None,
        "title": title or None,
        "province": (ai_result or {}).get("province") or simple.get("province") or None,
        "city": (ai_result or {}).get("city") or simple.get("city") or None,
        "district": (ai_result or {}).get("district") or simple.get("district") or None,
        "detail": content or None,
        "published_at": published_at,
        "bid_doc_fetched_at": None,
        "project_no": simple.get("project_no") or None,
        "project_budget": simple.get("project_budget") or None,
        "winning_amount": simple.get("winning_amount") or None,
        "bidding_method": bidding_method,
        "project_owner": simple.get("project_owner") or None,
        "owner_contact": simple.get("owner_contact") or None,
        "owner_phone": simple.get("owner_phone") or None,
        "winning_bidder": simple.get("winning_bidder") or None,
        "winning_bidder_contact": simple.get("winning_bidder_contact") or None,
        "winning_bidder_phone": simple.get("winning_bidder_phone") or None,
        "bidding_agent": simple.get("bidding_agent") or None,
        "bid_deadline": bid_deadline,
        "detail_url": url or None,
        "product_related": product_related or None,
        "reserve1": None,
        "reserve2": None,
    }
    if crawl_time:
        row["reserve1"] = crawl_time
    # reserve2：非金融机构（或提示词要求 false 的无法判断）→「待确认」；否则沿用 product_related_terms（与规则覆盖互斥逻辑不变）
    fi_ok = _parse_owner_party_is_financial_institution(ai_result)
    if fi_ok is False:
        row["reserve2"] = "待确认"
    elif (
        ai_result
        and ai_result.get("product_related_terms")
        and rule_ov is None
    ):
        terms = str(ai_result.get("product_related_terms", "")).strip()
        if terms:
            row["reserve2"] = terms[:500]
    row["audit_status"] = (
        AUDIT_STATUS_PENDING
        if (
            is_short_detail_for_pending(content)
            or is_placeholder_detail_content(content)
            or is_attachment_only_shell_content(content)
            or is_pdf_scan_placeholder_content(content)
        )
        else AUDIT_STATUS_APPROVED
    )
    return row
