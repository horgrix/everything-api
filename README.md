# Everything API

Everything 数据平台的查询服务：基于 **FastAPI + SQLAlchemy + MySQL**，对外提供直接执行 SQL 的查询接口。

- 连接信息全部来自环境变量，不落代码、不进仓库
- 统一响应结构 `{code, message, data, total, truncated}`，与 `everything-ui` 现有前端契约一致
- 默认**只读**：只放行 `SELECT / WITH / SHOW / DESC / DESCRIBE / EXPLAIN`，只允许一次提交一条语句
- 单次返回行数、查询超时都有服务端上限，避免一次请求拖垮数据库

---

## 快速开始

```bash
cd everything-api

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env      # 填入真实的 MySQL 连接信息
python -m app             # 等价于 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动后：

- 交互式文档 <http://127.0.0.1:8000/docs>
- 健康检查 <http://127.0.0.1:8000/health>

也可以用 uvicorn 直接起（生产建议关掉 reload）：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

---

## 环境变量

### MySQL 连接（必填）

| 变量 | 说明 | 示例 |
|------|------|------|
| `HK_DB_MYSQL_HOST` | 主机 | `10.0.0.12` |
| `HK_DB_MYSQL_PORT` | 端口，默认 3306 | `3306` |
| `HK_DB_MYSQL_USER` | 用户名（建议只读账号） | `readonly_user` |
| `HK_DB_MYSQL_PWD` | 密码 | `******` |
| `HK_DB_MYSQL_DB` | 库名 | `everything` |

连接串由上述变量拼装为 `mysql+pymysql://...?charset=utf8mb4`，密码做了 URL 转义，包含 `@`、`/` 等字符也能正常连接。

### 服务行为（可选）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_HOST` | `0.0.0.0` | 监听地址（`python -m app` 使用） |
| `API_PORT` | `8000` | 监听端口 |
| `API_ALLOW_WRITE` | `false` | 是否允许非只读语句，生产请保持 `false` |
| `API_MAX_ROWS` | `5000` | 单次返回行数上限，请求里的 `max_rows` 会被压到该值 |
| `API_QUERY_TIMEOUT_MS` | `30000` | 查询超时（毫秒），通过 `MAX_EXECUTION_TIME` 生效，需 MySQL 5.7.8+ |
| `API_CORS_ORIGINS` | `*` | 允许的来源，逗号分隔；上线建议收紧成具体域名 |
| `API_CORS_ALLOW_CREDENTIALS` | `false` | 是否允许跨域携带 Cookie / Authorization |
| `API_DATABASE_URL` | 无 | 直接指定 SQLAlchemy URL，优先级高于 `HK_DB_MYSQL_*`（测试用） |

---

## 接口

### 1. 执行 SQL — `POST /api/v09/data/sql`

```bash
curl -X POST http://127.0.0.1:8000/api/v09/data/sql \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "SELECT game_name, download_count FROM dws_taptap_download_daily WHERE dt = :dt ORDER BY download_count DESC",
    "params": {"dt": "2026-10-19"},
    "max_rows": 100
  }'
```

请求字段：

| 字段 | 必填 | 说明 |
|------|------|------|
| `sql` | 是 | 单条 SQL；只读模式下限 `SELECT / WITH / SHOW / DESC / DESCRIBE / EXPLAIN` |
| `params` | 否 | 绑定参数。`dict` 对应 `%(name)s` / `:name` 占位符，`list` 对应 `%s` 占位符 |
| `max_rows` | 否 | 本次最大行数，不超过 `API_MAX_ROWS` |

返回：

```json
{
  "code": 0,
  "message": "success",
  "data": [{"game_name": "火炬之光", "download_count": 1500}],
  "total": 1,
  "truncated": false
}
```

`total` 是本次返回的行数，`truncated` 为 `true` 表示命中上限、结果被截断（还有更多行）。

### 2. 列出表 — `GET /api/v09/data/tables`

MySQL 下执行 `SHOW FULL TABLES`，返回表名数组，方便排查「接口查的表到底在不在」。

### 3. 健康检查

| 路径 | 说明 |
|------|------|
| `GET /health` | 进程状态 + 数据库配置是否齐全，**不连库** |
| `GET /health/db` | 真正执行 `SELECT 1`，连不上返回 503 |

---

## 跨域（CORS）

默认放开所有来源，前端本地开发（Vite、直奔 IP 调试）开箱可用：

- 预检 `OPTIONS` 直接返回 200，`Access-Control-Max-Age: 600` 缓存 10 分钟，减少 OPTIONS 往返
- 放行方法 `GET / POST / OPTIONS`，请求头不限制，所有响应（含 4xx / 5xx 错误）都会带 `Access-Control-Allow-Origin`
- 只允许特定前端时设 `API_CORS_ORIGINS=https://ui.example.com,https://admin.example.com`
- 前端要带 Cookie 或 `Authorization` 时，同时设 `API_CORS_ALLOW_CREDENTIALS=true`：此时浏览器规范不允许返回通配符 `*`，服务会自动回显请求来源，无需改成具体域名

```bash
# 验证预检
curl -i -X OPTIONS http://127.0.0.1:8000/api/v09/data/sql   -H "Origin: http://localhost:5173"   -H "Access-Control-Request-Method: POST"   -H "Access-Control-Request-Headers: content-type"
```

> 通配符 + 凭据的组合只应在内网或受控环境下使用；公网部署请把 `API_CORS_ORIGINS` 收紧到具体域名。

---

## 响应与错误码

成功统一为 `code: 0`。失败时 HTTP 状态与 `code` 同时表达语义：

| HTTP | code | 含义 |
|------|------|------|
| 400 | `40000` | 请求参数不合法（body / query 校验失败） |
| 400 | `40001` | SQL 执行失败（语法错、表或字段不存在） |
| 403 | `40300` | 被只读 / 单语句检查拒绝 |
| 500 | `50000` | 服务内部错误 |
| 503 | `50300` | 数据库未配置或连接不可用 |

---

## 项目结构

```
everything-api/
├── app/
│   ├── main.py            # FastAPI 应用、CORS、异常处理、健康检查
│   ├── __main__.py        # python -m app
│   ├── config.py          # 环境变量 → Settings
│   ├── db.py              # 引擎单例、SQL 校验、执行、超时
│   ├── schemas.py         # 请求/响应模型
│   ├── errors.py          # 错误码与统一响应
│   └── routers/data.py    # 数据接口
├── tests/                 # 用临时 SQLite 验证接口契约
├── .env.example
├── requirements.txt
└── pyproject.toml         # pytest / ruff 配置
```

---

## 测试与检查

```bash
pip install -r requirements-dev.txt
pytest          # 12 个用例（含跨域预检/响应头），用临时 SQLite 覆盖接口契约与安全校验
ruff check app tests
```

SQL 级测试用 SQLite 跑（走 `API_DATABASE_URL` 覆盖），因此**不需要本地 MySQL 也能验证接口行为**；连真实 MySQL 时需要自己配好 `HK_DB_MYSQL_*` 后访问 `/health/db` 确认。

---

## 部署提示

- 用只读 MySQL 账号（`GRANT SELECT`）最稳；确需写入再把 `API_ALLOW_WRITE` 打开
- 前置 Nginx / 网关做 HTTPS 与访问控制：这个接口能执行任意 `SELECT`，不要裸奔在公网
- `API_CORS_ORIGINS` 上线后改成前端具体域名
- 多 worker 部署时每个进程各自持有一个连接池（默认 5 常驻 + 10 溢出），注意给数据库留够连接数
