const fs = require('fs');
const path = require('path');
const ini = require('ini');

function trim(s) {
  return typeof s === 'string' ? s.trim() : (s || '');
}

/**
 * 从项目根目录 config.ini 读取 [database] 配置，与 Python 版共用同一配置。
 * 依次尝试：web-view-node 上级目录、当前工作目录、当前工作目录上级。
 */
function loadDbConfig() {
  const candidates = [
    path.join(__dirname, '..', '..', 'config.ini'),
    path.join(process.cwd(), 'config.ini'),
    path.join(process.cwd(), '..', 'config.ini'),
  ];
  let raw = null;
  let configPath = null;
  for (const p of candidates) {
    if (fs.existsSync(p)) {
      configPath = p;
      break;
    }
  }
  if (!configPath) return null;
  try {
    raw = fs.readFileSync(configPath, 'utf-8');
  } catch (e) {
    return null;
  }
  const cfg = ini.parse(raw);
  const d = cfg.database || cfg.Database;
  if (!d) return null;
  return {
    host: trim(d.host) || '127.0.0.1',
    port: parseInt(trim(d.port), 10) || 3306,
    database: trim(d.database) || '',
    user: trim(d.user) || 'root',
    password: trim(d.password) || '',
    charset: trim(d.charset) || 'utf8mb4',
  };
}

/**
 * 读取 config.ini [scraper] 中的 start_date / end_date，用于前端默认日期过滤。
 * 支持 today 关键字，返回 { startDate: 'YYYY-MM-DD' | '', endDate: 'YYYY-MM-DD' | '' }
 */
function resolveDate(v) {
  const s = (v || '').trim();
  if (!s) return '';
  if (s.toLowerCase() === 'today') {
    const d = new Date();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${d.getFullYear()}-${mm}-${dd}`;
  }
  return s;
}

function loadScraperDates() {
  const candidates = [
    path.join(__dirname, '..', '..', 'config.ini'),
    path.join(process.cwd(), 'config.ini'),
    path.join(process.cwd(), '..', 'config.ini'),
  ];
  let raw = null;
  for (const p of candidates) {
    if (fs.existsSync(p)) {
      try { raw = fs.readFileSync(p, 'utf-8'); break; } catch (e) { /* ignore */ }
    }
  }
  if (!raw) return { startDate: '', endDate: '' };
  const cfg = ini.parse(raw);
  const s = cfg.scraper || cfg.Scraper || {};
  const startDate = resolveDate(s.start_date || '');
  const endDate   = resolveDate(s.end_date   || '');
  return { startDate, endDate };
}

module.exports = { loadDbConfig, loadScraperDates };
