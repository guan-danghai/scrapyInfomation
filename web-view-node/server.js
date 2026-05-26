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
/** Windows 下 Python 子进程 stdout 须 UTF-8，否则中文 JSON 被 Node 按 utf-8 解码会乱码 */
function pythonSpawnEnv() {
  return { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' };
}
/** 摘要包目录：优先环境变量 DIGEST_PACK_DIR，其次 config.ini [web_view] digest_pack_dir，默认 <项目根>/digest_packs */
let _cachedDigestPacksDir;
function getDigestPacksDir() {
  if (_cachedDigestPacksDir !== undefined) return _cachedDigestPacksDir;
  const fromEnv = (process.env.DIGEST_PACK_DIR || '').trim();
  if (fromEnv) {
    _cachedDigestPacksDir = path.isAbsolute(fromEnv) ? fromEnv : path.resolve(PROJECT_ROOT, fromEnv);
    return _cachedDigestPacksDir;
  }
  const iniPath = readConfigIniPath();
  let rel = '';
  if (iniPath) {
    try {
      const cfg = require('ini').parse(fs.readFileSync(iniPath, 'utf-8'));
      const wv = cfg.web_view || cfg.webView || {};
      rel = String(wv.digest_pack_dir || wv.digest_packs_dir || '').trim();
    } catch (e) {
      /* ignore */
    }
  }
  if (rel) {
    _cachedDigestPacksDir = path.isAbsolute(rel) ? rel : path.resolve(PROJECT_ROOT, rel);
  } else {
    _cachedDigestPacksDir = path.join(PROJECT_ROOT, 'digest_packs');
  }
  return _cachedDigestPacksDir;
}

function readDigestPendingMeta() {
  const pendingFp = path.join(PROJECT_ROOT, 'digest_pending_send.json');
  if (!fs.existsSync(pendingFp)) return null;
  try {
    const pen = JSON.parse(fs.readFileSync(pendingFp, 'utf-8'));
    const tok = String(pen.token || '').trim().toLowerCase();
    if (!/^[a-f0-9]{32}$/.test(tok)) return null;
    return pen;
  } catch (e) {
    return null;
  }
}
const SEND_DIGEST_SCRIPT = path.join(PROJECT_ROOT, 'send_digest_wecom.py');
const FORWARD_ITEM_SCRIPT = path.join(PROJECT_ROOT, 'forward_item_wecom.py');
const MATCH_BY_DISPATCH_SCRIPT = path.join(PROJECT_ROOT, 'dispatch_match_by_dispatch.py');

function getDashboardSecret() {
  const iniPath = readConfigIniPath();
  if (!iniPath) return '';
  try {
    const cfg = require('ini').parse(fs.readFileSync(iniPath, 'utf-8'));
    const wv = cfg.web_view || cfg.webView || {};
    return String(wv.dashboard_secret || wv.dashboard_key || '').trim();
  } catch (e) {
    return '';
  }
}

function assertDashboardKey(req) {
  const need = getDashboardSecret();
  if (!need) return true;
  const q = String(req.query.key || req.query.secret || '').trim();
  const h = String(req.headers['x-dashboard-key'] || '').trim();
  return q === need || h === need;
}

/** 摘要分发链路：发送企微卡片、打开卡片入口、摘要内阅读；控制台 grep `[digest_trace]`；ts 为 Asia/Shanghai 墙钟（避免 toISOString 的 UTC 误读） */
function digestTraceLog(event, payload) {
  const row = {
    event,
    ts: formatDateTimeAsiaShanghai(new Date()),
    ...(payload && typeof payload === 'object' ? payload : { detail: payload }),
  };
  try {
    console.log('[digest_trace]', JSON.stringify(row));
  } catch (e) {
    console.log('[digest_trace]', event, payload);
  }
}

function clientIpHint(req) {
  const xf = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim();
  return xf || (req.socket && req.socket.remoteAddress) || '';
}

function readConfigIniPath() {
  /* __dirname 为 web-view-node，项目根为上一级；勿用 ../.. 否则会指到仓库外的错误 config.ini */
  const candidates = [
    path.join(PROJECT_ROOT, 'config.ini'),
    path.join(__dirname, '..', 'config.ini'),
    path.join(process.cwd(), 'config.ini'),
    path.join(process.cwd(), '..', 'config.ini'),
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  return null;
}

/**
 * 单条转发卡片的 link_prefix，须带齐协议、域名、**端口**（如 :8335）与 /ztb 或 /ztb-test。
 * 经 Nginx 用 $host 转发时曾会丢端口；已改 $http_host。仍无端口时，用 config.ini [wecom] disclose_page_url 的 origin 兜底。
 */
function resolveForwardLinkPrefix(req, envPrefix) {
  const xfProto = String(req.headers['x-forwarded-proto'] || '').split(',')[0].trim();
  const proto = xfProto || req.protocol || 'http';
  const hostRaw = String(req.headers['x-forwarded-host'] || req.headers.host || '').split(',')[0].trim();
  if (!hostRaw) return null;
  const env = String(envPrefix || '');
  const direct = `${proto}://${hostRaw}${env}`;

  const hostHasNumericPort = () => {
    if (hostRaw.startsWith('[')) {
      return /]:\d+$/.test(hostRaw);
    }
    const idx = hostRaw.lastIndexOf(':');
    if (idx <= 0) return false;
    return /^\d+$/.test(hostRaw.slice(idx + 1));
  };
  if (hostHasNumericPort()) return direct;

  const iniPath = readConfigIniPath();
  if (!iniPath) return direct;
  let disclose;
  try {
    const cfg = require('ini').parse(fs.readFileSync(iniPath, 'utf-8'));
    disclose = String((cfg.wecom || cfg.Wecom || {}).disclose_page_url || '').trim();
  } catch (e) {
    return direct;
  }
  if (!disclose) return direct;
  try {
    const du = new URL(disclose);
    let reqName = hostRaw;
    if (hostRaw.startsWith('[')) {
      const close = hostRaw.indexOf(']');
      if (close > 1) reqName = hostRaw.slice(1, close);
    }
    if (du.hostname.toLowerCase() !== reqName.toLowerCase()) return direct;
    const cfgPath = (du.pathname || '').replace(/\/$/, '');
    const envPath = env.replace(/\/$/, '') || '';
    if (cfgPath && envPath && cfgPath !== envPath) return direct;
    return `${du.origin}${env}`;
  } catch (e) {
    return direct;
  }
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
        const mf = path.join(getDigestPacksDir(), tok, 'manifest.json');
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
  const packDir = getDigestPacksDir();
  if (fs.existsSync(packDir)) {
    for (const name of fs.readdirSync(packDir)) {
      const tok = String(name || '').trim().toLowerCase();
      if (!/^[a-f0-9]{32}$/.test(tok)) continue;
      if (tok === pendingTok) continue;
      const mf = path.join(packDir, tok, 'manifest.json');
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
  const mergedRows = [...pendingRows, ...packRows];
  /** 同一 manifest.digest_date 多次跑批会在磁盘留下多个目录 → 列表会出现多行同摘要日（与是否已发送无关） */
  const dateCount = new Map();
  for (const r of mergedRows) {
    const d = String(r.digest_date || '').trim();
    if (!d) continue;
    dateCount.set(d, (dateCount.get(d) || 0) + 1);
  }
  const duplicate_digest_dates = [...dateCount.entries()]
    .filter(([, c]) => c > 1)
    .map(([d]) => d);
  return res.json({
    ok: true,
    rows: mergedRows,
    duplicate_digest_dates,
    default_tousers: defaults.default_tousers,
    can_app_send: defaults.can_app_send,
    has_webhook: defaults.has_webhook,
  });
}

/** 摘要审核发送弹窗：本摘要包在 digest_item_presale_route 里匹配到的售前 userid（与 H5 @售前 同源） */
async function handleDigestMatchedRecipients(req, res) {
  const token = String(req.query.token || '').trim().toLowerCase();
  if (!/^[a-f0-9]{32}$/.test(token)) {
    return res.status(400).json({ ok: false, error: '无效 token' });
  }
  const p = getPool();
  if (!p) {
    return res.json({
      ok: true,
      matched_userids: [],
      hint: '数据库未配置，无法读取售前匹配 userid',
    });
  }
  try {
    await ensureDispatchTables(p);
    const [rows] = await p.execute(
      'SELECT DISTINCT `presale_userid` AS u FROM `digest_item_presale_route` WHERE `digest_token` = ? ORDER BY `presale_userid`',
      [token]
    );
    const matchedUserids = (rows || [])
      .map((r) => String(r.u || '').trim())
      .filter(Boolean);
    return res.json({ ok: true, matched_userids: matchedUserids });
  } catch (e) {
    console.error('[digest_review/matched_recipients]', e);
    return res.status(500).json({ ok: false, error: e.message || '查询失败' });
  }
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
  const digestToken = String((req.body && req.body.digest_token) || (req.body && req.body.token) || '').trim();
  digestTraceLog('send_digest_request', {
    digest_token: digestToken || null,
    recipient_count: users.length,
    recipients_preview: users.slice(0, 64),
  });
  const defaults = parseWecomSendDefaults();
  if (!defaults.can_app_send) {
    digestTraceLog('send_digest_blocked', { reason: 'wecom_app_not_configured' });
    return res.status(400).json({
      ok: false,
      error: '未配置企业微信应用消息（corp_id / agent_id / secret），无法按成员发送卡片',
    });
  }
  if (!fs.existsSync(SEND_DIGEST_SCRIPT)) {
    digestTraceLog('send_digest_blocked', { reason: 'script_missing' });
    return res.status(500).json({ ok: false, error: '未找到 send_digest_wecom.py' });
  }
  const py = process.env.PYTHON || 'python';
  const input = JSON.stringify({ touser_list: users, digest_token: digestToken });
  const r = spawnSync(py, [SEND_DIGEST_SCRIPT], {
    input,
    encoding: 'utf-8',
    maxBuffer: 2 * 1024 * 1024,
    cwd: PROJECT_ROOT,
    timeout: 60000,
    windowsHide: true,
    env: pythonSpawnEnv(),
  });
  if (r.error) {
    digestTraceLog('send_digest_spawn_error', { error: r.error.message || String(r.error) });
    return res.status(500).json({ ok: false, error: r.error.message || String(r.error) });
  }
  if (r.stderr && String(r.stderr).trim()) {
    digestTraceLog('send_digest_py_stderr', { stderr: String(r.stderr).trim().slice(0, 2000) });
  }
  const out = (r.stdout || '').trim();
  let parsed;
  try {
    const line = out.split(/\r?\n/).filter(Boolean).pop() || '{}';
    parsed = JSON.parse(line);
  } catch (e) {
    digestTraceLog('send_digest_bad_json', { stdout_tail: (out || '').slice(0, 500), exit_code: r.status });
    return res.status(500).json({
      ok: false,
      error: '发送脚本输出非 JSON',
      detail: (out || '').slice(0, 500),
      code: r.status,
    });
  }
  if (!parsed.ok) {
    digestTraceLog('send_digest_script_fail', { error: parsed.error || '发送失败' });
    return res.status(400).json({ ok: false, error: parsed.error || '发送失败' });
  }
  digestTraceLog('send_digest_ok', {
    sent: parsed.sent || [],
    failed: parsed.failed || [],
    auto_route: parsed.auto_route,
  });
  return res.json({
    ok: true,
    sent: parsed.sent || [],
    failed: parsed.failed || [],
    auto_route: parsed.auto_route,
  });
}

app.get('/ztb/api/review/digest_pending', handleDigestPendingApi);
app.get('/api/review/digest_pending', handleDigestPendingApi);
app.post('/ztb/api/review/send_digest', handleSendDigestApi);
app.post('/api/review/send_digest', handleSendDigestApi);
app.get('/ztb/api/digest_review/list', handleDigestReviewList);
app.get('/api/digest_review/list', handleDigestReviewList);
app.get('/ztb/api/digest_review/matched_recipients', handleDigestMatchedRecipients);
app.get('/api/digest_review/matched_recipients', handleDigestMatchedRecipients);
app.get('/ztb-test/api/digest_review/matched_recipients', handleDigestMatchedRecipients);
app.get('/ztb/digest_review', sendDigestReviewPage);
app.get('/digest_review', sendDigestReviewPage);
app.post('/ztb/api/digest_review/remove_item', handleDigestReviewRemoveItem);

/**
 * 详情页 HTML（注入 record_id）。须写在「剥离 /ztb、/ztb-test」中间件之前：
 * 部分 Nginx 只把完整路径转发到 Node（不剥前缀），此时必须先匹配 /ztb-test/detail/:id，
 * 否则会落到默认站点的 404，请求根本进不了 Node。
 */
function sendDetailHtmlPage(req, res) {
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
    Pragma: 'no-cache',
  });
  res.type('html').send(html);
}
app.get('/ztb-test/detail/:recordId', sendDetailHtmlPage);
app.get('/ztb/detail/:recordId', sendDetailHtmlPage);

function sendReadLogsPage(req, res) {
  if (!assertDashboardKey(req)) {
    return res.status(403).type('html').send('<p style="padding:24px;font-family:sans-serif">无权访问。请在 config.ini [web_view] 配置 dashboard_secret，并于 URL 附加 ?key=密钥</p>');
  }
  const htmlPath = path.join(VIEWS_DIR, 'read_logs.html');
  if (!fs.existsSync(htmlPath)) {
    return res.status(500).send('views/read_logs.html not found');
  }
  res.set({
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    Pragma: 'no-cache',
  });
  res.sendFile(htmlPath);
}

/** 部分 Nginx 不剥 /ztb、/ztb-test 前缀时须直接匹配完整路径（与 detail 页同理） */
app.get('/ztb/read_logs', sendReadLogsPage);
app.get('/ztb-test/read_logs', sendReadLogsPage);

/**
 * 浏览器在 /ztb/review、/ztb/api/... 下访问时，将 URL 剥成 /review、/api/...，
 * 与根路径共用同一套路由（避免重复注册）。
 * 改写 req.url 后清除 parseurl 缓存，否则后续路由可能仍按旧 path 匹配失败。
 */
app.use((req, res, next) => {
  const raw = req.originalUrl || '';
  const pathOnly = raw.split('?')[0];
  const qs = raw.includes('?') ? raw.slice(raw.indexOf('?')) : '';
  if (pathOnly === '/ztb' || pathOnly.startsWith('/ztb/')) {
    const rest = pathOnly === '/ztb' ? '/' : pathOnly.slice(4) || '/';
    req.url = rest + qs;
    delete req._parsedUrl;
  } else if (pathOnly === '/ztb-test' || pathOnly.startsWith('/ztb-test/')) {
    const rest = pathOnly === '/ztb-test' ? '/' : pathOnly.slice('/ztb-test'.length) || '/';
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

/**
 * MySQL DATETIME → JSON 前格式化为东八区墙钟字符串。
 * mysql2 读出为 JS Date，res.json 默认会变成 UTC 的 ISO（如 …T07:24Z），前端直接展示会少 8 小时。
 */
function formatDateTimeAsiaShanghai(value) {
  if (value == null || value === '') return null;
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(d);
  const pick = (t) => (parts.find((x) => x.type === t) || {}).value || '';
  const y = pick('year');
  if (!y) return String(value);
  return `${y}-${pick('month')}-${pick('day')} ${pick('hour')}:${pick('minute')}:${pick('second')}`;
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

/** 与 digest_message.materialize_digest_pack 同一套日期窗（入库日）与审核条件 */
async function fetchDigestPackItemsFromDb(p, digestDateYmd) {
  const ds = String(digestDateYmd || '').trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(ds)) return [];
  const de = ds;
  const sql = `
    SELECT id, title, sub_type, product_related, reserve2 AS product_related_terms,
           project_no, project_budget, winning_amount, bidding_method,
           project_owner, owner_contact, owner_phone,
           winning_bidder, bidding_agent,
           published_at, bid_deadline, detail_url, created_at, audit_status
    FROM scraping_infos
    WHERE ${SQL_AUDIT_APPROVED}
    AND (
      (DATE(created_at) >= ? AND DATE(created_at) <= DATE_ADD(?, INTERVAL 1 DAY))
      OR (DATE(updated_at) >= ? AND DATE(updated_at) <= DATE_ADD(?, INTERVAL 1 DAY))
    )
    ORDER BY id DESC
    LIMIT 8000`;
  const [rows] = await p.execute(sql, [ds, de, ds, de]);
  return (rows || []).map(serializeRow);
}

/** 与 /api/latest_date 一致：最近一条审核通过记录的入库日 */
async function getLatestApprovedIngestDate(p) {
  const [rows] = await p.execute(
    `SELECT DATE_FORMAT(MAX(d), '%Y-%m-%d') AS d FROM (
      SELECT DATE(created_at) AS d FROM scraping_infos WHERE ${SQL_AUDIT_APPROVED}
      UNION ALL
      SELECT DATE(updated_at) AS d FROM scraping_infos WHERE ${SQL_AUDIT_APPROVED}
    ) x WHERE d IS NOT NULL`
  );
  return String((rows && rows[0] && rows[0].d) || '').trim();
}

async function digestTokenHasDispatchRecord(p, token) {
  const [rows] = await p.execute(
    'SELECT 1 FROM wecom_card_dispatch_log WHERE LOWER(TRIM(IFNULL(digest_token,\'\'))) = ? LIMIT 1',
    [token]
  );
  return !!(rows && rows.length);
}

/** 企微 userid（org_user.user_account）→ 中文名；用于路由表里 presale_display_name 为空时的展示兜底 */
async function resolveOrgDisplayNamesForAccounts(pool, accounts) {
  const uniq = [...new Set((accounts || []).map((a) => String(a || '').trim()).filter(Boolean))];
  if (!uniq.length || !pool) return new Map();
  const iniPath = readConfigIniPath();
  if (!iniPath) return new Map();
  try {
    const cfg = require('ini').parse(fs.readFileSync(iniPath, 'utf-8'));
    const dc = cfg.dispatch || cfg.Dispatch || {};
    const tbl = String(dc.org_user_table || 'org_user').trim();
    const nameCol = String(dc.org_user_name_col || 'user_name').trim();
    const accountCol = String(dc.org_user_account_col || 'user_account').trim();
    const delCol = String(dc.org_user_deleted_col || 'is_deleted').trim();
    const ph = uniq.map(() => '?').join(',');
    const [nrows] = await pool.execute(
      `SELECT \`${accountCol}\` AS acct, \`${nameCol}\` AS nm FROM \`${tbl}\` WHERE \`${accountCol}\` IN (${ph}) AND (\`${delCol}\` = 0 OR \`${delCol}\` IS NULL)`,
      uniq
    );
    const m = new Map();
    for (const r of nrows || []) {
      const a = String(r.acct || '').trim();
      const nm = String(r.nm || '').trim();
      if (a && nm) m.set(a, nm);
    }
    return m;
  } catch (e) {
    return new Map();
  }
}

/** 将 digest_item_presale_route 预计算结果挂到每条 manifest 上，供 H5 卡片头展示「匹配客户 / @售前」 */
async function mergeDigestItemPresaleRoutes(pool, token, items) {
  if (!pool || !items || !items.length) return items;
  const t = String(token || '')
    .trim()
    .toLowerCase();
  if (!/^[a-f0-9]{32}$/.test(t)) return items;
  try {
    await ensureDispatchTables(pool);
    const [rows] = await pool.execute(
      'SELECT record_id, presale_userid, presale_display_name, canonical_customer FROM digest_item_presale_route WHERE digest_token = ?',
      [t]
    );
    const needAccounts = [];
    for (const r of rows || []) {
      const pn = String(r.presale_display_name || '').trim();
      const uid = String(r.presale_userid || '').trim();
      if (!pn && uid) needAccounts.push(uid);
    }
    const uidToName = await resolveOrgDisplayNamesForAccounts(pool, needAccounts);
    const byId = new Map();
    for (const r of rows || []) {
      const id = Number(r.record_id);
      if (!id || Number.isNaN(id)) continue;
      if (!byId.has(id)) byId.set(id, { names: new Set(), customers: new Set() });
      const ent = byId.get(id);
      const pn = String(r.presale_display_name || '').trim();
      const uid = String(r.presale_userid || '').trim();
      const disp = pn || uidToName.get(uid) || '';
      const cc = String(r.canonical_customer || '').trim();
      if (disp) ent.names.add(disp);
      if (cc) ent.customers.add(cc);
    }
    return items.map((it) => {
      const id = Number(it.id);
      const ex = byId.get(id);
      if (!ex) return it;
      const digest_presale_names = Array.from(ex.names);
      const digest_matched_customers = Array.from(ex.customers);
      return {
        ...it,
        ...(digest_presale_names.length ? { digest_presale_names } : {}),
        ...(digest_matched_customers.length ? { digest_matched_customers } : {}),
      };
    });
  } catch (e) {
    console.warn('[digest_pack] mergeDigestItemPresaleRoutes', e.message || e);
    return items;
  }
}

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

// 爬取详情页（前缀已由中间件剥掉时走此处）
app.get('/detail/:recordId', sendDetailHtmlPage);

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

/** 摘要列表「我的 @ 置顶」：解析当前查看者 userid（企微拼音账号） */
async function resolveDigestViewerUserid(pool, token, req) {
  const qv = String((req.query && req.query.viewer) || (req.query && req.query.wx_userid) || '')
    .trim()
    .toLowerCase();
  if (qv) return qv;
  const did = String((req.query && req.query.dispatch_id) || '').trim();
  if (!did || !pool) return '';
  try {
    await ensureDispatchTables(pool);
    const [rows] = await pool.execute(
      'SELECT `receiver_userid`, LOWER(TRIM(IFNULL(`digest_token`,\'\'))) AS dt FROM `wecom_card_dispatch_log` WHERE `dispatch_id` = ? LIMIT 1',
      [did]
    );
    if (!rows || !rows.length) return '';
    const tok = String(token || '')
      .trim()
      .toLowerCase();
    const rowDt = String(rows[0].dt || '').trim().toLowerCase();
    const recv = String(rows[0].receiver_userid || '')
      .trim()
      .toLowerCase();
    if (!recv) return '';
    if (rowDt === tok) return recv;
    /**
     * 分发表里 digest_token 可能为 NULL、旧包名或与当前 manifest 不一致（补发/复制链接/历史脚本未写 token），
     * 但用户已打开 /digest/<pathToken>?dispatch_id=…。置顶查询 digest_item_presale_route 用的是 path 上 token，
     * 此处仍应用 receiver_userid，与 handleCardOpen 用 URL 上 token 降级跳转的语义一致。
     */
    if (/^[a-f0-9]{32}$/.test(tok)) {
      digestTraceLog('digest_pack_viewer_token_mismatch_use_receiver', {
        dispatch_id: did,
        path_digest_token: tok,
        row_digest_token: rowDt || null,
        receiver_userid: recv,
      });
      return recv;
    }
    return '';
  } catch (e) {
    return '';
  }
}

/** 与 digest_item_presale_route 对照：本条是否路由到当前 viewer */
async function pinMyRoutedPresaleItems(pool, token, items, viewerUid) {
  if (!pool || !items || !items.length || !viewerUid) return items;
  const u = String(viewerUid)
    .trim()
    .toLowerCase();
  if (!u) return items;
  try {
    await ensureDispatchTables(pool);
    const t = String(token || '')
      .trim()
      .toLowerCase();
    const [prRows] = await pool.execute(
      'SELECT DISTINCT `record_id` FROM `digest_item_presale_route` WHERE LOWER(TRIM(`digest_token`)) = ? AND LOWER(TRIM(`presale_userid`)) = ?',
      [t, u]
    );
    const pinIds = new Set();
    for (const r of prRows || []) {
      const id = Number(r.record_id);
      if (id && !Number.isNaN(id)) pinIds.add(id);
    }
    if (!pinIds.size) return items;
    return items.map((it) => {
      const id = Number(it.id);
      return pinIds.has(id) ? { ...it, my_routed_presale: true } : it;
    });
  } catch (e) {
    console.warn('[digest_pack] pinMyRoutedPresaleItems', e.message || e);
    return items;
  }
}

/** 企微摘要卡片专用：manifest 缺失或 items 为空时，按 digest_date 从库补全（与 Python 摘要包口径一致） */
async function handleDigestPackApi(req, res) {
  try {
    const token = String(req.params.token || '').trim().toLowerCase();
    if (!/^[a-f0-9]{32}$/.test(token)) {
      return res.status(400).json({ ok: false, error: '无效的链接' });
    }
    const fp = path.join(getDigestPacksDir(), token, 'manifest.json');
    const pen = readDigestPendingMeta();
    const pendingMatches = !!(pen && String(pen.token || '').trim().toLowerCase() === token);

    let manifestDigestDate = '';
    let generatedAt = '';
    let items = [];
    let hadManifestFile = false;

    if (fs.existsSync(fp)) {
      hadManifestFile = true;
      try {
        const raw = fs.readFileSync(fp, 'utf8');
        const data = JSON.parse(raw);
        manifestDigestDate = String(data.digest_date || '').trim();
        generatedAt = String(data.generated_at || '').trim();
        items = Array.isArray(data.items) ? data.items : [];
      } catch (e) {
        console.error('[api/digest_pack]', e);
        return res.status(500).json({ ok: false, error: '读取摘要包失败' });
      }
    } else if (pendingMatches) {
      manifestDigestDate = String(pen.digest_date || '').trim();
      generatedAt = String(pen.generated_at || '').trim();
      items = [];
    } else {
      const pool0 = getPool();
      if (pool0 && (await digestTokenHasDispatchRecord(pool0, token))) {
        manifestDigestDate = await getLatestApprovedIngestDate(pool0);
        generatedAt = '';
        items = [];
        console.warn(
          '[api/digest_pack] 无 manifest 且 pending 无此 token，但分发表有该 digest_token，按库中最新入库日补全 token=%s',
          token
        );
      } else {
        return res.status(404).json({ ok: false, error: '摘要已失效或未发布' });
      }
    }

    const pool = getPool();
    let hydratedFromDb = false;
    if (pool && manifestDigestDate && /^\d{4}-\d{2}-\d{2}$/.test(manifestDigestDate) && items.length === 0) {
      try {
        const dbItems = await fetchDigestPackItemsFromDb(pool, manifestDigestDate);
        if (dbItems.length > 0) {
          items = dbItems;
          hydratedFromDb = true;
        }
      } catch (e) {
        console.error('[api/digest_pack hydrate]', e);
      }
    }

    const degraded_no_manifest = !hadManifestFile && !hydratedFromDb;

    let mergedItems = items;
    if (pool && mergedItems.length > 0) {
      mergedItems = await mergeDigestItemPresaleRoutes(pool, token, mergedItems);
    }

    let viewerUid = '';
    if (pool) {
      viewerUid = await resolveDigestViewerUserid(pool, token, req);
    }
    if (pool && mergedItems.length > 0 && viewerUid) {
      mergedItems = await pinMyRoutedPresaleItems(pool, token, mergedItems, viewerUid);
    }

    return res.json({
      ok: true,
      digest_date: manifestDigestDate,
      generated_at: generatedAt,
      items: mergedItems,
      degraded_no_manifest,
      hydrated_from_db: hydratedFromDb,
    });
  } catch (e) {
    console.error('[api/digest_pack]', e);
    return res.status(500).json({ ok: false, error: e.message || '服务器错误' });
  }
}
app.get('/api/digest_pack/:token', handleDigestPackApi);

async function ensureDispatchTables(pool) {
  await pool.execute(
    `CREATE TABLE IF NOT EXISTS wecom_card_dispatch_log (
      id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      dispatch_id VARCHAR(40) NOT NULL,
      digest_token VARCHAR(32) DEFAULT NULL,
      receiver_userid VARCHAR(128) NOT NULL,
      receiver_customer VARCHAR(255) DEFAULT NULL,
      item_count INT NOT NULL DEFAULT 0,
      send_status VARCHAR(16) NOT NULL DEFAULT 'SENT',
      send_error VARCHAR(500) DEFAULT NULL,
      first_read_at DATETIME DEFAULT NULL,
      last_read_at DATETIME DEFAULT NULL,
      read_count INT NOT NULL DEFAULT 0,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      UNIQUE KEY uk_dispatch_id (dispatch_id),
      KEY idx_receiver_created (receiver_userid, created_at),
      KEY idx_digest_token (digest_token)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci`
  );
  await pool.execute(
    `CREATE TABLE IF NOT EXISTS wecom_card_item_read_log (
      id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
      dispatch_id VARCHAR(40) NOT NULL,
      record_id BIGINT NOT NULL,
      reader_userid VARCHAR(128) NOT NULL,
      read_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      dwell_seconds INT UNSIGNED DEFAULT NULL,
      UNIQUE KEY uk_dispatch_record_reader (dispatch_id, record_id, reader_userid),
      KEY idx_reader_read_at (reader_userid, read_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci`
  );
  try {
    await pool.execute(
      'ALTER TABLE wecom_card_item_read_log ADD COLUMN dwell_seconds INT UNSIGNED DEFAULT NULL COMMENT "详情页停留秒数" AFTER read_at'
    );
  } catch (e) {
    const code = e && (e.code || e.errno);
    const msg = (e && e.message) || '';
    if (code !== 'ER_DUP_FIELDNAME' && msg.indexOf('Duplicate column') < 0) {
      console.warn('[ensureDispatchTables] dwell_seconds', msg);
    }
  }
  await pool.execute(
    `CREATE TABLE IF NOT EXISTS digest_item_presale_route (
      digest_token CHAR(32) NOT NULL COMMENT '摘要包目录名',
      record_id BIGINT NOT NULL COMMENT 'scraping_infos.id',
      presale_userid VARCHAR(128) NOT NULL,
      presale_display_name VARCHAR(500) DEFAULT NULL,
      canonical_customer VARCHAR(500) DEFAULT NULL,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (digest_token, record_id, presale_userid),
      KEY idx_dt_uid (digest_token, presale_userid)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci`
  );
  await pool.execute(
    `CREATE TABLE IF NOT EXISTS forward_detail_ticket (
      ticket CHAR(32) NOT NULL,
      record_id BIGINT NOT NULL,
      to_userid VARCHAR(128) NOT NULL,
      digest_token CHAR(32) DEFAULT NULL COMMENT '从摘要页转发时可选，用于阅读名单过滤',
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (ticket),
      KEY idx_record (record_id),
      KEY idx_digest (digest_token, record_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='单条转发详情链 fr=，用于记录指定接收人阅读/停留'`
  );
}

/**
 * 对外路径前缀（/ztb 或 /ztb-test）。直连 Node 时可从 originalUrl 推断；经 Nginx 剥前缀后须依赖 X-ZTB-Public-Prefix，
 * 否则 handleCardOpen 会 302 到 /digest/…，裸 /digest 会按 Referer 落到错误 Node 端口（测试环境表现为「链接已失效」）。
 */
function publicPathPrefix(req) {
  const fromProxy = String(req.headers['x-ztb-public-prefix'] || '').trim();
  if (fromProxy === '/ztb-test' || fromProxy === '/ztb') return fromProxy;
  const pathOnly = (req.originalUrl || '').split('?')[0];
  if (pathOnly === '/ztb-test' || pathOnly.startsWith('/ztb-test/')) return '/ztb-test';
  if (pathOnly === '/ztb' || pathOnly.startsWith('/ztb/')) return '/ztb';
  return '';
}

async function handleCardOpen(req, res) {
  const dispatchId = String(req.query.dispatch_id || '').trim();
  const digestTokFallback = String(req.query.digest_token || '')
    .trim()
    .toLowerCase();
  const hex32 = /^[a-f0-9]{32}$/;
  if (!dispatchId) {
    if (hex32.test(digestTokFallback)) {
      digestTraceLog('card_open_redirect_only_token', {
        digest_token: digestTokFallback,
        client_ip: clientIpHint(req),
      });
      const prefix = publicPathPrefix(req);
      return res.redirect(`${prefix}/digest/${digestTokFallback}`);
    }
    digestTraceLog('card_open_bad_request', { reason: 'missing_dispatch_id' });
    return res.status(400).send('缺少 dispatch_id');
  }
  const p = getPool();
  if (!p) {
    digestTraceLog('card_open_error', { dispatch_id: dispatchId, reason: 'no_db_pool' });
    return res.status(500).send('数据库未配置');
  }
  try {
    await ensureDispatchTables(p);
    const [rows] = await p.execute(
      'SELECT digest_token, receiver_userid, first_read_at FROM wecom_card_dispatch_log WHERE dispatch_id = ? LIMIT 1',
      [dispatchId]
    );
    if (!rows || rows.length === 0) {
      /* Python 写入的分发表若在本机库而 Node 连的是其它库，会查不到；可用 URL 上 digest_token 降级进摘要页 */
      digestTraceLog('card_open_no_dispatch_row', {
        dispatch_id: dispatchId,
        digest_token_fallback: hex32.test(digestTokFallback) ? digestTokFallback : null,
        client_ip: clientIpHint(req),
      });
      if (hex32.test(digestTokFallback)) {
        const prefix = publicPathPrefix(req);
        return res.redirect(`${prefix}/digest/${digestTokFallback}`);
      }
      return res.status(404).send('分发记录不存在');
    }
    const receiverUserid = String(rows[0].receiver_userid || '').trim();
    const firstOpen = rows[0].first_read_at == null;
    const tok = String(rows[0].digest_token || '').trim().toLowerCase();
    if (!hex32.test(tok) && hex32.test(digestTokFallback)) {
      digestTraceLog('card_open_fallback_digest_token', {
        dispatch_id: dispatchId,
        receiver_userid: receiverUserid,
        digest_token_url: digestTokFallback,
      });
      const prefix = publicPathPrefix(req);
      return res.redirect(
        `${prefix}/digest/${digestTokFallback}?dispatch_id=${encodeURIComponent(dispatchId)}`
      );
    }
    if (!hex32.test(tok)) {
      digestTraceLog('card_open_invalid_digest_binding', { dispatch_id: dispatchId, receiver_userid: receiverUserid });
      return res.status(400).send('分发记录未绑定有效摘要');
    }
    await p.execute(
      `UPDATE wecom_card_dispatch_log
       SET first_read_at = IF(first_read_at IS NULL, NOW(), first_read_at),
           last_read_at = NOW(),
           read_count = IFNULL(read_count,0) + 1
       WHERE dispatch_id = ?`,
      [dispatchId]
    );
    digestTraceLog('card_open_redirect_digest', {
      dispatch_id: dispatchId,
      receiver_userid: receiverUserid,
      digest_token: tok,
      first_open: firstOpen,
      client_ip: clientIpHint(req),
    });
    const prefix = publicPathPrefix(req);
    const loc = `${prefix}/digest/${tok}?dispatch_id=${encodeURIComponent(dispatchId)}`;
    return res.redirect(loc);
  } catch (e) {
    digestTraceLog('card_open_exception', { dispatch_id: dispatchId, error: e.message || String(e) });
    return res.status(500).send(e.message || '打开卡片失败');
  }
}

app.get('/api/card/open', handleCardOpen);

app.post('/api/read/mark', async (req, res) => {
  const recordId = parseInt(req.body && req.body.record_id, 10);
  const forwardTicket = String((req.body && req.body.forward_ticket) || (req.body && req.body.fr) || '').trim().toLowerCase();
  const dispatchId = String((req.body && req.body.dispatch_id) || '').trim();
  if (Number.isNaN(recordId) || recordId < 1) {
    return res.status(400).json({ ok: false, error: '参数错误' });
  }
  const p = getPool();
  if (!p) return res.status(500).json({ ok: false, error: poolError || '未配置数据库' });
  try {
    await ensureDispatchTables(p);
    if (forwardTicket && /^[a-f0-9]{32}$/.test(forwardTicket)) {
      const [trows] = await p.execute(
        'SELECT to_userid FROM forward_detail_ticket WHERE ticket = ? AND record_id = ? LIMIT 1',
        [forwardTicket, recordId]
      );
      if (!trows || !trows.length) {
        return res.status(404).json({ ok: false, error: '转发凭证无效或已过期' });
      }
      const uid = String(trows[0].to_userid || '').trim();
      await p.execute(
        `INSERT INTO wecom_card_item_read_log(dispatch_id, record_id, reader_userid)
         VALUES (?, ?, ?)
         ON DUPLICATE KEY UPDATE read_at = NOW()`,
        [forwardTicket, recordId, uid]
      );
      digestTraceLog('detail_read_mark', {
        via: 'forward_ticket',
        forward_ticket: forwardTicket,
        record_id: recordId,
        reader_userid: uid,
      });
      return res.json({ ok: true, via: 'forward_ticket' });
    }
    if (!dispatchId) {
      return res.status(400).json({ ok: false, error: '缺少 dispatch_id 或 forward_ticket' });
    }
    const [rows] = await p.execute(
      'SELECT receiver_userid FROM wecom_card_dispatch_log WHERE dispatch_id = ? LIMIT 1',
      [dispatchId]
    );
    if (!rows || rows.length === 0) return res.status(404).json({ ok: false, error: '分发记录不存在' });
    const uid = String(rows[0].receiver_userid || '').trim();
    await p.execute(
      `INSERT INTO wecom_card_item_read_log(dispatch_id, record_id, reader_userid)
       VALUES (?, ?, ?)
       ON DUPLICATE KEY UPDATE read_at = NOW()`,
      [dispatchId, recordId, uid]
    );
    digestTraceLog('detail_read_mark', {
      via: 'digest_dispatch',
      dispatch_id: dispatchId,
      record_id: recordId,
      reader_userid: uid,
    });
    return res.json({ ok: true });
  } catch (e) {
    return res.status(500).json({ ok: false, error: e.message || '标记已阅失败' });
  }
});

app.get('/api/read/status', async (req, res) => {
  const dispatchId = String(req.query.dispatch_id || '').trim();
  if (!dispatchId) return res.json({ ok: true, read_record_ids: [] });
  const p = getPool();
  if (!p) return res.json({ ok: true, read_record_ids: [] });
  try {
    await ensureDispatchTables(p);
    const [rows] = await p.execute(
      `SELECT record_id FROM wecom_card_item_read_log WHERE dispatch_id = ?`,
      [dispatchId]
    );
    const ids = (rows || []).map((r) => Number(r.record_id)).filter((n) => Number.isFinite(n));
    return res.json({ ok: true, read_record_ids: ids });
  } catch (e) {
    return res.json({ ok: true, read_record_ids: [] });
  }
});

app.post('/api/forward/digest', (req, res) => {
  const tok = String((req.body && req.body.token) || '').trim().toLowerCase();
  const raw = (req.body && req.body.touser_ids) || [];
  const users = Array.isArray(raw) ? raw.map((u) => String(u || '').trim()).filter(Boolean) : [];
  digestTraceLog('forward_digest_request', {
    digest_token: tok || null,
    recipient_count: users.length,
    recipients_preview: users.slice(0, 64),
  });
  if (!/^[a-f0-9]{32}$/.test(tok)) {
    return res.status(400).json({ ok: false, error: '无效 token' });
  }
  if (!users.length) {
    return res.status(400).json({ ok: false, error: '请提供至少一个接收人 userid' });
  }
  if (!fs.existsSync(SEND_DIGEST_SCRIPT)) {
    return res.status(500).json({ ok: false, error: '未找到 send_digest_wecom.py' });
  }
  const py = process.env.PYTHON || 'python';
  const input = JSON.stringify({ digest_token: tok, touser_list: users });
  const r = spawnSync(py, [SEND_DIGEST_SCRIPT], {
    input,
    encoding: 'utf-8',
    maxBuffer: 2 * 1024 * 1024,
    cwd: PROJECT_ROOT,
    timeout: 60000,
    windowsHide: true,
    env: pythonSpawnEnv(),
  });
  if (r.error) {
    digestTraceLog('forward_digest_spawn_error', { error: r.error.message || String(r.error) });
    return res.status(500).json({ ok: false, error: r.error.message || String(r.error) });
  }
  if (r.stderr && String(r.stderr).trim()) {
    digestTraceLog('forward_digest_py_stderr', { stderr: String(r.stderr).trim().slice(0, 2000) });
  }
  const out = (r.stdout || '').trim();
  try {
    const line = out.split(/\r?\n/).filter(Boolean).pop() || '{}';
    const parsed = JSON.parse(line);
    if (!parsed.ok) {
      digestTraceLog('forward_digest_script_fail', { error: parsed.error || '转发失败' });
      return res.status(400).json({ ok: false, error: parsed.error || '转发失败' });
    }
    digestTraceLog('forward_digest_ok', { sent: parsed.sent || [], failed: parsed.failed || [] });
    return res.json({ ok: true, sent: parsed.sent || [], failed: parsed.failed || [] });
  } catch (e) {
    digestTraceLog('forward_digest_bad_json', { stdout_tail: (out || '').slice(0, 500) });
    return res.status(500).json({ ok: false, error: '转发脚本输出非 JSON' });
  }
});

/** 摘要页打开：补记「看过概览」（不增加 read_count，避免刷新刷屏；企微卡片首次打开仍由 /api/card/open 计次） */
app.post('/api/read/overview', async (req, res) => {
  const dispatchId = String((req.body && req.body.dispatch_id) || '').trim();
  if (!dispatchId) return res.status(400).json({ ok: false, error: '缺少 dispatch_id' });
  const p = getPool();
  if (!p) return res.status(500).json({ ok: false, error: poolError || '未配置数据库' });
  try {
    await ensureDispatchTables(p);
    const [rows] = await p.execute(
      'SELECT dispatch_id, receiver_userid FROM wecom_card_dispatch_log WHERE dispatch_id = ? LIMIT 1',
      [dispatchId]
    );
    if (!rows || !rows.length) return res.status(404).json({ ok: false, error: '分发记录不存在' });
    const receiverUserid = String(rows[0].receiver_userid || '').trim();
    await p.execute(
      `UPDATE wecom_card_dispatch_log
       SET first_read_at = IF(first_read_at IS NULL, NOW(), first_read_at),
           last_read_at = NOW()
       WHERE dispatch_id = ?`,
      [dispatchId]
    );
    digestTraceLog('digest_overview_ping', {
      dispatch_id: dispatchId,
      receiver_userid: receiverUserid,
      client_ip: clientIpHint(req),
    });
    return res.json({ ok: true });
  } catch (e) {
    return res.status(500).json({ ok: false, error: e.message || '记录失败' });
  }
});

app.post('/api/read/dwell', async (req, res) => {
  const recordId = parseInt(req.body && req.body.record_id, 10);
  const forwardTicket = String((req.body && req.body.forward_ticket) || (req.body && req.body.fr) || '').trim().toLowerCase();
  const dispatchId = String((req.body && req.body.dispatch_id) || '').trim();
  let seconds = parseInt(req.body && req.body.dwell_seconds, 10);
  if (Number.isNaN(seconds) || seconds < 0) seconds = 0;
  if (seconds > 86400) seconds = 86400;
  if (Number.isNaN(recordId) || recordId < 1) {
    return res.status(400).json({ ok: false, error: '参数错误' });
  }
  const p = getPool();
  if (!p) return res.status(500).json({ ok: false, error: poolError || '未配置数据库' });
  try {
    await ensureDispatchTables(p);
    if (forwardTicket && /^[a-f0-9]{32}$/.test(forwardTicket)) {
      const [trows] = await p.execute(
        'SELECT to_userid FROM forward_detail_ticket WHERE ticket = ? AND record_id = ? LIMIT 1',
        [forwardTicket, recordId]
      );
      if (!trows || !trows.length) {
        return res.status(404).json({ ok: false, error: '转发凭证无效' });
      }
      const uid = String(trows[0].to_userid || '').trim();
      await p.execute(
        `UPDATE wecom_card_item_read_log
         SET dwell_seconds = GREATEST(COALESCE(dwell_seconds, 0), ?)
         WHERE dispatch_id = ? AND record_id = ? AND reader_userid = ?`,
        [seconds, forwardTicket, recordId, uid]
      );
      digestTraceLog('detail_dwell', {
        via: 'forward_ticket',
        forward_ticket: forwardTicket,
        record_id: recordId,
        reader_userid: uid,
        dwell_seconds: seconds,
      });
      return res.json({ ok: true, via: 'forward_ticket' });
    }
    if (!dispatchId) {
      return res.status(400).json({ ok: false, error: '缺少 dispatch_id 或 forward_ticket' });
    }
    const [rows] = await p.execute(
      'SELECT receiver_userid FROM wecom_card_dispatch_log WHERE dispatch_id = ? LIMIT 1',
      [dispatchId]
    );
    if (!rows || !rows.length) return res.status(404).json({ ok: false, error: '分发记录不存在' });
    const uid = String(rows[0].receiver_userid || '').trim();
    await p.execute(
      `UPDATE wecom_card_item_read_log
       SET dwell_seconds = GREATEST(COALESCE(dwell_seconds, 0), ?)
       WHERE dispatch_id = ? AND record_id = ? AND reader_userid = ?`,
      [seconds, dispatchId, recordId, uid]
    );
    digestTraceLog('detail_dwell', {
      via: 'digest_dispatch',
      dispatch_id: dispatchId,
      record_id: recordId,
      reader_userid: uid,
      dwell_seconds: seconds,
    });
    return res.json({ ok: true });
  } catch (e) {
    return res.status(500).json({ ok: false, error: e.message || '记录失败' });
  }
});

async function handleReadSummary(req, res) {
  const dispatchId = String((req.params && req.params.dispatchId) || req.query.dispatch_id || '').trim();
  if (!dispatchId) return res.json({ ok: false, error: '缺少 dispatch_id' });
  const p = getPool();
  if (!p) return res.json({ ok: false, error: poolError || '未配置数据库' });
  try {
    await ensureDispatchTables(p);
    const [drows] = await p.execute(
      `SELECT receiver_userid, first_read_at, last_read_at, read_count
       FROM wecom_card_dispatch_log WHERE dispatch_id = ? LIMIT 1`,
      [dispatchId]
    );
    if (!drows || !drows.length) return res.json({ ok: false, error: '分发记录不存在' });
    const receiverUserid = String(drows[0].receiver_userid || '').trim();
    let receiverName = '';
    try {
      const iniPath = readConfigIniPath();
      if (iniPath && receiverUserid) {
        const cfg = require('ini').parse(fs.readFileSync(iniPath, 'utf-8'));
        const dc = cfg.dispatch || cfg.Dispatch || {};
        const t = String(dc.org_user_table || 'org_user').trim();
        const nameCol = String(dc.org_user_name_col || 'user_name').trim();
        const accountCol = String(dc.org_user_account_col || 'user_account').trim();
        const delCol = String(dc.org_user_deleted_col || 'is_deleted').trim();
        const [nrows] = await p.execute(
          `SELECT \`${nameCol}\` AS nm FROM \`${t}\` WHERE \`${accountCol}\` = ? AND (\`${delCol}\` = 0 OR \`${delCol}\` IS NULL) LIMIT 1`,
          [receiverUserid]
        );
        if (nrows && nrows.length) {
          receiverName = String(nrows[0].nm || '').trim();
        }
      }
    } catch (e) {
      // 不阻断主流程：取不到中文名时回退显示 userid
      receiverName = '';
    }
    const [items] = await p.execute(
      `SELECT record_id, read_at, dwell_seconds FROM wecom_card_item_read_log WHERE dispatch_id = ? ORDER BY record_id`,
      [dispatchId]
    );
    return res.json({
      ok: true,
      receiver_userid: receiverUserid,
      receiver_name: receiverName,
      first_read_at: formatDateTimeAsiaShanghai(drows[0].first_read_at),
      last_read_at: formatDateTimeAsiaShanghai(drows[0].last_read_at),
      read_count: drows[0].read_count,
      detail_reads: (items || []).map((r) => ({
        record_id: Number(r.record_id),
        read_at: formatDateTimeAsiaShanghai(r.read_at),
        dwell_seconds: r.dwell_seconds != null ? Number(r.dwell_seconds) : null,
      })),
    });
  } catch (e) {
    return res.status(500).json({ ok: false, error: e.message || '查询失败' });
  }
}

app.get('/api/read/summary', handleReadSummary);
app.get('/ztb/api/read/summary', handleReadSummary);
app.get('/ztb-test/api/read/summary', handleReadSummary);
app.get('/api/read/summary/:dispatchId', handleReadSummary);
app.get('/ztb/api/read/summary/:dispatchId', handleReadSummary);
app.get('/ztb-test/api/read/summary/:dispatchId', handleReadSummary);

/** 摘要页 @售前：优先读 digest_item_presale_route（跑批写 manifest 时已预计算），避免点击时再跑 Python 导致 504 */
async function handleReadMatch(req, res) {
  const dispatchId = String((req.params && req.params.dispatchId) || req.query.dispatch_id || '').trim();
  if (!dispatchId) return res.json({ ok: false, error: '缺少 dispatch_id' });
  const p = getPool();
  if (!p) return res.status(500).json({ ok: false, error: poolError || '未配置数据库' });
  try {
    await ensureDispatchTables(p);
    const [drows] = await p.execute(
      'SELECT digest_token, receiver_userid FROM wecom_card_dispatch_log WHERE dispatch_id = ? LIMIT 1',
      [dispatchId]
    );
    if (!drows || !drows.length) {
      return res.status(400).json({ ok: false, error: '分发记录不存在' });
    }
    const tok = String(drows[0].digest_token || '').trim().toLowerCase();
    const receiver = String(drows[0].receiver_userid || '').trim();
    let receiverName = '';
    try {
      const iniPath = readConfigIniPath();
      if (iniPath && receiver) {
        const cfg = require('ini').parse(fs.readFileSync(iniPath, 'utf-8'));
        const dc = cfg.dispatch || cfg.Dispatch || {};
        const t = String(dc.org_user_table || 'org_user').trim();
        const nameCol = String(dc.org_user_name_col || 'user_name').trim();
        const accountCol = String(dc.org_user_account_col || 'user_account').trim();
        const delCol = String(dc.org_user_deleted_col || 'is_deleted').trim();
        const [nrows] = await p.execute(
          `SELECT \`${nameCol}\` AS nm FROM \`${t}\` WHERE \`${accountCol}\` = ? AND (\`${delCol}\` = 0 OR \`${delCol}\` IS NULL) LIMIT 1`,
          [receiver]
        );
        if (nrows && nrows.length) {
          receiverName = String(nrows[0].nm || '').trim();
        }
      }
    } catch (e) {
      receiverName = '';
    }

    if (tok && /^[a-f0-9]{32}$/.test(tok)) {
      const [cntRows] = await p.execute(
        'SELECT COUNT(*) AS c FROM digest_item_presale_route WHERE digest_token = ?',
        [tok]
      );
      const hasIndex = cntRows && cntRows.length && Number(cntRows[0].c) > 0;
      if (hasIndex) {
        const [mrows] = await p.execute(
          'SELECT DISTINCT record_id FROM digest_item_presale_route WHERE digest_token = ? AND presale_userid = ? ORDER BY record_id',
          [tok, receiver]
        );
        const matched_record_ids = (mrows || []).map((r) => Number(r.record_id)).filter((n) => n > 0);
        return res.json({
          ok: true,
          receiver_userid: receiver,
          receiver_name: receiverName,
          matched_record_ids,
          match_source: 'db',
        });
      }
    }

    if (!fs.existsSync(MATCH_BY_DISPATCH_SCRIPT)) {
      return res.status(500).json({
        ok: false,
        error:
          '摘要售前索引未生成：请重新执行跑批以写入 digest_item_presale_route（或部署 dispatch_match_by_dispatch.py 作兜底）',
      });
    }
    const py = process.env.PYTHON || 'python';
    const input = JSON.stringify({ dispatch_id: dispatchId });
    const r = spawnSync(py, [MATCH_BY_DISPATCH_SCRIPT], {
      input,
      encoding: 'utf-8',
      maxBuffer: 4 * 1024 * 1024,
      cwd: PROJECT_ROOT,
      timeout: 60000,
      windowsHide: true,
      env: pythonSpawnEnv(),
    });
    if (r.error) {
      return res.status(500).json({ ok: false, error: r.error.message || String(r.error) });
    }
    const out = (r.stdout || '').trim();
    try {
      const line = out.split(/\r?\n/).filter(Boolean).pop() || '{}';
      const parsed = JSON.parse(line);
      if (!parsed.ok) {
        return res.status(400).json({ ok: false, error: parsed.error || '匹配计算失败' });
      }
      parsed.match_source = 'python';
      return res.json(parsed);
    } catch (e) {
      return res.status(500).json({ ok: false, error: '匹配脚本输出非 JSON', detail: out.slice(0, 200) });
    }
  } catch (e) {
    console.error('[api/read/match]', e);
    return res.status(500).json({ ok: false, error: e.message || '匹配失败' });
  }
}

function handleReadMatchWrap(req, res) {
  handleReadMatch(req, res).catch((e) => {
    console.error('[api/read/match] async', e);
    if (!res.headersSent) {
      res.status(500).json({ ok: false, error: e.message || '匹配失败' });
    }
  });
}

app.get('/api/read/match/:dispatchId', handleReadMatchWrap);
app.get('/ztb/api/read/match/:dispatchId', handleReadMatchWrap);
app.get('/ztb-test/api/read/match/:dispatchId', handleReadMatchWrap);

async function handleItemReaders(req, res) {
  const recordId = parseInt((req.params && req.params.recordId) || '0', 10);
  const tok = String(req.query.digest_token || '').trim().toLowerCase();
  if (Number.isNaN(recordId) || recordId < 1) {
    return res.status(400).json({ ok: false, error: '无效 record_id' });
  }
  const p = getPool();
  if (!p) return res.status(500).json({ ok: false, error: poolError || '未配置数据库' });
  try {
    await ensureDispatchTables(p);
    const filterTok = tok && /^[a-f0-9]{32}$/.test(tok);
    const dExtra = filterTok ? ' AND LOWER(TRIM(IFNULL(d.digest_token,\'\'))) = ?' : '';
    const fExtra = filterTok ? ' AND LOWER(TRIM(IFNULL(f.digest_token,\'\'))) = ?' : '';
    const params = [recordId];
    if (filterTok) params.push(tok);
    params.push(recordId);
    if (filterTok) params.push(tok);
    const sql = `SELECT * FROM (
      SELECT l.dispatch_id, l.reader_userid, l.read_at, l.dwell_seconds, d.digest_token AS digest_token
      FROM wecom_card_item_read_log l
      INNER JOIN wecom_card_dispatch_log d ON d.dispatch_id = l.dispatch_id AND d.send_status = 'SENT'
      WHERE l.record_id = ?${dExtra}
      UNION ALL
      SELECT l.dispatch_id, l.reader_userid, l.read_at, l.dwell_seconds, f.digest_token AS digest_token
      FROM wecom_card_item_read_log l
      INNER JOIN forward_detail_ticket f ON f.ticket = l.dispatch_id AND f.record_id = l.record_id
      WHERE l.record_id = ?${fExtra}
    ) u ORDER BY u.read_at DESC LIMIT 200`;
    const [rows] = await p.execute(sql, params);
    /** 与卡片 @售前 同源：路由表中的 userid 在「谁已看」中优先排在最上 */
    const presaleUidSet = new Set();
    if (filterTok) {
      try {
        const [prRows] = await p.execute(
          'SELECT `presale_userid` FROM `digest_item_presale_route` WHERE LOWER(TRIM(`digest_token`)) = ? AND `record_id` = ?',
          [tok, recordId]
        );
        for (const pr of prRows || []) {
          const u = String(pr.presale_userid || '')
            .trim()
            .toLowerCase();
          if (u) presaleUidSet.add(u);
        }
      } catch (e) {
        console.warn('[api/read/item_readers] presale route', e.message || e);
      }
    }
    const rowList = (rows || []).slice();
    rowList.sort((a, b) => {
      const ua = String(a.reader_userid || '')
        .trim()
        .toLowerCase();
      const ub = String(b.reader_userid || '')
        .trim()
        .toLowerCase();
      const pa = presaleUidSet.has(ua) ? 0 : 1;
      const pb = presaleUidSet.has(ub) ? 0 : 1;
      if (pa !== pb) return pa - pb;
      const ta = a.read_at ? new Date(a.read_at).getTime() : 0;
      const tb = b.read_at ? new Date(b.read_at).getTime() : 0;
      return tb - ta;
    });
    const iniPath = readConfigIniPath();
    let dc = {};
    if (iniPath) {
      try {
        const cfg = require('ini').parse(fs.readFileSync(iniPath, 'utf-8'));
        dc = cfg.dispatch || cfg.Dispatch || {};
      } catch (e) {
        dc = {};
      }
    }
    const nameCol = String(dc.org_user_name_col || 'user_name').trim();
    const accountCol = String(dc.org_user_account_col || 'user_account').trim();
    const delCol = String(dc.org_user_deleted_col || 'is_deleted').trim();
    const tbl = String(dc.org_user_table || 'org_user').trim();
    const readers = [];
    for (const r of rowList) {
      let nm = '';
      const uid = String(r.reader_userid || '').trim();
      const uidKey = uid.toLowerCase();
      const onItemRoute = filterTok && presaleUidSet.has(uidKey);
      if (uid && tbl && accountCol && nameCol) {
        try {
          const [nr] = await p.execute(
            `SELECT \`${nameCol}\` AS nm FROM \`${tbl}\` WHERE \`${accountCol}\` = ? AND (\`${delCol}\` = 0 OR \`${delCol}\` IS NULL) LIMIT 1`,
            [uid]
          );
          if (nr && nr.length) nm = String(nr[0].nm || '').trim();
        } catch (e) {
          nm = '';
        }
      }
      readers.push({
        dispatch_id: r.dispatch_id,
        reader_userid: uid,
        reader_display: nm || uid,
        read_at: formatDateTimeAsiaShanghai(r.read_at),
        dwell_seconds: r.dwell_seconds != null ? Number(r.dwell_seconds) : null,
        on_item_route: onItemRoute,
      });
    }
    return res.json({ ok: true, readers });
  } catch (e) {
    console.error('[api/read/item_readers]', e);
    return res.status(500).json({ ok: false, error: e.message || '查询失败' });
  }
}

app.get('/api/read/item_readers/:recordId', handleItemReaders);

/** 东八区日历日 YYYY-MM-DD（用于摘要 digest_date 筛选） */
function todayYmdShanghai() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date());
}

function addCalendarDaysYmdFromYmd(ymd, deltaDays) {
  const parts = ymd.split('-').map(Number);
  const jd = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
  jd.setUTCDate(jd.getUTCDate() + deltaDays);
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(jd);
}

function parseDigestDateQuery(raw) {
  const q = String(raw || '').trim();
  if (!q || q.toLowerCase() === 'all') return { mode: 'all', ymd: null };
  const low = q.toLowerCase();
  const today = todayYmdShanghai();
  if (low === 'today' || low === '今天') return { mode: 'day', ymd: today };
  if (low === 'yesterday' || low === '昨天') return { mode: 'day', ymd: addCalendarDaysYmdFromYmd(today, -1) };
  if (low === 'before_yesterday' || low === '前天' || low === 'day_before_yesterday') {
    return { mode: 'day', ymd: addCalendarDaysYmdFromYmd(today, -2) };
  }
  const m = q.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (m) {
    return {
      mode: 'day',
      ymd: `${m[1]}-${String(m[2]).padStart(2, '0')}-${String(m[3]).padStart(2, '0')}`,
    };
  }
  return { mode: 'all', ymd: null };
}

/** 扫描 digest_packs 下各 token 目录 manifest.json 的 digest_date，匹配某日期的 token 列表 */
function listDigestTokensForDigestDate(ymd) {
  const base = getDigestPacksDir();
  if (!base || !fs.existsSync(base)) return [];
  const out = [];
  let ents;
  try {
    ents = fs.readdirSync(base, { withFileTypes: true });
  } catch (e) {
    return [];
  }
  for (const ent of ents) {
    if (!ent.isDirectory()) continue;
    const name = ent.name;
    if (!/^[a-f0-9]{32}$/i.test(name)) continue;
    const mf = path.join(base, name, 'manifest.json');
    if (!fs.existsSync(mf)) continue;
    try {
      const manifest = JSON.parse(fs.readFileSync(mf, 'utf8'));
      const ds = String(manifest.digest_date || '').trim();
      if (ds === ymd) out.push(name.toLowerCase());
    } catch (e) {
      /* 跳过损坏 manifest */
    }
  }
  return out;
}

/**
 * 全站详情阅读记录：谁、哪条、停留秒数（摘要分发 + 单条转发），按 read_at 倒序。
 * 与 dispatch-dashboard 相同：若配置了 [web_view] dashboard_secret，须 ?key= 或 X-Dashboard-Key。
 * 查询参数：digest_date=all|today|yesterday|before_yesterday|YYYY-MM-DD（按 manifest digest_date 过滤摘要包 token）
 * title：招标标题模糊匹配（scraping_infos.title）
 * read_status=all|seen|unseen（仅 sheet=presale 生效：售前是否已在摘要链路下打开过详情；others 页全部为已读日志）
 */
async function handleReadLogsList(req, res) {
  if (!assertDashboardKey(req)) {
    return res.status(403).json({ ok: false, error: '无权访问，请在 URL 附加 ?key= 或传 X-Dashboard-Key' });
  }
  let limit = parseInt(req.query && req.query.limit, 10);
  if (Number.isNaN(limit) || limit < 1) limit = 50;
  if (limit > 200) limit = 200;
  let offset = parseInt(req.query && req.query.offset, 10);
  if (Number.isNaN(offset) || offset < 0) offset = 0;
  /** 预编译绑定 LIMIT/OFFSET 在部分 MySQL 上会 ER_WRONG_ARGUMENTS(1210)，改为拼接已校验的整数 */
  const safeLimit = Math.min(200, Math.max(1, limit));
  const safeOffset = Math.max(0, offset);
  const readerFilter = String((req.query && req.query.reader) || '').trim();
  const titleFilter = String((req.query && req.query.title) || '').trim();
  const titleSql = titleFilter ? `AND IFNULL(s.title,'') LIKE ?` : '';
  const titleParams = titleFilter ? [`%${titleFilter}%`] : [];
  const digestDateRaw = String((req.query && req.query.digest_date) || '').trim();
  const dd = parseDigestDateQuery(digestDateRaw);
  let digestTokensFiltered = null;
  if (dd.mode === 'day' && dd.ymd) {
    digestTokensFiltered = listDigestTokensForDigestDate(dd.ymd);
  }
  const p = getPool();
  if (!p) return res.status(500).json({ ok: false, error: poolError || '未配置数据库' });
  const sheetRaw = String((req.query && req.query.sheet) || 'presale').trim().toLowerCase();
  const sheet = sheetRaw === 'other' ? 'other' : 'presale';
  const readStatusRaw = String((req.query && req.query.read_status) || 'all').trim().toLowerCase();
  const readStatus = readStatusRaw === 'seen' || readStatusRaw === 'unseen' ? readStatusRaw : 'all';
  /** 「其他人」页：仅读者不是本条 digest+record 下 @售前 的阅读记录 */
  const sheetWhereOther = `AND (
      TRIM(IFNULL(u.digest_token,'')) = ''
      OR NOT EXISTS (
        SELECT 1 FROM digest_item_presale_route pr
        WHERE LOWER(TRIM(pr.digest_token)) = LOWER(TRIM(IFNULL(u.digest_token,'')))
        AND pr.record_id = u.record_id
        AND pr.presale_userid = u.reader_userid
      )
    )`;
  const readerWhereUnion = readerFilter ? 'u.reader_userid LIKE ?' : '1=1';
  const readerParamsUnion = readerFilter ? [`%${readerFilter}%`] : [];
  const readerWherePr = readerFilter
    ? '(pr.presale_userid LIKE ? OR IFNULL(pr.presale_display_name,\'\') LIKE ?)'
    : '1=1';
  const readerParamsPr = readerFilter ? [`%${readerFilter}%`, `%${readerFilter}%`] : [];
  const unionInner = `
      SELECT l.record_id, l.reader_userid, l.read_at, l.dwell_seconds, l.dispatch_id,
             'dispatch' AS source_type, IFNULL(d.digest_token,'') AS digest_token
      FROM wecom_card_item_read_log l
      INNER JOIN wecom_card_dispatch_log d ON d.dispatch_id = l.dispatch_id AND d.send_status = 'SENT'
      UNION ALL
      SELECT l.record_id, l.reader_userid, l.read_at, l.dwell_seconds, l.dispatch_id,
             'forward' AS source_type, IFNULL(f.digest_token,'') AS digest_token
      FROM wecom_card_item_read_log l
      INNER JOIN forward_detail_ticket f ON f.ticket = l.dispatch_id AND f.record_id = l.record_id`;
  /** 摘要分发 + fr 的阅读聚合，用于 @售前名单与 digest_item_presale_route 对照是否已读 */
  const presaleAggSql = `
      SELECT
        LOWER(TRIM(IFNULL(x.dt,''))) AS dt,
        x.record_id,
        x.reader_userid,
        MAX(x.read_at) AS read_at,
        MAX(COALESCE(x.dwell_seconds, 0)) AS dwell_seconds
      FROM (
        SELECT TRIM(IFNULL(d.digest_token,'')) AS dt, l.record_id, l.reader_userid, l.read_at, l.dwell_seconds
        FROM wecom_card_item_read_log l
        INNER JOIN wecom_card_dispatch_log d ON d.dispatch_id = l.dispatch_id AND d.send_status = 'SENT'
        UNION ALL
        SELECT TRIM(IFNULL(f.digest_token,'')), l.record_id, l.reader_userid, l.read_at, l.dwell_seconds
        FROM wecom_card_item_read_log l
        INNER JOIN forward_detail_ticket f ON f.ticket = l.dispatch_id AND f.record_id = l.record_id
        WHERE TRIM(IFNULL(f.digest_token,'')) <> ''
      ) x
      GROUP BY LOWER(TRIM(IFNULL(x.dt,''))), x.record_id, x.reader_userid`;
  try {
    await ensureDispatchTables(p);

    const tokPh =
      digestTokensFiltered && digestTokensFiltered.length
        ? digestTokensFiltered.map(() => '?').join(',')
        : '';
    const digestTokenSqlPr =
      digestTokensFiltered && digestTokensFiltered.length
        ? `AND LOWER(TRIM(pr.digest_token)) IN (${tokPh})`
        : '';
    const digestTokenSqlUnion =
      digestTokensFiltered && digestTokensFiltered.length
        ? `AND LOWER(TRIM(IFNULL(u.digest_token,''))) IN (${tokPh})`
        : '';
    const tokParams =
      digestTokensFiltered && digestTokensFiltered.length
        ? digestTokensFiltered.map((t) => String(t).toLowerCase())
        : [];

    /** 按 manifest digest_date 筛单日时，可能对应多个摘要包目录（多个 token）；阅读应按「该日任一 token」汇总，避免同一招标+同人出现一行已看一行未看 */
    const useDayTokenMerge = !!(digestTokensFiltered && digestTokensFiltered.length);
    const useMultiTokenDedupe = !!(digestTokensFiltered && digestTokensFiltered.length > 1);
    let presaleAggFragment = presaleAggSql;
    let presaleAggJoin = `agg.dt = LOWER(TRIM(pr.digest_token))
          AND agg.record_id = pr.record_id
          AND agg.reader_userid = pr.presale_userid`;
    let presaleAggExtraParams = [];
    if (useDayTokenMerge) {
      const ph = tokParams.map(() => '?').join(',');
      presaleAggFragment = `
      SELECT
        x.record_id,
        x.reader_userid,
        MAX(x.read_at) AS read_at,
        MAX(COALESCE(x.dwell_seconds, 0)) AS dwell_seconds
      FROM (
        SELECT l.record_id, l.reader_userid, l.read_at, l.dwell_seconds
        FROM wecom_card_item_read_log l
        INNER JOIN wecom_card_dispatch_log d ON d.dispatch_id = l.dispatch_id AND d.send_status = 'SENT'
        WHERE LOWER(TRIM(IFNULL(d.digest_token,''))) IN (${ph})
        UNION ALL
        SELECT l.record_id, l.reader_userid, l.read_at, l.dwell_seconds
        FROM wecom_card_item_read_log l
        INNER JOIN forward_detail_ticket f ON f.ticket = l.dispatch_id AND f.record_id = l.record_id
        WHERE TRIM(IFNULL(f.digest_token,'')) <> ''
        AND LOWER(TRIM(IFNULL(f.digest_token,''))) IN (${ph})
      ) x
      GROUP BY x.record_id, x.reader_userid`;
      presaleAggJoin = `agg.record_id = pr.record_id
          AND agg.reader_userid = pr.presale_userid`;
      presaleAggExtraParams = [...tokParams, ...tokParams];
    }

    if (dd.mode === 'day' && dd.ymd && (!digestTokensFiltered || !digestTokensFiltered.length)) {
      return res.json({
        ok: true,
        rows: [],
        total: 0,
        limit: safeLimit,
        offset: safeOffset,
        sheet,
        digest_date: digestDateRaw || null,
        digest_ymd: dd.ymd,
        title: titleFilter || null,
        digest_token_count: 0,
        read_status: readStatus,
      });
    }

    const readStatusPr =
      sheet === 'presale' && readStatus === 'seen'
        ? 'AND agg.read_at IS NOT NULL'
        : sheet === 'presale' && readStatus === 'unseen'
          ? 'AND agg.read_at IS NULL'
          : '';
    /** 同日多摘要包 token 时按 record+售前分组，阅读筛选改用 HAVING */
    const readStatusHavingPresale =
      sheet === 'presale' && readStatus === 'seen'
        ? 'HAVING MAX(agg.read_at) IS NOT NULL'
        : sheet === 'presale' && readStatus === 'unseen'
          ? 'HAVING MAX(agg.read_at) IS NULL'
          : '';
    /** 「其他人」页数据源均为阅读日志，不存在「未看」；筛未看时直接空结果 */
    const readStatusOther = sheet === 'other' && readStatus === 'unseen' ? 'AND 1=0' : '';

    let rawRows;
    let total;
    if (sheet === 'presale') {
      let countSqlPr;
      let dataSqlPr;
      let countParamsPr;
      let dataParamsPr;
      if (useMultiTokenDedupe) {
        countSqlPr = `SELECT COUNT(*) AS c FROM (
        SELECT pr.record_id, pr.presale_userid
        FROM digest_item_presale_route pr
        LEFT JOIN scraping_infos s ON s.id = pr.record_id
        LEFT JOIN (${presaleAggFragment}) agg ON ${presaleAggJoin}
        WHERE ${readerWherePr} ${digestTokenSqlPr} ${titleSql}
        GROUP BY pr.record_id, pr.presale_userid
        ${readStatusHavingPresale}
      ) z`;
        countParamsPr = [...readerParamsPr, ...presaleAggExtraParams, ...tokParams, ...titleParams];
        dataSqlPr = `
        SELECT GROUP_CONCAT(DISTINCT LOWER(TRIM(pr.digest_token)) ORDER BY LOWER(TRIM(pr.digest_token)) SEPARATOR ',') AS digest_token,
               pr.record_id,
               pr.presale_userid AS reader_userid,
               MAX(pr.presale_display_name) AS presale_display_name,
               MAX(pr.canonical_customer) AS canonical_customer,
               MAX(s.title) AS record_title,
               MAX(agg.read_at) AS read_at,
               MAX(agg.dwell_seconds) AS dwell_seconds,
               NULL AS dispatch_id,
               'presale_roster' AS source_type
        FROM digest_item_presale_route pr
        LEFT JOIN scraping_infos s ON s.id = pr.record_id
        LEFT JOIN (${presaleAggFragment}) agg ON ${presaleAggJoin}
        WHERE ${readerWherePr} ${digestTokenSqlPr} ${titleSql}
        GROUP BY pr.record_id, pr.presale_userid
        ${readStatusHavingPresale}
        ORDER BY COALESCE(MAX(agg.read_at), MAX(pr.updated_at)) DESC, pr.record_id DESC, pr.presale_userid ASC
        LIMIT ${safeLimit} OFFSET ${safeOffset}`;
        dataParamsPr = [...readerParamsPr, ...presaleAggExtraParams, ...tokParams, ...titleParams];
      } else {
        countSqlPr = `SELECT COUNT(*) AS c FROM digest_item_presale_route pr
        LEFT JOIN scraping_infos s ON s.id = pr.record_id
        LEFT JOIN (${presaleAggFragment}) agg
          ON ${presaleAggJoin}
        WHERE ${readerWherePr} ${digestTokenSqlPr} ${titleSql} ${readStatusPr}`;
        countParamsPr = [...readerParamsPr, ...presaleAggExtraParams, ...tokParams, ...titleParams];
        dataSqlPr = `
        SELECT LOWER(TRIM(pr.digest_token)) AS digest_token,
               pr.record_id,
               pr.presale_userid AS reader_userid,
               pr.presale_display_name,
               pr.canonical_customer,
               s.title AS record_title,
               agg.read_at,
               agg.dwell_seconds,
               NULL AS dispatch_id,
               'presale_roster' AS source_type
        FROM digest_item_presale_route pr
        LEFT JOIN scraping_infos s ON s.id = pr.record_id
        LEFT JOIN (${presaleAggFragment}) agg
          ON ${presaleAggJoin}
        WHERE ${readerWherePr} ${digestTokenSqlPr} ${titleSql} ${readStatusPr}
        ORDER BY COALESCE(agg.read_at, pr.updated_at) DESC, pr.record_id DESC, pr.presale_userid ASC
        LIMIT ${safeLimit} OFFSET ${safeOffset}`;
        dataParamsPr = [...readerParamsPr, ...presaleAggExtraParams, ...tokParams, ...titleParams];
      }
      const [countRowsPr] = await p.execute(countSqlPr, countParamsPr);
      total = countRowsPr && countRowsPr[0] ? Number(countRowsPr[0].c) || 0 : 0;
      const [rowsPr] = await p.execute(dataSqlPr, dataParamsPr);
      rawRows = rowsPr || [];
    } else {
      const countParamsUnion = [...readerParamsUnion, ...tokParams, ...titleParams];
      const countSql = `SELECT COUNT(*) AS c FROM (${unionInner}) u
      LEFT JOIN scraping_infos s ON s.id = u.record_id
      WHERE ${readerWhereUnion} ${sheetWhereOther} ${digestTokenSqlUnion} ${titleSql} ${readStatusOther}`;
      const [countRows] = await p.execute(countSql, countParamsUnion);
      total = countRows && countRows[0] ? Number(countRows[0].c) || 0 : 0;
      const dataSql = `
      SELECT u.record_id, u.reader_userid, u.read_at, u.dwell_seconds, u.dispatch_id, u.source_type, u.digest_token,
             s.title AS record_title
      FROM (${unionInner}) u
      LEFT JOIN scraping_infos s ON s.id = u.record_id
      WHERE ${readerWhereUnion} ${sheetWhereOther} ${digestTokenSqlUnion} ${titleSql} ${readStatusOther}
      ORDER BY u.read_at DESC
      LIMIT ${safeLimit} OFFSET ${safeOffset}`;
      const dataParamsUnion = [...readerParamsUnion, ...tokParams, ...titleParams];
      const [rows] = await p.execute(dataSql, dataParamsUnion);
      rawRows = rows || [];
    }
    /** @售前：digest_item_presale_route 与「谁实际打开详情」对照（摘要 dispatch + 带 digest 的 fr） */
    const routeGroups = new Map();
    const readMap = new Map();
    function digestTokensFromRow(dtRaw) {
      const s = String(dtRaw || '').trim();
      if (!s) return [];
      return s
        .split(',')
        .map((t) => t.trim().toLowerCase())
        .filter((t) => /^[a-f0-9]{32}$/.test(t));
    }
    const tokens = [...new Set(rawRows.flatMap((x) => digestTokensFromRow(x.digest_token)))];
    const rids = [...new Set(rawRows.map((x) => Number(x.record_id)).filter((n) => !Number.isNaN(n) && n > 0))];
    const pairSet = new Set();
    for (const x of rawRows) {
      const rid = Number(x.record_id);
      for (const dt of digestTokensFromRow(x.digest_token)) {
        pairSet.add(`${dt}|${rid}`);
      }
    }

    function mergePresaleRead(prev, cur) {
      if (!prev) return cur;
      if (!cur) return prev;
      const da = prev.dwell_seconds != null ? Number(prev.dwell_seconds) : 0;
      const db = cur.dwell_seconds != null ? Number(cur.dwell_seconds) : 0;
      const pick = da >= db ? prev : cur;
      return {
        read_at: pick.read_at,
        dwell_seconds: pick.dwell_seconds != null ? Number(pick.dwell_seconds) : null,
      };
    }

    if (tokens.length && rids.length) {
      const tokPh = tokens.map(() => '?').join(',');
      const ridPh = rids.map(() => '?').join(',');
      try {
        const [routeRows] = await p.execute(
          `SELECT LOWER(TRIM(digest_token)) AS digest_token, record_id, presale_userid, presale_display_name, canonical_customer
           FROM digest_item_presale_route
           WHERE LOWER(TRIM(digest_token)) IN (${tokPh})
           AND record_id IN (${ridPh})`,
          [...tokens, ...rids]
        );
        for (const rt of routeRows || []) {
          const pk = `${String(rt.digest_token || '').trim().toLowerCase()}|${Number(rt.record_id)}`;
          if (!pairSet.has(pk)) continue;
          if (!routeGroups.has(pk)) routeGroups.set(pk, []);
          routeGroups.get(pk).push(rt);
        }
        const [dispReads] = await p.execute(
          `SELECT LOWER(TRIM(IFNULL(d.digest_token,''))) AS dt, l.record_id, l.reader_userid, l.read_at, l.dwell_seconds
           FROM wecom_card_item_read_log l
           INNER JOIN wecom_card_dispatch_log d ON d.dispatch_id = l.dispatch_id AND d.send_status = 'SENT'
           WHERE LOWER(TRIM(IFNULL(d.digest_token,''))) IN (${tokPh})
           AND l.record_id IN (${ridPh})`,
          [...tokens, ...rids]
        );
        for (const x of dispReads || []) {
          const dt = String(x.dt || '').trim().toLowerCase();
          const uid = String(x.reader_userid || '').trim();
          const rk = `${dt}|${Number(x.record_id)}|${uid}`;
          readMap.set(rk, mergePresaleRead(readMap.get(rk), { read_at: x.read_at, dwell_seconds: x.dwell_seconds }));
        }
        const [fwdReads] = await p.execute(
          `SELECT LOWER(TRIM(IFNULL(f.digest_token,''))) AS dt, l.record_id, l.reader_userid, l.read_at, l.dwell_seconds
           FROM wecom_card_item_read_log l
           INNER JOIN forward_detail_ticket f ON f.ticket = l.dispatch_id AND f.record_id = l.record_id
           WHERE TRIM(IFNULL(f.digest_token,'')) <> ''
           AND LOWER(TRIM(IFNULL(f.digest_token,''))) IN (${tokPh})
           AND l.record_id IN (${ridPh})`,
          [...tokens, ...rids]
        );
        for (const x of fwdReads || []) {
          const dt = String(x.dt || '').trim().toLowerCase();
          const uid = String(x.reader_userid || '').trim();
          const rk = `${dt}|${Number(x.record_id)}|${uid}`;
          readMap.set(rk, mergePresaleRead(readMap.get(rk), { read_at: x.read_at, dwell_seconds: x.dwell_seconds }));
        }
      } catch (e) {
        console.warn('[api/read/logs] presale enrich', e.message || e);
      }
      const formatted = new Map();
      for (const [rk, v] of readMap) {
        formatted.set(rk, {
          read_at: formatDateTimeAsiaShanghai(v.read_at),
          dwell_seconds: v.dwell_seconds != null ? Number(v.dwell_seconds) : null,
        });
      }
      readMap.clear();
      for (const [k, v] of formatted) readMap.set(k, v);
    }

    const iniPath = readConfigIniPath();
    let dc = {};
    if (iniPath) {
      try {
        const cfg = require('ini').parse(fs.readFileSync(iniPath, 'utf-8'));
        dc = cfg.dispatch || cfg.Dispatch || {};
      } catch (e) {
        dc = {};
      }
    }
    const nameCol = String(dc.org_user_name_col || 'user_name').trim();
    const accountCol = String(dc.org_user_account_col || 'user_account').trim();
    const delCol = String(dc.org_user_deleted_col || 'is_deleted').trim();
    const tbl = String(dc.org_user_table || 'org_user').trim();
    const uids = [...new Set(rawRows.map((r) => String(r.reader_userid || '').trim()).filter(Boolean))];
    const nameByUid = new Map();
    if (tbl && accountCol && nameCol && uids.length) {
      const ph = uids.map(() => '?').join(',');
      try {
        const [nrows] = await p.execute(
          `SELECT \`${accountCol}\` AS ac, \`${nameCol}\` AS nm FROM \`${tbl}\` WHERE \`${accountCol}\` IN (${ph}) AND (\`${delCol}\` = 0 OR \`${delCol}\` IS NULL)`,
          uids
        );
        for (const nr of nrows || []) {
          const ac = String(nr.ac || '').trim();
          if (ac) nameByUid.set(ac, String(nr.nm || '').trim());
        }
      } catch (e) {
        /* 表/列不可用时仅显示 userid */
      }
    }
    const out = rawRows.map((r) => {
      const uid = String(r.reader_userid || '').trim();
      const roster = r.source_type === 'presale_roster';
      const nm =
        nameByUid.get(uid) || String(r.presale_display_name || '').trim() || uid;
      const dt = String(r.digest_token || '').trim().toLowerCase();
      const rid = Number(r.record_id);
      const pk = `${dt}|${rid}`;
      const routes = routeGroups.get(pk) || [];
      const presale_watchers = routes.map((rt) => {
        const puid = String(rt.presale_userid || '').trim();
        const rk = `${dt}|${rid}|${puid}`;
        const info = readMap.get(rk);
        return {
          presale_userid: puid,
          presale_display_name: String(rt.presale_display_name || '').trim(),
          canonical_customer: String(rt.canonical_customer || '').trim(),
          seen: !!info,
          read_at: info ? info.read_at : null,
          dwell_seconds: info ? info.dwell_seconds : null,
        };
      });
      return {
        record_id: rid,
        reader_userid: uid,
        reader_display: nm,
        read_at: formatDateTimeAsiaShanghai(r.read_at),
        dwell_seconds: r.dwell_seconds != null ? Number(r.dwell_seconds) : null,
        source_type: roster ? 'presale_roster' : r.source_type === 'forward' ? 'forward' : 'dispatch',
        source_label: roster ? '@售前（全员）' : r.source_type === 'forward' ? '单条转发' : '摘要分发',
        digest_token: String(r.digest_token || '').trim() || null,
        record_title: String(r.record_title || '').trim() || null,
        dispatch_id: r.dispatch_id != null && String(r.dispatch_id).length ? String(r.dispatch_id) : '',
        presale_watchers,
      };
    });
    return res.json({
      ok: true,
      rows: out,
      total,
      limit: safeLimit,
      offset: safeOffset,
      sheet,
      digest_date: digestDateRaw || null,
      digest_ymd: dd.mode === 'day' ? dd.ymd : null,
      title: titleFilter || null,
      digest_token_count: digestTokensFiltered ? digestTokensFiltered.length : null,
      read_status: readStatus,
    });
  } catch (e) {
    console.error('[api/read/logs]', e);
    return res.status(500).json({ ok: false, error: e.message || '查询失败' });
  }
}

/**
 * 详情阅读记录统计看板：与销售「@的人观看」同筛选口径（摘要日 / 人员 / 标题），
 * 汇总应看条数、已看、未看及按销售分组；不受 URL 上 read_status 影响（看板始终看全量漏斗）。
 */
async function handleReadLogsStats(req, res) {
  if (!assertDashboardKey(req)) {
    return res.status(403).json({ ok: false, error: '无权访问，请在 URL 附加 ?key= 或传 X-Dashboard-Key' });
  }
  const readerFilter = String((req.query && req.query.reader) || '').trim();
  const titleFilter = String((req.query && req.query.title) || '').trim();
  const titleSql = titleFilter ? `AND IFNULL(s.title,'') LIKE ?` : '';
  const titleParams = titleFilter ? [`%${titleFilter}%`] : [];
  const digestDateRaw = String((req.query && req.query.digest_date) || '').trim();
  const dd = parseDigestDateQuery(digestDateRaw);
  let digestTokensFiltered = null;
  if (dd.mode === 'day' && dd.ymd) {
    digestTokensFiltered = listDigestTokensForDigestDate(dd.ymd);
  }
  const p = getPool();
  if (!p) return res.status(500).json({ ok: false, error: poolError || '未配置数据库' });
  const sheetRaw = String((req.query && req.query.sheet) || 'presale').trim().toLowerCase();
  const sheet = sheetRaw === 'other' ? 'other' : 'presale';
  const sheetWhereOther = `AND (
      TRIM(IFNULL(u.digest_token,'')) = ''
      OR NOT EXISTS (
        SELECT 1 FROM digest_item_presale_route pr
        WHERE LOWER(TRIM(pr.digest_token)) = LOWER(TRIM(IFNULL(u.digest_token,'')))
        AND pr.record_id = u.record_id
        AND pr.presale_userid = u.reader_userid
      )
    )`;
  const readerWhereUnion = readerFilter ? 'u.reader_userid LIKE ?' : '1=1';
  const readerParamsUnion = readerFilter ? [`%${readerFilter}%`] : [];
  const readerWherePr = readerFilter
    ? '(pr.presale_userid LIKE ? OR IFNULL(pr.presale_display_name,\'\') LIKE ?)'
    : '1=1';
  const readerParamsPr = readerFilter ? [`%${readerFilter}%`, `%${readerFilter}%`] : [];
  const unionInner = `
      SELECT l.record_id, l.reader_userid, l.read_at, l.dwell_seconds, l.dispatch_id,
             'dispatch' AS source_type, IFNULL(d.digest_token,'') AS digest_token
      FROM wecom_card_item_read_log l
      INNER JOIN wecom_card_dispatch_log d ON d.dispatch_id = l.dispatch_id AND d.send_status = 'SENT'
      UNION ALL
      SELECT l.record_id, l.reader_userid, l.read_at, l.dwell_seconds, l.dispatch_id,
             'forward' AS source_type, IFNULL(f.digest_token,'') AS digest_token
      FROM wecom_card_item_read_log l
      INNER JOIN forward_detail_ticket f ON f.ticket = l.dispatch_id AND f.record_id = l.record_id`;
  const presaleAggSql = `
      SELECT
        LOWER(TRIM(IFNULL(x.dt,''))) AS dt,
        x.record_id,
        x.reader_userid,
        MAX(x.read_at) AS read_at,
        MAX(COALESCE(x.dwell_seconds, 0)) AS dwell_seconds
      FROM (
        SELECT TRIM(IFNULL(d.digest_token,'')) AS dt, l.record_id, l.reader_userid, l.read_at, l.dwell_seconds
        FROM wecom_card_item_read_log l
        INNER JOIN wecom_card_dispatch_log d ON d.dispatch_id = l.dispatch_id AND d.send_status = 'SENT'
        UNION ALL
        SELECT TRIM(IFNULL(f.digest_token,'')), l.record_id, l.reader_userid, l.read_at, l.dwell_seconds
        FROM wecom_card_item_read_log l
        INNER JOIN forward_detail_ticket f ON f.ticket = l.dispatch_id AND f.record_id = l.record_id
        WHERE TRIM(IFNULL(f.digest_token,'')) <> ''
      ) x
      GROUP BY LOWER(TRIM(IFNULL(x.dt,''))), x.record_id, x.reader_userid`;
  try {
    await ensureDispatchTables(p);
    const tokPh =
      digestTokensFiltered && digestTokensFiltered.length
        ? digestTokensFiltered.map(() => '?').join(',')
        : '';
    const digestTokenSqlPr =
      digestTokensFiltered && digestTokensFiltered.length
        ? `AND LOWER(TRIM(pr.digest_token)) IN (${tokPh})`
        : '';
    const digestTokenSqlUnion =
      digestTokensFiltered && digestTokensFiltered.length
        ? `AND LOWER(TRIM(IFNULL(u.digest_token,''))) IN (${tokPh})`
        : '';
    const tokParams =
      digestTokensFiltered && digestTokensFiltered.length
        ? digestTokensFiltered.map((t) => String(t).toLowerCase())
        : [];
    const useDayTokenMerge = !!(digestTokensFiltered && digestTokensFiltered.length);
    let presaleAggFragment = presaleAggSql;
    let presaleAggJoin = `agg.dt = LOWER(TRIM(pr.digest_token))
          AND agg.record_id = pr.record_id
          AND agg.reader_userid = pr.presale_userid`;
    let presaleAggExtraParams = [];
    if (useDayTokenMerge) {
      const ph = tokParams.map(() => '?').join(',');
      presaleAggFragment = `
      SELECT
        x.record_id,
        x.reader_userid,
        MAX(x.read_at) AS read_at,
        MAX(COALESCE(x.dwell_seconds, 0)) AS dwell_seconds
      FROM (
        SELECT l.record_id, l.reader_userid, l.read_at, l.dwell_seconds
        FROM wecom_card_item_read_log l
        INNER JOIN wecom_card_dispatch_log d ON d.dispatch_id = l.dispatch_id AND d.send_status = 'SENT'
        WHERE LOWER(TRIM(IFNULL(d.digest_token,''))) IN (${ph})
        UNION ALL
        SELECT l.record_id, l.reader_userid, l.read_at, l.dwell_seconds
        FROM wecom_card_item_read_log l
        INNER JOIN forward_detail_ticket f ON f.ticket = l.dispatch_id AND f.record_id = l.record_id
        WHERE TRIM(IFNULL(f.digest_token,'')) <> ''
        AND LOWER(TRIM(IFNULL(f.digest_token,''))) IN (${ph})
      ) x
      GROUP BY x.record_id, x.reader_userid`;
      presaleAggJoin = `agg.record_id = pr.record_id
          AND agg.reader_userid = pr.presale_userid`;
      presaleAggExtraParams = [...tokParams, ...tokParams];
    }

    if (dd.mode === 'day' && dd.ymd && (!digestTokensFiltered || !digestTokensFiltered.length)) {
      return res.json({
        ok: true,
        sheet,
        digest_date: digestDateRaw || null,
        digest_ymd: dd.ymd,
        digest_token_count: 0,
        presale: { total: 0, seen: 0, unseen: 0, seen_pct: 0, by_reader: [] },
        other: null,
      });
    }

    if (sheet === 'presale') {
      const rosterSub = `
        SELECT pr.record_id, pr.presale_userid AS reader_userid,
               MAX(pr.presale_display_name) AS reader_display,
               MAX(agg.read_at) AS read_at
        FROM digest_item_presale_route pr
        LEFT JOIN scraping_infos s ON s.id = pr.record_id
        LEFT JOIN (${presaleAggFragment}) agg ON ${presaleAggJoin}
        WHERE ${readerWherePr} ${digestTokenSqlPr} ${titleSql}
        GROUP BY pr.record_id, pr.presale_userid`;
      const rosterParams = [...readerParamsPr, ...presaleAggExtraParams, ...tokParams, ...titleParams];
      const [totRows] = await p.execute(
        `SELECT COUNT(*) AS total,
                SUM(CASE WHEN t.read_at IS NOT NULL THEN 1 ELSE 0 END) AS seen,
                SUM(CASE WHEN t.read_at IS NULL THEN 1 ELSE 0 END) AS unseen
         FROM (${rosterSub}) t`,
        rosterParams
      );
      const tr = totRows && totRows[0] ? totRows[0] : {};
      const total = Number(tr.total) || 0;
      const seen = Number(tr.seen) || 0;
      const unseen = Number(tr.unseen) || 0;
      const seenPct = total > 0 ? Math.round((seen * 1000) / total) / 10 : 0;
      const [brRows] = await p.execute(
        `SELECT t.reader_userid,
                MAX(t.reader_display) AS reader_display,
                COUNT(*) AS total,
                SUM(CASE WHEN t.read_at IS NOT NULL THEN 1 ELSE 0 END) AS seen,
                SUM(CASE WHEN t.read_at IS NULL THEN 1 ELSE 0 END) AS unseen
         FROM (${rosterSub}) t
         GROUP BY t.reader_userid
         ORDER BY unseen DESC, total DESC, t.reader_userid ASC
         LIMIT 80`,
        rosterParams
      );
      const uids = (brRows || []).map((r) => String(r.reader_userid || '').trim()).filter(Boolean);
      const nameByUid = new Map();
      const iniPath = readConfigIniPath();
      if (iniPath && uids.length) {
        try {
          const cfg = require('ini').parse(fs.readFileSync(iniPath, 'utf-8'));
          const dc = cfg.dispatch || cfg.Dispatch || {};
          const tbl = String(dc.org_user_table || 'org_user').trim();
          const nameCol = String(dc.org_user_name_col || 'user_name').trim();
          const accountCol = String(dc.org_user_account_col || 'user_account').trim();
          const delCol = String(dc.org_user_deleted_col || 'is_deleted').trim();
          if (tbl && accountCol && nameCol) {
            const ph = uids.map(() => '?').join(',');
            const [nrows] = await p.execute(
              `SELECT \`${accountCol}\` AS ac, \`${nameCol}\` AS nm FROM \`${tbl}\` WHERE \`${accountCol}\` IN (${ph}) AND (\`${delCol}\` = 0 OR \`${delCol}\` IS NULL)`,
              uids
            );
            for (const nr of nrows || []) {
              const ac = String(nr.ac || '').trim();
              if (ac) nameByUid.set(ac, String(nr.nm || '').trim());
            }
          }
        } catch (e) {
          /* ignore */
        }
      }
      const by_reader_base = (brRows || []).map((r) => {
        const uid = String(r.reader_userid || '').trim();
        const nm = nameByUid.get(uid) || String(r.reader_display || '').trim() || uid;
        const t = Number(r.total) || 0;
        const s = Number(r.seen) || 0;
        const u = Number(r.unseen) || 0;
        return {
          reader_userid: uid,
          reader_display: nm,
          total: t,
          seen: s,
          unseen: u,
          seen_pct: t > 0 ? Math.round((s * 1000) / t) / 10 : 0,
        };
      });
      let tip_seen_lines = [];
      let tip_unseen_lines = [];
      let tip_roster_lines = [];
      let tip_seen_sales = [];
      let tip_unseen_sales = [];
      const tipMap = new Map();
      try {
        const [detailRows] = await p.execute(
          `SELECT t.reader_userid, t.record_id, IFNULL(s2.title,'') AS record_title,
                  (t.read_at IS NOT NULL) AS seen_flag
           FROM (${rosterSub}) t
           LEFT JOIN scraping_infos s2 ON s2.id = t.record_id
           ORDER BY t.reader_userid ASC, (t.read_at IS NOT NULL) DESC, t.record_id DESC
           LIMIT 4000`,
          rosterParams
        );
        const seenSalesSet = new Set();
        const unseenSalesSet = new Set();
        for (const row of detailRows || []) {
          const uid = String(row.reader_userid || '').trim();
          if (!uid) continue;
          const disp = nameByUid.get(uid) || uid;
          const title = String(row.record_title || '').trim() || `#${Number(row.record_id)}`;
          const sf = row.seen_flag === true || row.seen_flag === 1 || Number(row.seen_flag) === 1;
          if (tip_roster_lines.length < 55) tip_roster_lines.push(`${disp} · ${title}`);
          if (sf) {
            seenSalesSet.add(disp);
            if (tip_seen_lines.length < 55) tip_seen_lines.push(`${disp} · ${title}`);
          } else {
            unseenSalesSet.add(disp);
            if (tip_unseen_lines.length < 55) tip_unseen_lines.push(`${disp} · ${title}`);
          }
          if (!tipMap.has(uid)) tipMap.set(uid, { seen_titles: [], unseen_titles: [] });
          const ent = tipMap.get(uid);
          if (sf) {
            if (ent.seen_titles.length < 40) ent.seen_titles.push(title);
          } else if (ent.unseen_titles.length < 40) {
            ent.unseen_titles.push(title);
          }
        }
        tip_seen_sales = [...seenSalesSet].sort((a, b) => a.localeCompare(b, 'zh-CN'));
        tip_unseen_sales = [...unseenSalesSet].sort((a, b) => a.localeCompare(b, 'zh-CN'));
        if (tip_roster_lines.length > 0 && total > tip_roster_lines.length) {
          const shown = tip_roster_lines.length;
          tip_roster_lines.push(`…（应看共 ${total} 条，仅列 ${shown} 条）`);
        }
      } catch (e) {
        console.warn('[api/read/logs/stats] detail tips', e.message || e);
      }
      const by_reader = by_reader_base.map((br) => {
        const t = tipMap.get(br.reader_userid) || { seen_titles: [], unseen_titles: [] };
        return {
          ...br,
          seen_titles: t.seen_titles,
          unseen_titles: t.unseen_titles,
        };
      });
      return res.json({
        ok: true,
        sheet,
        digest_date: digestDateRaw || null,
        digest_ymd: dd.mode === 'day' ? dd.ymd : null,
        digest_token_count: digestTokensFiltered ? digestTokensFiltered.length : null,
        title: titleFilter || null,
        presale: {
          total,
          seen,
          unseen,
          seen_pct: seenPct,
          by_reader,
          tip_seen_lines,
          tip_unseen_lines,
          tip_roster_lines,
          tip_seen_sales,
          tip_unseen_sales,
        },
        other: null,
      });
    }

    const countParamsUnion = [...readerParamsUnion, ...tokParams, ...titleParams];
    const otherSql = `
      SELECT COUNT(*) AS total_reads,
             COUNT(DISTINCT u.reader_userid) AS unique_readers
      FROM (${unionInner}) u
      LEFT JOIN scraping_infos s ON s.id = u.record_id
      WHERE ${readerWhereUnion} ${sheetWhereOther} ${digestTokenSqlUnion} ${titleSql}`;
    const [oTot] = await p.execute(otherSql, countParamsUnion);
    const ot = oTot && oTot[0] ? oTot[0] : {};
    const [byR] = await p.execute(
      `SELECT u.reader_userid, COUNT(*) AS cnt
       FROM (${unionInner}) u
       LEFT JOIN scraping_infos s ON s.id = u.record_id
       WHERE ${readerWhereUnion} ${sheetWhereOther} ${digestTokenSqlUnion} ${titleSql}
       GROUP BY u.reader_userid
       ORDER BY cnt DESC
       LIMIT 40`,
      countParamsUnion
    );
    const top_readers = (byR || []).map((r) => ({
      reader_userid: String(r.reader_userid || '').trim(),
      read_count: Number(r.cnt) || 0,
    }));
    return res.json({
      ok: true,
      sheet,
      digest_date: digestDateRaw || null,
      digest_ymd: dd.mode === 'day' ? dd.ymd : null,
      digest_token_count: digestTokensFiltered ? digestTokensFiltered.length : null,
      title: titleFilter || null,
      presale: null,
      other: {
        total_reads: Number(ot.total_reads) || 0,
        unique_readers: Number(ot.unique_readers) || 0,
        top_readers,
      },
    });
  } catch (e) {
    console.error('[api/read/logs/stats]', e);
    return res.status(500).json({ ok: false, error: e.message || '统计失败' });
  }
}

app.get('/api/read/logs/stats', handleReadLogsStats);
app.get('/api/read/logs', handleReadLogsList);

app.post('/api/forward/item', (req, res) => {
  const recordId = parseInt(req.body && req.body.record_id, 10);
  const raw = (req.body && req.body.touser_ids) || (req.body && req.body.touser_list) || [];
  const users = Array.isArray(raw) ? raw.map((u) => String(u || '').trim()).filter(Boolean) : [];
  const digestTok = String((req.body && req.body.digest_token) || '').trim().toLowerCase();
  const envPrefix = String((req.body && req.body.env_prefix) || '').trim();
  if (Number.isNaN(recordId) || recordId < 1) {
    return res.status(400).json({ ok: false, error: '无效 record_id' });
  }
  if (!users.length) {
    return res.status(400).json({ ok: false, error: '请填写接收人（userid 或中文名）' });
  }
  if (!fs.existsSync(FORWARD_ITEM_SCRIPT)) {
    return res.status(500).json({ ok: false, error: '未找到 forward_item_wecom.py' });
  }
  const linkPrefix = resolveForwardLinkPrefix(req, envPrefix);
  if (!linkPrefix) {
    return res.status(400).json({ ok: false, error: '无法解析站点域名，请检查 Host 头' });
  }
  const py = process.env.PYTHON || 'python';
  const input = JSON.stringify({
    record_id: recordId,
    touser_list: users,
    link_prefix: linkPrefix,
    digest_token: digestTok || undefined,
  });
  const r = spawnSync(py, [FORWARD_ITEM_SCRIPT], {
    input,
    encoding: 'utf-8',
    maxBuffer: 2 * 1024 * 1024,
    cwd: PROJECT_ROOT,
    timeout: 120000,
    windowsHide: true,
    env: pythonSpawnEnv(),
  });
  if (r.error) {
    return res.status(500).json({ ok: false, error: r.error.message || String(r.error) });
  }
  const errStd = (r.stderr || '').trim();
  if (errStd) console.error('[forward/item stderr]', errStd.slice(0, 500));
  const out = (r.stdout || '').trim();
  try {
    const line = out.split(/\r?\n/).filter(Boolean).pop() || '{}';
    const parsed = JSON.parse(line);
    if (!parsed.ok) {
      return res.status(400).json({
        ok: false,
        error: parsed.error || '转发失败',
        unresolved: parsed.unresolved,
        warning: parsed.warning,
        failed: parsed.failed,
        failed_detail: parsed.failed_detail,
      });
    }
    return res.json({
      ok: true,
      sent: parsed.sent || [],
      failed: parsed.failed || [],
      warning: parsed.warning,
    });
  } catch (e) {
    return res.status(500).json({ ok: false, error: '转发脚本输出非 JSON', detail: (out || '').slice(0, 200) });
  }
});

async function handleDispatchReadReport(req, res) {
  if (!assertDashboardKey(req)) {
    return res.status(403).json({ ok: false, error: '无权访问，请在配置 [web_view] dashboard_secret 或传 ?key=' });
  }
  const p = getPool();
  if (!p) return res.status(500).json({ ok: false, error: poolError || '未配置数据库' });
  const tok = String(req.query.digest_token || '').trim().toLowerCase();
  let limit = parseInt(req.query.limit || '80', 10);
  if (Number.isNaN(limit) || limit < 1) limit = 80;
  if (limit > 200) limit = 200;
  try {
    await ensureDispatchTables(p);
    let sql = `SELECT dispatch_id, digest_token, receiver_userid, receiver_customer, item_count,
        first_read_at, last_read_at, read_count, send_status, created_at
        FROM wecom_card_dispatch_log WHERE send_status = 'SENT'`;
    const params = [];
    if (tok && /^[a-f0-9]{32}$/.test(tok)) {
      sql += ' AND digest_token = ?';
      params.push(tok);
    }
    sql += ' ORDER BY id DESC LIMIT ' + limit;
    const [dispatches] = await p.execute(sql, params);
    const rows = [];
    for (const d of dispatches || []) {
      const did = d.dispatch_id;
      const [reads] = await p.execute(
        `SELECT record_id, read_at, dwell_seconds, reader_userid FROM wecom_card_item_read_log WHERE dispatch_id = ?`,
        [did]
      );
      rows.push({
        dispatch_id: did,
        digest_token: d.digest_token,
        receiver_userid: d.receiver_userid,
        receiver_customer: d.receiver_customer,
        item_count: d.item_count,
        overview_first_read_at: formatDateTimeAsiaShanghai(d.first_read_at),
        overview_last_read_at: formatDateTimeAsiaShanghai(d.last_read_at),
        overview_open_count: d.read_count,
        created_at: formatDateTimeAsiaShanghai(d.created_at),
        detail_reads: (reads || []).map((r) => ({
          record_id: Number(r.record_id),
          read_at: formatDateTimeAsiaShanghai(r.read_at),
          dwell_seconds: r.dwell_seconds != null ? Number(r.dwell_seconds) : null,
          reader_userid: r.reader_userid,
        })),
      });
    }
    return res.json({ ok: true, rows });
  } catch (e) {
    console.error('[api/dispatch/read-report]', e);
    return res.status(500).json({ ok: false, error: e.message || '查询失败' });
  }
}

app.get('/api/dispatch/read-report', handleDispatchReadReport);
app.get('/ztb/api/dispatch/read-report', handleDispatchReadReport);
app.get('/ztb-test/api/dispatch/read-report', handleDispatchReadReport);

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
  const fp = path.join(getDigestPacksDir(), token, 'manifest.json');
  if (!fs.existsSync(fp)) {
    return res.status(404).json({ ok: false, error: '摘要包不存在（请确认已同步 digest_packs 或 config [web_view] digest_pack_dir）' });
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

async function handleDigestPackPage(req, res) {
  const token = String(req.params.token || '').trim().toLowerCase();
  if (!/^[a-f0-9]{32}$/.test(token)) {
    return res.type('html').status(400).send('无效的链接');
  }
  const fp = path.join(getDigestPacksDir(), token, 'manifest.json');
  if (!fs.existsSync(fp)) {
    const pen = readDigestPendingMeta();
    const pendingOk = !!(pen && String(pen.token || '').trim().toLowerCase() === token);
    let allowDegraded = pendingOk;
    if (!allowDegraded) {
      const p = getPool();
      if (p) {
        try {
          allowDegraded = await digestTokenHasDispatchRecord(p, token);
        } catch (e) {
          console.error('[digest_pack page] dispatch 校验', e);
        }
      }
    }
    if (!allowDegraded) {
      const hint =
        '<p style="margin:12px 0 0;font-size:13px;color:#64748b">若刚跑批过，请把跑批机上的 <code>digest_packs</code> 目录同步到运行 Node 的机器，或在 <code>config.ini</code> 的 <code>[web_view]</code> 中设置 <code>digest_pack_dir</code> 指向摘要包目录；并重启 Node。</p>';
      return res
        .type('html')
        .status(404)
        .send(
          `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>摘要不可用</title></head><body style="font-family:system-ui,sans-serif;padding:20px;max-width:520px;margin:0 auto;color:#1e293b"><h1 style="font-size:18px">链接已失效或尚未生成</h1><p style="line-height:1.6">请从<strong>最新</strong>企微摘要卡片进入；旧卡片中的链接可能已过期。</p>${hint}</body></html>`
        );
    }
    console.warn(
      '[digest_pack page] manifest 缺失，降级打开 token=%s digest_dir=%s pending=%s',
      token,
      getDigestPacksDir(),
      pendingOk
    );
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
    env: pythonSpawnEnv(),
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

function sendDispatchDashboardPage(req, res) {
  if (!assertDashboardKey(req)) {
    return res.status(403).type('html').send('<p style="padding:24px;font-family:sans-serif">无权访问。请在 config.ini [web_view] 配置 dashboard_secret，并于 URL 附加 ?key=密钥</p>');
  }
  const htmlPath = path.join(VIEWS_DIR, 'dispatch_dashboard.html');
  if (!fs.existsSync(htmlPath)) {
    return res.status(500).send('views/dispatch_dashboard.html not found');
  }
  res.set({
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    Pragma: 'no-cache',
  });
  res.sendFile(htmlPath);
}

/* 与 Nginx 招采入口 /ztb/、测试入口 /ztb-test/ 一致，避免只配前缀反代时 Not found */
app.get('/dispatch-dashboard', sendDispatchDashboardPage);
app.get('/ztb/dispatch-dashboard', sendDispatchDashboardPage);
app.get('/ztb-test/dispatch-dashboard', sendDispatchDashboardPage);

app.get('/read_logs', sendReadLogsPage);

app.listen(PORT, BIND_HOST, () => {
  const cfg = loadDbConfig();
  const dbOk = cfg && cfg.database ? `数据库 ${cfg.database}@${cfg.host}` : '未读取到数据库配置';
  const hostHint = BIND_HOST === '0.0.0.0' ? '127.0.0.1' : BIND_HOST;
  console.log(`招采信息披露 Node 版: http://${hostHint}:${PORT} | 监听 ${BIND_HOST}:${PORT} | ${dbOk}`);
});
