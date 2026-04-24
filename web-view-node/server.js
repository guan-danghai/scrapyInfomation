/**
 * 招采信息披露 - Node.js 版
 * 使用项目根目录 config.ini 的 [database]，与 Python 入库脚本共用配置。
 * 启动：npm start 或 node server.js
 * 与 Nginx 443 反代配合时建议：BIND_HOST=127.0.0.1（仅本机可达，由 Nginx 对外）
 */

const express = require('express');
const path = require('path');
const fs = require('fs');
const { spawnSync } = require('child_process');
const mysql = require('mysql2/promise');
const { loadDbConfig, loadScraperDates } = require('./lib/config');

const app = express();
app.use(express.json({ limit: '2mb' }));

const PORT = process.env.PORT || 5000;
/** 监听地址：默认 0.0.0.0（局域网调试）；生产在 Nginx 后建议设 127.0.0.1 */
const BIND_HOST = process.env.BIND_HOST || '0.0.0.0';
const VIEWS_DIR = path.join(__dirname, 'views');
const PROJECT_ROOT = path.join(__dirname, '..');
const DIGEST_PACK_DIR = path.join(PROJECT_ROOT, 'digest_packs');
const SEND_DIGEST_SCRIPT = path.join(PROJECT_ROOT, 'send_digest_wecom.py');

function readConfigIniPath() {
  const candidates = [
    path.join(__dirname, '..', '..', 'config.ini'),
    path.join(process.cwd(), 'config.ini'),
    path.join(process.cwd(), '..', 'config.ini'),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  return null;
}

function parseWecomSendDefaults() {
  const iniPath = readConfigIniPath();
  if (!iniPath) {
    return { default_tousers: [], can_app_send: false, has_webhook: false };
  }
  let cfg;
  try {
    cfg = require('ini').parse(fs.readFileSync(iniPath, 'utf-8'));
  } catch (e) {
    return { default_tousers: [], can_app_send: false, has_webhook: false };
  }
  const w = cfg.wecom || cfg.Wecom || {};
  const toUser = String(w.to_user || w.to_User || '').trim();
  const parts = toUser.split(/[|,\s，]+/).map((s) => s.trim()).filter(Boolean);
  const can_app_send = !!(String(w.corp_id || '').trim()
    && String(w.agent_id || '').trim()
    && String(w.secret || '').trim());
  const has_webhook = !!String(w.webhook_url || '').trim();
  return { default_tousers: parts, can_app_send, has_webhook };
}

/** 审核台摘要 API：必须在 /ztb 剥离中间件之前注册，否则部分环境下 path 缓存导致匹配失败返回 HTML */
function handleDigestPendingApi(req, res) {
  const fp = path.join(PROJECT_ROOT, 'digest_pending_send.json');
  const defaults = parseWecomSendDefaults();
  if (!fs.existsSync(fp)) {
    return res.json({
      ok: true,
      pending: null,
      default_tousers: defaults.default_tousers,
      can_app_send: defaults.can_app_send,
      has_webhook: defaults.has_webhook,
      hint: '暂无跑批生成的摘要，请先执行 python run_pipeline.py（入库后会写入 digest_pending_send.json）',
    });
  }
  try {
    const pending = JSON.parse(fs.readFileSync(fp, 'utf8'));
    return res.json({
      ok: true,
      pending,
      default_tousers: defaults.default_tousers,
      can_app_send: defaults.can_app_send,
      has_webhook: defaults.has_webhook,
    });
  } catch (e) {
    return res.status(500).json({
      ok: false,
      error: e.message || '读取 digest_pending_send.json 失败',
    });
  }
}

function handleDigestReviewList(req, res) {
  const defaults = parseWecomSendDefaults();
  const rows = [];
  const pendingFp = path.join(PROJECT_ROOT, 'digest_pending_send.json');
  let pendingTok = '';
  if (fs.existsSync(pendingFp)) {
    try {
      const p = JSON.parse(fs.readFileSync(pendingFp, 'utf8'));
      const tok = String(p.token || '').trim().toLowerCase();
      pendingTok = tok;
      let itemCount = null;
      if (tok && /^[a-f0-9]{32}$/.test(tok)) {
        const mf = path.join(DIGEST_PACK_DIR, tok, 'manifest.json');
        if (fs.existsSync(mf)) {
          const m = JSON.parse(fs.readFileSync(mf, 'utf8'));
          itemCount = (m.items || []).length;
        }
      }
      rows.push({
        kind: 'pending_send',
        token: p.token || '',
        digest_date: p.digest_date || '',
        title: p.title || '',
        description: p.description || '',
        card_url: p.card_url || '',
        generated_at: p.generated_at || '',
        item_count: itemCount,
        label: '当前跑批待发送',
      });
    } catch (e) {
      console.error('[digest_review/list] pending json', e);
    }
  }

  const packTokens = new Set();
  if (fs.existsSync(DIGEST_PACK_DIR)) {
    for (const name of fs.readdirSync(DIGEST_PACK_DIR)) {
      const tok = String(name || '').trim().toLowerCase();
      if (!/^[a-f0-9]{32}$/.test(tok)) continue;
      if (tok === pendingTok) continue;
      const mf = path.join(DIGEST_PACK_DIR, tok, 'manifest.json');
      if (!fs.existsSync(mf)) continue;
      try {
        const m = JSON.parse(fs.readFileSync(mf, 'utf8'));
        packTokens.add(tok);
        rows.push({
          kind: 'pack',
          token: tok,
          digest_date: m.digest_date || '',
          title: '',
          description: '',
          card_url: '',
          generated_at: m.generated_at || '',
          item_count: (m.items || []).length,
          label: '历史摘要包',
        });
      } catch (e) {
        console.error('[digest_review/list] manifest', tok, e);
      }
    }
  }

  const pendingRows = rows.filter((r) => r.kind === 'pending_send');
  const packRows = rows.filter((r) => r.kind === 'pack').sort((a, b) => {
    const ta = String(a.generated_at || '');
    const tb = String(b.generated_at || '');
    return tb.localeCompare(ta);
  });
  return res.json({
    ok: true,
    rows: [...pendingRows, ...packRows],
    default_tousers: defaults.default_tousers,
    can_app_send: defaults.can_app_send,
    has_webhook: defaults.has_webhook,
  });
}

function sendDigestReviewPage(req, res) {
  const htmlPath = path.join(VIEWS_DIR, 'digest_review.html');
  if (!fs.existsSync(htmlPath)) {
    return res.status(500).send('views/digest_review.html not found');
  }
  res.set({
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    Pragma: 'no-cache',
  });
  res.sendFile(htmlPath);
}

function handleSendDigestApi(req, res) {
  const raw = (req.body && req.body.touser_ids) || (req.body && req.body.touser_list);
  const list = Array.isArray(raw) ? raw : [];
  const users = list.map((u) => String(u || '').trim()).filter(Boolean);
  if (!users.length) {
    return res.status(400).json({ ok: false, error: '请至少选择一名接收人（userid）' });
  }
  const defaults = parseWecomSendDefaults();
  if (!defaults.can_app_send) {
    return res.status(400).json({
      ok: false,
      error: '未配置企业微信应用消息（corp_id / agent_id / secret），无法按成员发送卡片',
    });
  }
  if (!fs.existsSync(SEND_DIGEST_SCRIPT)) {
    return res.status(500).json({ ok: false, error: '未找到 send_digest_wecom.py' });
  }
  const py = process.env.PYTHON || 'python';
  const digestToken = String((req.body && req.body.digest_token) || (req.body && req.body.token) || '').trim();
  const input = JSON.stringify({ touser_list: users, digest_token: digestToken });
  const r = spawnSync(py, [SEND_DIGEST_SCRIPT], {
    input,
    encoding: 'utf-8',
    maxBuffer: 2 * 1024 * 1024,
    cwd: PROJECT_ROOT,
    timeout: 60000,
    windowsHide: true,
  });
  if (r.error) {
    return res.status(500).json({ ok: false, error: r.error.message || String(r.error) });
  }
  const out = (r.stdout || '').trim();
  let parsed;
  try {
    const line = out.split(/\r?\n/).filter(Boolean).pop() || '{}';
    parsed = JSON.parse(line);
  } catch (e) {
    return res.status(500).json({
      ok: false,
      error: '发送脚本输出非 JSON',
      detail: (out || '').slice(0, 500),
      code: r.status,
    });
  }
  if (!parsed.ok) {
    return res.status(400).json({ ok: false, error: parsed.error || '发送失败' });
  }
  return res.json({ ok: true, touser: parsed.touser });
}

app.get('/ztb/api/review/digest_pending', handleDigestPendingApi);
app.get('/api/review/digest_pending', handleDigestPendingApi);
app.post('/ztb/api/review/send_digest', handleSendDigestApi);
app.post('/api/review/send_digest', handleSendDigestApi);
app.get('/ztb/api/digest_review/list', handleDigestReviewList);
app.get('/api/digest_review/list', handleDigestReviewList);
app.get('/ztb/digest_review', sendDigestReviewPage);
app.get('/digest_review', sendDigestReviewPage);
app.post('/ztb/api/digest_review/remove_item', handleDigestReviewRemoveItem);

/**
 * 浏览器在 /ztb/review、/ztb/api/... 下访问时，将 URL 剥成 /review、/api/...，
 * 与根路径共用同一套路由（避免重复注册）。
 * 改写 req.url 后清除 parseurl 缓存，否则后续路由可能仍按旧 path 匹配失败。
 */
app.use((req, res, next) => {
  const raw = req.originalUrl || '';
  const pathOnly = raw.split('?')[0];
  if (pathOnly === '/ztb' || pathOnly.startsWith('/ztb/')) {
    const rest = pathOnly.slice(4) || '/';
    const qs = raw.includes('?') ? raw.slice(raw.indexOf('?')) : '';
    req.url = rest + qs;
    delete req._parsedUrl;
  }
  next();
});

/* 静态资源：样式表（显式路由，避免路径或中间件顺序导致 404） */
const CSS_DIR = path.join(VIEWS_DIR, 'css');
app.get('/css/infos.css', (req, res) => {
  const fp = path.join(CSS_DIR, 'infos.css');
  if (!fs.existsSync(fp)) return res.status(404).send('Not found');
  res.type('text/css').sendFile(fp);
});
app.get('/css/detail.css', (req, res) => {
  const fp = path.join(CSS_DIR, 'detail.css');
  if (!fs.existsSync(fp)) return res.status(404).send('Not found');
  res.type('text/css').sendFile(fp);
});
app.get('/css/review.css', (req, res) => {
  const fp = path.join(CSS_DIR, 'review.css');
  if (!fs.existsSync(fp)) return res.status(404).send('Not found');
  res.type('text/css').sendFile(fp);
});
app.get('/css/digest_review.css', (req, res) => {
  const fp = path.join(CSS_DIR, 'digest_review.css');
  if (!fs.existsSync(fp)) return res.status(404).send('Not found');
  res.type('text/css').sendFile(fp);
});

let pool = null;
let poolError = null;

function getPool() {
  if (pool) return pool;
  if (poolError) return null;
  const cfg = loadDbConfig();
  if (!cfg || !cfg.database) {
    poolError = '未配置数据库（请检查 config.ini [database] 或从项目根目录启动）';
    return null;
  }
  try {
    pool = mysql.createPool({
      host: cfg.host,
      port: cfg.port,
      user: cfg.user,
      password: cfg.password,
      database: cfg.database,
      charset: cfg.charset,
      waitForConnections: true,
      connectionLimit: 10,
    });
    return pool;
  } catch (e) {
    poolError = e.message;
    return null;
  }
}

function serializeRow(row) {
  const out = { ...row };
  const pad = n => String(n).padStart(2, '0');
  for (const k of Object.keys(out)) {
    if (out[k] && typeof out[k].toISOString === 'function') {
      const d = out[k];
      // 用本地时间格式化，避免 toISOString() 转 UTC 导致跨天偏移（UTC+8 问题）
      out[k] = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} `
             + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }
  }
  return out;
}

/** 与爬虫归一后的 sub_type 一致：用于统计与「招标 / 中标」筛选 */
const SUB_TYPES_ZHAOBIAO = ['招标公告', '招标预告', '招标变更', '采购信息', '审批公示'];
const SUB_TYPES_ZHONGBIAO = [
  '中标结果',
  '中标公示',
  '中标公告',
  '中标结果公示',
  '中标候选人公示',
  '中标',
];

function sqlInPlaceholders(n) {
  return n > 0 ? Array(n).fill('?').join(',') : '';
}

const EMPTY_TYPE_COUNTS = { 招标公告: 0, 中标结果: 0 };

/** 披露列表与企微摘要：仅「审核通过」（含 NULL/空，兼容旧数据） */
const SQL_AUDIT_APPROVED = "COALESCE(NULLIF(TRIM(audit_status), ''), '审核通过') = '审核通过'";

function auditFilterClause(auditFilter) {
  const f = (auditFilter || 'approved').trim();
  if (f === 'pending') return "TRIM(IFNULL(audit_status,'')) = '待审核'";
  if (f === 'all') return '1=1';
  return SQL_AUDIT_APPROVED;
}

// 列表页
app.get('/', (req, res) => {
  const htmlPath = path.join(VIEWS_DIR, 'infos.html');
  if (!fs.existsSync(htmlPath)) {
    return res.status(500).send('views/infos.html not found');
  }
  res.set({
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    'Pragma': 'no-cache',
  });
  res.sendFile(htmlPath);
});

// 审核工作台：待审核 / 审核通过 / 人工补全
app.get('/review', (req, res) => {
  const htmlPath = path.join(VIEWS_DIR, 'review.html');
  if (!fs.existsSync(htmlPath)) {
    return res.status(500).send('views/review.html not found');
  }
  res.set({
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    'Pragma': 'no-cache',
  });
  res.sendFile(htmlPath);
});

// 爬取详情页（注入 record_id）
app.get('/detail/:recordId', (req, res) => {
  const id = parseInt(req.params.recordId, 10);
  if (Number.isNaN(id) || id < 1) {
    return res.status(400).send('Invalid id');
  }
  const htmlPath = path.join(VIEWS_DIR, 'detail.html');
  if (!fs.existsSync(htmlPath)) {
    return res.status(500).send('views/detail.html not found');
  }
  let html = fs.readFileSync(htmlPath, 'utf-8');
  html = html.replace(/\{\{\s*record_id\s*\}\}/g, String(id));
  res.set({
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    'Pragma': 'no-cache',
  });
  res.type('html').send(html);
});

// API: 单条详情
app.get('/api/detail/:recordId', async (req, res) => {
  const id = parseInt(req.params.recordId, 10);
  if (Number.isNaN(id) || id < 1) {
    return res.json({ ok: false, error: 'Invalid id' });
  }
  const p = getPool();
  if (!p) {
    return res.status(500).json({ ok: false, error: '未配置数据库' });
  }
  try {
    const [rows] = await p.execute(
      `SELECT id, title, sub_type, product_related, reserve2 AS product_related_terms,
              project_no, project_budget, winning_amount, bidding_method,
              project_owner, owner_contact, owner_phone,
              winning_bidder, winning_bidder_contact, winning_bidder_phone,
              bidding_agent, bid_deadline, published_at, detail_url, detail,
              province, city, district, created_at, audit_status
       FROM scraping_infos WHERE id = ?`,
      [id]
    );
    if (!rows || rows.length === 0) {
      return res.status(404).json({ ok: false, error: '记录不存在' });
    }
    return res.json({ ok: true, item: serializeRow(rows[0]) });
  } catch (e) {
    return res.status(500).json({ ok: false, error: e.message });
  }
});

// API: 爬取配置的发布日期范围（读 config.ini [scraper] start_date / end_date）
app.get('/api/scrape_dates', (req, res) => {
  const { startDate, endDate } = loadScraperDates();
  return res.json({ ok: true, start_date: startDate, end_date: endDate });
});

// API: 最新入库日期（用于列表页默认日期）
app.get('/api/latest_date', async (req, res) => {
  const p = getPool();
  if (!p) return res.json({ ok: true, date: '' });
  try {
    const [rows] = await p.execute(
      `SELECT DATE_FORMAT(MAX(d), '%Y-%m-%d') AS d FROM (
        SELECT DATE(created_at) AS d FROM scraping_infos WHERE ${SQL_AUDIT_APPROVED}
        UNION ALL
        SELECT DATE(updated_at) AS d FROM scraping_infos WHERE ${SQL_AUDIT_APPROVED}
      ) x WHERE d IS NOT NULL`
    );
    const date = (rows && rows[0] && rows[0].d) || '';
    return res.json({ ok: true, date });
  } catch (e) {
    return res.json({ ok: true, date: '' });
  }
});

// API: sub_type 枚举（动态读库）
app.get('/api/sub_type_options', async (req, res) => {
  const p = getPool();
  if (!p) return res.json({ ok: true, options: [] });
  try {
    const [rows] = await p.execute(
      "SELECT DISTINCT sub_type FROM scraping_infos WHERE sub_type IS NOT NULL AND sub_type <> '' ORDER BY sub_type"
    );
    const options = (rows || []).map(r => r.sub_type).filter(Boolean);
    return res.json({ ok: true, options });
  } catch (e) {
    return res.json({ ok: true, options: [] });
  }
});

// API: 分页列表
app.get('/api/list', async (req, res) => {
  const page = Math.max(1, parseInt(req.query.page, 10) || 1);
  const pageSize = Math.min(100, Math.max(1, parseInt(req.query.page_size, 10) || 50));
  const keyword = (req.query.keyword || '').trim();
  const subType = (req.query.sub_type || '').trim();
  const productRelated = (req.query.product_related || '').trim();
  const dateStart = (req.query.date_start || '').trim();  // YYYY-MM-DD
  const dateEnd   = (req.query.date_end   || '').trim();  // YYYY-MM-DD
  const pubStart  = (req.query.pub_start  || '').trim();  // YYYY-MM-DD 按发布日期筛选
  const pubEnd    = (req.query.pub_end    || '').trim();  // YYYY-MM-DD 按发布日期筛选
  const auditFilter = (req.query.audit_filter || 'approved').trim();

  const p = getPool();
  if (!p) {
    return res.status(500).json({
      ok: false,
      error: poolError || '未配置数据库',
      items: [],
      total: 0,
      type_counts: { ...EMPTY_TYPE_COUNTS },
    });
  }

  const conditions = [];
  const params = [];

  if (keyword) {
    const q = `%${keyword}%`;
    conditions.push('(title LIKE ? OR product_related LIKE ? OR reserve2 LIKE ? OR project_no LIKE ?)');
    params.push(q, q, q, q);
  }
  if (subType === '招标公告') {
    const ph = sqlInPlaceholders(SUB_TYPES_ZHAOBIAO.length);
    conditions.push(`TRIM(sub_type) IN (${ph})`);
    params.push(...SUB_TYPES_ZHAOBIAO);
  } else if (subType === '中标结果') {
    const ph = sqlInPlaceholders(SUB_TYPES_ZHONGBIAO.length);
    conditions.push(`TRIM(sub_type) IN (${ph})`);
    params.push(...SUB_TYPES_ZHONGBIAO);
  } else if (subType) {
    conditions.push('TRIM(sub_type) = ?');
    params.push(subType);
  }
  if (productRelated) {
    conditions.push('product_related LIKE ?');
    params.push(`%${productRelated}%`);
  }
  if (dateStart) {
    conditions.push("DATE_FORMAT(created_at, '%Y-%m-%d') >= ?");
    params.push(dateStart);
  }
  if (dateEnd) {
    conditions.push("DATE_FORMAT(created_at, '%Y-%m-%d') <= ?");
    params.push(dateEnd);
  }
  if (pubStart) {
    conditions.push("DATE_FORMAT(published_at, '%Y-%m-%d') >= ?");
    params.push(pubStart);
  }
  if (pubEnd) {
    conditions.push("DATE_FORMAT(published_at, '%Y-%m-%d') <= ?");
    params.push(pubEnd);
  }
  conditions.push(`(${auditFilterClause(auditFilter)})`);

  const whereSql = conditions.length ? conditions.join(' AND ') : '1=1';

  /* 与列表相同筛选条件，但不含 sub_type 和 product_related，用于筛选按钮上的类型条数 */
  const typeCountConditions = [];
  const typeCountParams = [];
  if (keyword) {
    const q = `%${keyword}%`;
    typeCountConditions.push('(title LIKE ? OR product_related LIKE ? OR reserve2 LIKE ? OR project_no LIKE ?)');
    typeCountParams.push(q, q, q, q);
  }
  if (dateStart) {
    typeCountConditions.push("DATE_FORMAT(created_at, '%Y-%m-%d') >= ?");
    typeCountParams.push(dateStart);
  }
  if (dateEnd) {
    typeCountConditions.push("DATE_FORMAT(created_at, '%Y-%m-%d') <= ?");
    typeCountParams.push(dateEnd);
  }
  if (pubStart) {
    typeCountConditions.push("DATE_FORMAT(published_at, '%Y-%m-%d') >= ?");
    typeCountParams.push(pubStart);
  }
  if (pubEnd) {
    typeCountConditions.push("DATE_FORMAT(published_at, '%Y-%m-%d') <= ?");
    typeCountParams.push(pubEnd);
  }
  typeCountConditions.push(`(${auditFilterClause(auditFilter)})`);
  const allCountedTypes = [...SUB_TYPES_ZHAOBIAO, ...SUB_TYPES_ZHONGBIAO];
  typeCountConditions.push(`TRIM(sub_type) IN (${sqlInPlaceholders(allCountedTypes.length)})`);
  typeCountParams.push(...allCountedTypes);
  const typeWhereSql = typeCountConditions.join(' AND ');

  /* product_related 计数：忽略 product_related 筛选，保留其余所有条件（含 sub_type） */
  const prCountConditions = [];
  const prCountParams = [];
  if (keyword) {
    const q = `%${keyword}%`;
    prCountConditions.push('(title LIKE ? OR product_related LIKE ? OR reserve2 LIKE ? OR project_no LIKE ?)');
    prCountParams.push(q, q, q, q);
  }
  if (subType === '招标公告') {
    const ph = sqlInPlaceholders(SUB_TYPES_ZHAOBIAO.length);
    prCountConditions.push(`TRIM(sub_type) IN (${ph})`);
    prCountParams.push(...SUB_TYPES_ZHAOBIAO);
  } else if (subType === '中标结果') {
    const ph = sqlInPlaceholders(SUB_TYPES_ZHONGBIAO.length);
    prCountConditions.push(`TRIM(sub_type) IN (${ph})`);
    prCountParams.push(...SUB_TYPES_ZHONGBIAO);
  } else if (subType) {
    prCountConditions.push('TRIM(sub_type) = ?');
    prCountParams.push(subType);
  }
  if (dateStart) {
    prCountConditions.push("DATE_FORMAT(created_at, '%Y-%m-%d') >= ?");
    prCountParams.push(dateStart);
  }
  if (dateEnd) {
    prCountConditions.push("DATE_FORMAT(created_at, '%Y-%m-%d') <= ?");
    prCountParams.push(dateEnd);
  }
  prCountConditions.push(`(${auditFilterClause(auditFilter)})`);
  const prWhereSql = prCountConditions.length ? prCountConditions.join(' AND ') : '1=1';

  const phZbb = sqlInPlaceholders(SUB_TYPES_ZHAOBIAO.length);
  const phZbJg = sqlInPlaceholders(SUB_TYPES_ZHONGBIAO.length);

  try {
    const [typeSumRows] = await p.execute(
      `SELECT
        SUM(CASE WHEN TRIM(sub_type) IN (${phZbb}) THEN 1 ELSE 0 END) AS c_zbb,
        SUM(CASE WHEN TRIM(sub_type) IN (${phZbJg}) THEN 1 ELSE 0 END) AS c_zb
       FROM scraping_infos WHERE ${typeWhereSql}`,
      [...SUB_TYPES_ZHAOBIAO, ...SUB_TYPES_ZHONGBIAO, ...typeCountParams]
    );
    const tr = (typeSumRows && typeSumRows[0]) || {};
    const type_counts = {
      招标公告: Number(tr.c_zbb) || 0,
      中标结果: Number(tr.c_zb) || 0,
    };

    const [prSumRows] = await p.execute(
      `SELECT
        SUM(CASE WHEN TRIM(product_related) = '软件相关' THEN 1 ELSE 0 END) AS c_sw,
        SUM(CASE WHEN TRIM(product_related) = '硬件相关' THEN 1 ELSE 0 END) AS c_hw
       FROM scraping_infos WHERE ${prWhereSql}`,
      prCountParams
    );
    const prr = (prSumRows && prSumRows[0]) || {};
    const product_related_counts = {
      软件相关: Number(prr.c_sw) || 0,
      硬件相关: Number(prr.c_hw) || 0,
    };

    const [countRows] = await p.execute(
      `SELECT COUNT(*) AS total FROM scraping_infos WHERE ${whereSql}`,
      params
    );
    const total = (countRows && countRows[0] && (countRows[0].total ?? countRows[0].TOTAL)) || 0;
    const limit = Math.min(100, Math.max(1, parseInt(pageSize, 10) || 50));
    const offset = Math.max(0, parseInt((page - 1) * pageSize, 10));

    // LIMIT/OFFSET 用字面量（已校验为整数），避免 mysqld_stmt_execute 占位符报错
    const [rows] = await p.execute(
      `SELECT id, title, sub_type, product_related, reserve2 AS product_related_terms,
              project_no, project_budget, winning_amount, bidding_method,
              project_owner, owner_contact, owner_phone,
              winning_bidder, bidding_agent,
              published_at, bid_deadline, detail_url, created_at, audit_status
       FROM scraping_infos
       WHERE ${whereSql}
       ORDER BY id DESC
       LIMIT ${limit} OFFSET ${offset}`,
      params
    );

    const items = (rows || []).map(serializeRow);
    return res.json({
      ok: true,
      items,
      total,
      page,
      page_size: pageSize,
      type_counts,
      product_related_counts,
    });
  } catch (e) {
    const msg = e && (e.message || String(e));
    console.error('[api/list]', msg);
    return res.status(500).json({
      ok: false,
      error: msg || '服务器错误',
      items: [],
      total: 0,
      type_counts: { ...EMPTY_TYPE_COUNTS },
    });
  }
});

/** 企微摘要卡片专用：仅能通过推送时生成的 token 读取 manifest，避免改 URL 日期枚举数据 */
function handleDigestPackApi(req, res) {
  const token = String(req.params.token || '').trim().toLowerCase();
  if (!/^[a-f0-9]{32}$/.test(token)) {
    return res.status(400).json({ ok: false, error: '无效的链接' });
  }
  const fp = path.join(DIGEST_PACK_DIR, token, 'manifest.json');
  if (!fs.existsSync(fp)) {
    return res.status(404).json({ ok: false, error: '摘要已失效或未发布' });
  }
  try {
    const raw = fs.readFileSync(fp, 'utf8');
    const data = JSON.parse(raw);
    return res.json({ ok: true, digest_date: data.digest_date, generated_at: data.generated_at, items: data.items || [] });
  } catch (e) {
    console.error('[api/digest_pack]', e);
    return res.status(500).json({ ok: false, error: '读取摘要包失败' });
  }
}
app.get('/api/digest_pack/:token', handleDigestPackApi);

function manifestDigestDescriptionFromItems(items) {
  const zbbSet = new Set(SUB_TYPES_ZHAOBIAO);
  const zbSet = new Set(SUB_TYPES_ZHONGBIAO);
  let zbb = 0;
  let zb = 0;
  for (const it of items || []) {
    const st = String((it && it.sub_type) || '').trim();
    if (zbbSet.has(st)) zbb += 1;
    else if (zbSet.has(st)) zb += 1;
  }
  return {
    zbb,
    zb,
    text: `📌 今日概览\n新增招标公告：${zbb} 条\n新增中标公示：${zb} 条`,
  };
}

/** 从当前摘要包移除一条：manifest 删行 + 库中置为待审核（与发送/预览一致） */
async function handleDigestReviewRemoveItem(req, res) {
  const token = String((req.body && req.body.token) || '').trim().toLowerCase();
  const recordId = parseInt(req.body && req.body.record_id, 10);
  if (!/^[a-f0-9]{32}$/.test(token)) {
    return res.status(400).json({ ok: false, error: '无效 token' });
  }
  if (Number.isNaN(recordId) || recordId < 1) {
    return res.status(400).json({ ok: false, error: '无效 record_id' });
  }
  const fp = path.join(DIGEST_PACK_DIR, token, 'manifest.json');
  if (!fs.existsSync(fp)) {
    return res.status(404).json({ ok: false, error: '摘要包不存在' });
  }
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(fp, 'utf8'));
  } catch (e) {
    return res.status(500).json({ ok: false, error: '读取 manifest 失败' });
  }
  const items = Array.isArray(manifest.items) ? manifest.items : [];
  const idx = items.findIndex((it) => Number(it && it.id) === recordId);
  if (idx < 0) {
    return res.status(404).json({ ok: false, error: '本摘要包中不包含该记录' });
  }

  const pool = getPool();
  if (!pool) {
    return res.status(500).json({ ok: false, error: poolError || '未配置数据库' });
  }

  try {
    const [hdr] = await pool.execute(
      "UPDATE scraping_infos SET audit_status = '待审核' WHERE id = ?",
      [recordId]
    );
    const ar = hdr && typeof hdr.affectedRows === 'number' ? hdr.affectedRows : 0;
    if (ar === 0) {
      return res.status(404).json({ ok: false, error: '数据库中不存在该 id' });
    }

    const nextItems = items.filter((_, i) => i !== idx);
    manifest.items = nextItems;
    const tmp = `${fp}.${process.pid}.${Date.now()}.tmp`;
    try {
      fs.writeFileSync(tmp, JSON.stringify(manifest, null, 2), 'utf-8');
      fs.renameSync(tmp, fp);
    } catch (fe) {
      try {
        if (fs.existsSync(tmp)) fs.unlinkSync(tmp);
      } catch (e2) { /* ignore */ }
      await pool.execute(
        "UPDATE scraping_infos SET audit_status = '审核通过' WHERE id = ?",
        [recordId]
      );
      console.error('[digest_review/remove_item] manifest 写入失败，已回滚审核状态', fe);
      return res.status(500).json({
        ok: false,
        error: fe.message || '写入 manifest 失败，已回滚该条审核状态',
      });
    }

    const descStats = manifestDigestDescriptionFromItems(nextItems);
    const pendingFp = path.join(PROJECT_ROOT, 'digest_pending_send.json');
    if (fs.existsSync(pendingFp)) {
      try {
        const pen = JSON.parse(fs.readFileSync(pendingFp, 'utf-8'));
        if (String(pen.token || '').trim().toLowerCase() === token) {
          pen.description = descStats.text;
          fs.writeFileSync(pendingFp, JSON.stringify(pen, null, 2), 'utf-8');
        }
      } catch (e) {
        console.error('[digest_review/remove_item] 更新 digest_pending_send.json', e);
      }
    }

    return res.json({
      ok: true,
      remaining: nextItems.length,
      description: descStats.text,
      zbb: descStats.zbb,
      zb: descStats.zb,
    });
  } catch (e) {
    const msg = e && (e.message || String(e));
    console.error('[digest_review/remove_item]', msg);
    return res.status(500).json({ ok: false, error: msg || '服务器错误' });
  }
}

app.post('/api/digest_review/remove_item', handleDigestReviewRemoveItem);

function handleDigestPackPage(req, res) {
  const token = String(req.params.token || '').trim().toLowerCase();
  if (!/^[a-f0-9]{32}$/.test(token)) {
    return res.type('html').status(400).send('无效的链接');
  }
  const fp = path.join(DIGEST_PACK_DIR, token, 'manifest.json');
  if (!fs.existsSync(fp)) {
    return res.type('html').status(404).send('链接已失效或尚未生成，请从最新企微摘要卡片进入。');
  }
  const htmlPath = path.join(VIEWS_DIR, 'digest_pack.html');
  if (!fs.existsSync(htmlPath)) {
    return res.status(500).send('digest_pack.html 缺失');
  }
  res.set({
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    Pragma: 'no-cache',
  });
  res.sendFile(htmlPath);
}
app.get('/digest/:token', handleDigestPackPage);

const REVIEW_SCRIPT = path.join(PROJECT_ROOT, 'review_supplement.py');

app.post('/api/review/supplement', (req, res) => {
  const id = parseInt(req.body && req.body.id, 10);
  const supplement = (req.body && req.body.supplement) || '';
  if (Number.isNaN(id) || id < 1) {
    return res.status(400).json({ ok: false, error: '无效 id' });
  }
  if (!String(supplement).trim()) {
    return res.status(400).json({ ok: false, error: '补充正文不能为空' });
  }
  if (!fs.existsSync(REVIEW_SCRIPT)) {
    return res.status(500).json({ ok: false, error: '未找到 review_supplement.py' });
  }
  const py = process.env.PYTHON || 'python';
  const input = JSON.stringify({ id, supplement: String(supplement) });
  const r = spawnSync(py, [REVIEW_SCRIPT], {
    input,
    encoding: 'utf-8',
    maxBuffer: 12 * 1024 * 1024,
    cwd: PROJECT_ROOT,
    timeout: 180000,
    windowsHide: true,
  });
  if (r.error) {
    return res.status(500).json({ ok: false, error: r.error.message || String(r.error) });
  }
  const stderr = (r.stderr || '').trim();
  if (stderr) console.error('[review_supplement]', stderr);
  const out = (r.stdout || '').trim();
  let parsed;
  try {
    const line = out.split(/\r?\n/).filter(Boolean).pop() || '{}';
    parsed = JSON.parse(line);
  } catch (e) {
    return res.status(500).json({
      ok: false,
      error: '脚本输出非 JSON',
      detail: (out || '').slice(0, 500),
      code: r.status,
    });
  }
  if (!parsed.ok) {
    return res.status(400).json({ ok: false, error: parsed.error || '补全失败' });
  }
  return res.json({ ok: true });
});

/** 审核工作台：仅将 audit_status 置为「审核通过」（不跑 AI 补全） */
app.post('/api/review/approve', async (req, res) => {
  const id = parseInt(req.body && req.body.id, 10);
  if (Number.isNaN(id) || id < 1) {
    return res.status(400).json({ ok: false, error: '无效 id' });
  }
  const p = getPool();
  if (!p) {
    return res.status(500).json({ ok: false, error: poolError || '未配置数据库' });
  }
  try {
    const [hdr] = await p.execute(
      "UPDATE scraping_infos SET audit_status = '审核通过' WHERE id = ?",
      [id]
    );
    const ar = hdr && typeof hdr.affectedRows === 'number' ? hdr.affectedRows : 0;
    if (ar === 0) {
      return res.status(404).json({ ok: false, error: '未找到该记录' });
    }
    return res.json({ ok: true });
  } catch (e) {
    const msg = e && (e.message || String(e));
    console.error('[api/review/approve]', msg);
    return res.status(500).json({ ok: false, error: msg || '服务器错误' });
  }
});

app.listen(PORT, BIND_HOST, () => {
  const cfg = loadDbConfig();
  const dbOk = cfg && cfg.database ? `数据库 ${cfg.database}@${cfg.host}` : '未读取到数据库配置';
  const hostHint = BIND_HOST === '0.0.0.0' ? '127.0.0.1' : BIND_HOST;
  console.log(`招采信息披露 Node 版: http://${hostHint}:${PORT} | 监听 ${BIND_HOST}:${PORT} | ${dbOk}`);
});
