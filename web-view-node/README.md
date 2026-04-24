# 招采信息披露 - Node.js 版

与 Python 版（`web_view.py`）功能一致，使用 Express + MySQL2，读取项目根目录 `config.ini` 的 `[database]` 配置。

## 安装与运行

```bash
cd web-view-node
npm install
npm start
```

默认端口 5000，可通过环境变量 `PORT` 修改。

## 技术栈

- **Express**：Web 框架
- **mysql2**：MySQL 连接（Promise）
- **ini**：解析根目录 `config.ini`

## 路由

| 路径 | 说明 |
|------|------|
| `GET /` | 列表页 |
| `GET /detail/:id` | 爬取详情页 |
| `GET /api/list` | 分页列表（支持 keyword、sub_type、product_related） |
| `GET /api/detail/:id` | 单条详情 JSON |
