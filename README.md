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

### 访问统计（PV/UV，可选）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_STATS_SALT` | 空 | UV 匿名化的盐。**留空即视为未启用**，两个统计接口返回空统计，前端组件自动隐藏 |
| `API_STATS_PREFIX` | `/api/v09/stats` | 主路由前缀（进 openapi）；与 `/api/v09/data` 并列。无论怎么改，同一份路由都会再挂一份 `/api/stats` 别名（前端默认就用它） |
| `API_STATS_TABLE` | `visit_event` | 明细表名（只允许字母数字下划线） |
| `API_STATS_CACHE_SECONDS` | `45` | 窗口统计的服务端缓存秒数（按 `range` 分别缓存），`0` 不缓存 |
| `API_STATS_RETENTION_DAYS` | `180` | 明细保留天数，`<=0` 不清理（每小时最多清理一次） |
| `API_STATS_DATABASE_URL` | 无 | 写库连接串，优先级最高（测试用 SQLite） |
| `API_STATS_TRUST_SSO_HEADER` | `true` | 是否采信反代透传的 SSO 登录身份（`X-Auth-Request-Email` / `-Preferred-Username` / `-User`）。置 `false` 可整体退回 `visitor_id` / 指纹 |
| `API_STATS_MYSQL_USER` / `API_STATS_MYSQL_PWD` | 无 | 写库专用账号（需 `INSERT`/`DELETE`）；不填则复用 `HK_DB_MYSQL_USER` |

> 写库连接默认复用 `HK_DB_MYSQL_HOST` / `HK_DB_MYSQL_PORT` / `HK_DB_MYSQL_DB`，只有账号可以单独指定 —— 通用查询接口依然是只读的，统计写入不走 `run_query`。

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

### 2. 首页概览聚合 — `GET /api/v09/data/home/*`

首页两块概览卡原来由前端直连 MySQL 拼 SQL，现改为服务端聚合。

主前缀挂在 `/api/v09/data/home` 下 —— 线上 nginx 只反代了 `/api/v09/data/`，这样不必新增 location；
同时保留 `/api/v09/home`、`/api/home` 两个别名（不进 openapi），以后 nginx 怎么放行都能用：

| 路径 | 说明 | 数据源 |
|------|------|--------|
| `GET /api/v09/data/home/active-apps?month=YYYY-MM` | 本月采集活跃 APP 数：月度快照（`ad_app_cnt` / `download_app_cnt` / `online_players_app_cnt` / `reserve_app_cnt`）+ 本月每日明细 | `dws_taptap_active_game_stat`（`period_type='monthly'` 与 `'daily'`） |
| `GET /api/v09/data/home/type-share?month=YYYY-MM` | 本月游戏类型占比（dist 0 商业 / 1 独立 / 2 Maker）的下载量、在线峰值、广告曝光，另含三类合计与「未分类」归集 | `dws_taptap_download_periodic_report`、`dws_taptap_pc_online_peak_players_periodic_report`、`dws_taptap_ad_aggr_v6_periodic_report`（`period_type='monthly'` + `category_type='dist'`） |
| `GET /api/v09/data/home/download-rank?month=YYYY-MM&limit=5` | 本月下载榜 TopN，三个榜（`top` 总量 / `gain` 增量最多 / `loss` 下滑最多）各取前 `limit` 名（1~20，默认 5） | `dws_taptap_download_periodic_rank`（`period_type='monthly'`） |
| `GET /api/v09/data/home/ad-rank?month=YYYY-MM&limit=5` | 本月广告曝光榜 TopN，结构同下载榜，数值口径为广告曝光次数 | `dws_taptap_ad_aggr_v6_periodic_rank`（`period_type='monthly'`） |

```bash
curl 'http://127.0.0.1:8000/api/v09/data/home/active-apps?month=2026-11'
# {"code":0,"message":"success","data":{
#   "month":"2026-11",
#   "monthly":{"ad_app_cnt":113,"download_app_cnt":885,"online_players_app_cnt":763,"reserve_app_cnt":206},
#   "daily":[{"date":"2026-11-01","ad_app_cnt":58,"download_app_cnt":375,
#             "online_players_app_cnt":null,"reserve_app_cnt":null}, ...],
#   "latest_day":"2026-11-11"}}

curl 'http://127.0.0.1:8000/api/v09/data/home/type-share?month=2026-11'
# {"code":0,"message":"success","data":{
#   "month":"2026-11",
#   "types":[{"dist":"0","download_total":5366097,"peak_online_players":115069,
#             "ad_views":8555,"game_cnt":443}, ...],
#   "totals":{"download_total":7991242,"peak_online_players":144463,"ad_views":8628},
#   "unclassified":{"download_total":0,"peak_online_players":0,"ad_views":89}}, "total":3}
```

```bash
curl 'http://127.0.0.1:8000/api/v09/data/home/download-rank?month=2026-11&limit=5'
# {"code":0,"message":"success","data":{
#   "month":"2026-11","limit":5,
#   "total_field":"total_download","prev_field":"prev_total_download","diff_field":"diff_download",
#   "categories":[
#     {"key":"top","items":[{"rank_no":1,"app_id":1007673,"app_name":"柚松屋",
#       "icon":"https://...","distribution_type":0,"tags":["模拟"],
#       "total":568784,"prev":262734,"diff":306050}, ...]},
#     {"key":"gain","items":[...]},
#     {"key":"loss","items":[...]}]}}

curl 'http://127.0.0.1:8000/api/v09/data/home/ad-rank?month=2026-11&limit=5'
# 结构同上（广告曝光榜），字段名透出为 total_views / prev_total_views / diff_views
```

口径与行为：

- `month` 缺省取服务器当前月（`Asia/Shanghai`），格式 `YYYY-MM`，非法值直接 **400 / code 40000**，不静默回落
- 两个榜单接口的 `categories` **恒含 top / gain / loss 三项且顺序固定**（当月缺数据时 `items` 为 `[]`）；同榜同 `rank_no` / 同 `app_id` 的残留重复行会去重；`limit` 超出 1~20 返回 400
- 榜单的数值键名统一为 `total` / `prev` / `diff`，真实列名（下载量 `total_download…`、曝光 `total_views…`）通过 `total_field` / `prev_field` / `diff_field` 透出，便于前端确认口径
- `types` **恒含 dist 0/1/2 三项**（当月没有该类型的行时补 0），顺序固定，前端不必判空
- `totals` 只累加这三类；`category_value` 不在 0/1/2 的行（如广告表的「未分类」）单独归集到 `unclassified`，不进占比分母
- 三张月表缺表/缺数时返回 `code 40001` 与数据库原文，不会伪造数据

### 3. 定期报告聚合 — `GET /api/v09/data/periodic/*`

周报 / 月报页面（`everything-ui` 的 `PeriodicReport`）原本由前端拼 4~6 条 SQL、并在 `data.js` 里
自己算「本期 / 上期 / 可用期数」。现在收敛成三个只读接口，一次请求拿到整篇报告：

| 路径 | 说明 |
|------|------|
| `GET .../periodic/periods?period=weekly\|monthly` | 可选期列表：每期起止日期 + 三条业务线各自是否有数据 + 哪条线期数不足 |
| `GET .../periodic/report?period=weekly&key=2026-46&window=12` | 一期完整载荷：6 个核心 KPI（含上期值、环比、sparkline 趋势点、可用期数）、三条业务线的分型拆解、按游戏指标、数据质量标记 |
| `GET .../periodic/rank?line=download\|ad\|online&period=weekly&key=&category=top\|gain\|loss&limit=10` | 一期某业务线榜单；PC 在线榜需带 `list_type=dist_0/1/2`（W45 起），历史周才用 `pc_played` 等旧维度 |

```bash
curl 'http://127.0.0.1:8000/api/v09/data/periodic/report?period=weekly&key=2026-46'
# {"code":0,"data":{
#   "meta":{"periodKey":"2026-46","periodStart":"2026-11-09","periodEnd":"2026-11-15",
#           "prevPeriodKey":"2026-45","isLatest":true,"isPartial":false,
#           "availablePeriods":["2026-40","2026-41","2026-42","2026-43","2026-44","2026-45","2026-46"],
#           "updatedAt":"2026-11-16T11:35:09+08:00","insufficientHistory":false},
#   "coreKpis":[
#     {"key":"download_total","label":"下载总量","unit":"次","value":10475431,
#      "prevValue":7459661,"prevPeriodKey":"2026-45","deltaAbs":3015770,"deltaPct":40.4,
#      "direction":"up","deltaComparable":true,
#      "trend":[{"period":"2026-45","label":"W45","value":7459661}, ...],
#      "availablePeriods":["2026-40", ..., "2026-46"],"historyPoints":7,
#      "insufficientHistory":false,"hasValue":true}, ...],
#   "lines":[{"key":"download","label":"TapTap 下载","kpis":[...],
#             "breakdown":{"items":[{"key":"0","label":"商业游戏","value":7024506,
#                                    "share":0.6706,"prevValue":5113959,"deltaPct":37.4}, ...],
#                          "unclassified":[],"unclassifiedTotal":0,"total":10475431}}, ...],
#   "players":[{"key":"torchlight","name":"火炬之光：无限","download":47319,
#               "downloadDeltaPct":-96.1,"avgOnline":10402.1,"downloadTrend":[...]}, ...],
#   "flags":[{"level":"high","item":"torchlight_download_jump","label":"火炬之光：无限下载量异常放量",
#             "detail":"2026-44 86,842 → 2026-45 1,224,138（14.1×），疑似口径切换或活动冲量", ...}]}}
```

取数与缺值口径：

- 汇总行取 `category_type='all'`，分型取 `category_type='dist'`；三条业务线分别来自
  `dws_taptap_download_periodic_report`（只用 `download_*` 增量列）、
  `dws_taptap_ad_aggr_v6_periodic_report`（`total_views = ad_views + pcebid_views`）、
  `dws_taptap_pc_online_peak_players_periodic_report`；按游戏指标来自 `dws_periodic_report_overview_weekly`
- **缺值不补 0**：某指标某期没有数据时 `value` 为 `null`、`hasValue` 为 `false`，环比取「该指标自己有数据的最近一期」，
  上期为 0 或缺失时 `deltaPct` 为 `null`（不会再画出假的 -100%）
- `availablePeriods` 给每个指标实际能画的期数，少于 3 期时 `insufficientHistory=true`；前端据此决定是否画趋势线
- `isPartial=true` 表示这一期还没走完（月中 / 周中取数），此时 `deltaComparable=false`，环比只作参考
- `flags` 由服务端算好，前端只负责渲染角标：概览表与在线周期表跨表不一致、按游戏单期放量（≥3×）、
  `distribution_type` 未归类、已下线列（`online_peak_pc` 等）、历史期数不足
- 报表汇总行 / 分型行同样只看 `is_active = 1`（表里有该列时自动加过滤，没有就不加）。
  列名按表探测一次并**进程内缓存**：以后给某张报表加 `is_active`，要重启服务才生效；
  探测本身失败时降级为「不过滤」并记 warning，不会把整篇报告打成 500。本库这几张表的
  `is_active` 是 `tinyint NOT NULL DEFAULT 1`，代码里用 `COALESCE(is_active,1)` 属防御性写法：
  DWS 下线残留行是「置 0 不删数据」，例如广告的 `(未分类)` 行，不过滤就会多出一个桶
- 榜单默认过滤 `is_active = 1`（线上有大量下线残留行），同 `(rank_no, app_id)` 去重
- **在线榜的样本信号**：每行带 `realHours`（该游戏本周期有有效在线的小时数，下载/广告榜为 `null`），
  payload 带 `sample: {minRealHours, maxRealHours, medianRealHours, lowSample}`。
  判定为 **`medianRealHours < 8`（严格小于）**，量的是「榜内单条数据够不够厚」：
  W46 `dist_1` / `dist_2` 中位数 4.5 / 4 → 低样本；W46 `dist_0` 中位数 84 → 正常。
  W45 `dist_0` 中位数正好 8.0 → 不判低样本：它是高区分度的商业榜，那一周只是覆盖只有 8 小时，
  属于覆盖问题，交给 `online_partial_coverage`，两条提示不叠加。
  前端据此决定「标低样本」而不是自己猜数值分布（猜「全部相同」会把真故障一起藏掉）
- **覆盖拆成三个正交信号**（`lines[online].coverage`，其他线为 `null`）：

  | 信号 | 字段 | 含义 | 是否告警 |
  |------|------|------|----------|
  | 窗口内完整性 | `sampleHours` / `expectedSamples` / `ratio` / `gap` / `windowEndFinal` | 采集窗口（源起点~期末，未结束的期只到昨天）里实采/应采 | **是**：`ratio < 0.9` 出 `medium` flag，但 `windowEndFinal=false` 的期只打印不判失败 |
  | 期初无源 | `sourceStart` / `sourceStartsLate` | 在线数据 2026-11-08 才开始有，期初几天本来就没数据 | 否，只出 `info` flag + 阻断环比 |
  | 采集是否还在跑 | `meta.updatedAt` | 最近更新时间，新鲜度由巡检按 ≤14h 判 | 否 |

  另给 `periodRatio`（期口径，含期初无源的天，如 2026-11 = 92/192 ≈ 48%）**只作参考、不告警**——
  它是一个永远补不回来的比例，拿来告警会红到月底然后被所有人忽略。
  W45：窗口 11-08 一天 = 12 点、实采 8 → 8/12 = **67%**（窗口内确有缺失，medium）；
  2026-11：窗口 11-08~11-15 = 96 点、实采 92 → **96%**（不告警），另有 `info` 说明期初无源。
  窗口末端取期末（进行中的期取昨天）而不是「末采样日」：否则采集中途挂掉时窗口会跟着缩短，
  反而算出 100% 假绿。进行中的期 `windowEndFinal=false`：昨天的数据要等每天 00:05 的同步任务
  落库，同步延迟时这一格会短暂偏小，巡检对它只打印不判失败。
  `reason=collection_gap` 指「采集窗口内少采了」；「源本来就没有」不在这条里，它走 `sourceStartsLate`。
  **级别按「本期数字准不准」定，不按「能不能修」定**（与巡检 FAIL 口径一致）：
  `scope=current` 且 `gap=true`（窗口内完整度 < 0.9）→ `medium`——哪怕缺失是启动造成的、
  补不回来（W45 用 8/12 的样本算出了整周的 581 款，读者必须知道这页数不能直接引用）；
  `scope=previous` → 永远 `info`（只解释为什么没有环比）；
  期初无源但窗口内采得够（2026-11 的 92/96）→ `info`。

  覆盖类 flag 都带 `scope`（`current` / `previous`）与 `relatedPeriod`，钉死它讲的是哪一期：
  查 W46 时会附一条讲 W45 的 flag（解释「为什么本期没有环比」），`scope=previous` 时 `level`
  是 `info` 而不是 `medium`，巡检/告警按 `scope` 过滤即可，不会把上期的问题当本期告警。
- PC 在线榜的维度参数是 **`list_type`（下划线）**；传 `listType` 会 400 并提示正确写法
  （FastAPI 会静默忽略拼错的 query 参数，那样只会拿到混合分组榜单而看不出来）
- **旧维度查历史周**：`list_type=pc_played / pc_emulator_played / app_hot_creative` 的行
  DWS 按规矩标成 `is_active=0`，但历史周只有它们。查这些维度时接口**跳过 `is_active` 过滤**，
  并在响应里带 `retiredDimension=true`，UI 可据此标「旧维度·仅历史期」；查 `dist_*` 时仍只取
  `is_active=1`
- 结果在进程内缓存 60 秒（`CACHE_TTL_SECONDS`），报告一天只更新几次，重复请求不再打库

`coreKpis` 的 6 个 key（顺序固定，前端直接按 key 取，不要再拼 SQL）：

| key | 含义 | 单位 | 来源业务线 |
|-----|------|------|------------|
| `online_game_cnt` | 在线游戏数（去重） | 款 | online |
| `online_peak_players` | 在线峰值 | 人 | online |
| `download_total` | 下载总量 | 次 | download |
| `download_pc` | PC 下载量 | 次 | download |
| `download_app` | APP 下载量 | 次 | download |
| `ad_total` | 广告曝光（`ad_views + pcebid_views`，**采集位次**） | 次 | ad |

**改名历史（兼容性）**：`online_peak` 已在 `ed3886c` 改名为 `online_peak_players`，
对齐 `report-metric-spec.json`；接口**不再返回旧名**，仍按 `online_peak` 取值会拿到 `undefined`。
其余 key 从未变过。若以后还有改名，会在本条下面按「旧名 → 新名 @ commit」追加，不做双写别名（双字段迟旱漂成两个值）。

每个 KPI 对象字段：`value` / `prevValue` / `prevPeriodKey` / `deltaAbs` / `deltaPct` /
`direction` / `deltaComparable` / `trend[]` / `availablePeriods` / `historyPoints` /
`insufficientHistory` / `hasValue` / `unit` / `isInt`；需要解释口径的指标额外带 `note`（目前只有广告三项：每游戏每小时在每个广告坑位出现计 1 次，**不是用户级曝光量**）。
`meta` 里还有整篇的 `availablePeriods`、`insufficientHistory`、`isPartial`、`retiredColumns`、`distLabels`。

**在线类指标的口径已定**：以在线周期表 `dws_taptap_pc_online_peak_players_periodic_report`
（`category_type='all'`）为准，概览表的 `online_game_cnt` / `online_peak_players` 只供
「按游戏」指标使用。两表差异不是旧快照（W45 概览偏大、W46 概览偏小，方向相反），
所以接口只把它作为 `medium` flag 透出，不参与 KPI 取值。

分发类型文案统一为：`0 商业游戏 / 1 独立小游戏 / 2 Maker 小游戏`（全站一致，旧名
「创意工坊 / TapMaker」已废弃）。

**在线款数：两个指标，不许相减**

| 指标 | 含义 | 位置 | 环比 |
|------|------|------|------|
| `online_game_cnt` | 本周有在线记录的游戏（`app_id` 去重）**主口径** | `coreKpis` / `lines[online].kpis` | 正常（覆盖不全时置 null） |
| `concurrent_game_cnt` | 峰值那一小时同时在线的款数（概览表口径） | 只在 `lines[online].kpis` | **永远为 null**（`deltaBlocked=secondary_metric`） |

概览周表是每周一生成上一周的，所以**进行中的当期没有这一行**：该期 `concurrent_game_cnt`
的 `value=null`、`hasValue=false`，并带 `unavailableReason` 说明原因（不是数据断了，
前端别渲染成 0）；跨表差异 flag 同样不会挂在当期上。

W46 去重 860、并发 671，两者相差 189 款是结构性的（同一时刻 vs 整周），不是数据错误。

**环比会在什么时候被置 null**：KPI 上的 `deltaBlocked` 字段给出原因

| 取值 | 含义 |
|------|------|
| `coverage_partial` | 本期**采集窗口内**有缺失（`coverage.ratio < 0.9`），此时另出 `medium` flag |
| `prev_coverage_partial` | 上期窗口内缺采，或上期期初无源（`sourceStartsLate`），环比不可比（如 W46 拿 W45 的 581 当基数算出 +48%，假的） |
| `secondary_metric` | 副指标本来就不出环比 |

置 null 时 `prevValue` / `deltaAbs` / `deltaPct` / `prevPeriodKey` 一并清空、`deltaComparable=false`，
避免调用方自己用 `prevValue` 再算一次；`trend[]` 仍保留原始点。该期还会多一条 `flags`
（如 `online_partial_coverage`），`detail` 里直接写着「仅覆盖 11-07~11-08（2/7 天）」。

**同小时快照 `lines[online].peakHour`**（取不到时字段不存在，不影响整篇）：

```json
{"crawledAt":"2026-11-13 22","gameCnt":653,"peakOnlinePlayers":146354,
 "sumGameCnt":653,"sumPeakOnlinePlayers":146354,"sumsMatch":true,
 "breakdown":[{"distributionType":"0","label":"商业游戏","gameCnt":440,"peakOnlinePlayers":127110}, …]}
```

来源是周期表里 DWS 每天重算的 `category_type='dist_peak'` 行（同小时快照），接口不读明细表、也不自己算。
表里有 `category_value='ALL'` 行时以它为准（`gameCnt` 就是该小时并发的 653，**不是整周去重的 860**）。
DWS 的 builder 保证「分型行与 ALL 行同一批产出」，所以正常数据不会出现「有分型、缺 ALL」；
「三项求和 + `all` 行峰值校验」只是手工写入/老数据异常时的防御性兜底。
整个字段取不到时缺省，不影响整篇报告。

**一条铁律**：占比 / 堆叠 / 合计只能用 `peakHour`。

| 字段 | 含义 | 能否相加 |
|------|------|----------|
| `lines[online].breakdown` | 每个分型**各自**的周内峰值峰值 | ❌ 峰值不在同一小时（W46 相加 146,929 > 146,354） |
| `lines[online].peakHour` | 全局峰值那一小时的同小时快照 | ✅ Σ 恒等于该期总峰值 |

`breakdown.consistency` 里现在带 `summable` 与 `note`：在线线是 `summable=false`
（`gapPct=0.39` 是结构性结果，**不是数据质量问题，别标红**），下载/广告是 `true`。
`peakHour.sumsMatch=false` 才是真告警（同小时快照都加不平 = 口径被动过）。

**采集兜底值占比：两个口径，别混用**

上游对低体量游戏返回哨兵值 100（不是我们填的默认值），所以「多少款含水」和「多少人含水」
是两个数，可以差 3 倍（W46 商业游戏：行数 42.95% vs 人数 14.87%）。

| 字段 | 分母 | 统计窗口 | 回答的问题 |
|------|------|----------|-----------|
| `peakHour.placeholderRatio` / `peakHour.breakdown[].placeholderRatio` | app-小时行数 | **峰值那一小时** | 这一小时里多少款游戏没拿到真值 |
| `lines[online].breakdown.items[].placeholderRatio` | app-小时行数 | **整个周期** | 这一周里该分型多少比例的采集行为哨兵值 |
| `peakHour.placeholderPlayerRatio` / `peakHour.breakdown[].placeholderPlayerRatio` | 在线人数 | **峰值那一小时** | 这一小时的在线人数里哨兵值贡献了多少 |
| `lines[online].breakdown.items[].placeholderPlayerRatio` | 在线人数 | **整个周期** | 这一周该分型的在线人数里哨兵值贡献了多少 |
| `*.placeholderPlayers` | 整数人数 | 同上两窗口 | 兜底直接贡献的在线人数（W46 独立 7,400）。**算余量用它**：`peakOnlinePlayers - placeholderPlayers`，不要用 `ratio × total` 反推（会 ±1 抖动） |

注意前两行的**分母口径相同但窗口不同**（W46 实测 0.43 vs 0.34），不是数据打架，
也不要去对账。字段名与 DWS 表列 `placeholder_ratio` 保持一致，名字里的「ratio」不自带单位，
所以「这是款数口径」只在本文档定义一次。

- 两者都是 0~1、保留 4 位小数；表里没有该列的线（下载/广告）为 `null`。
- `placeholderPlayerRatio` **直接读 DWS 的 `placeholder_player_ratio` 列**（峰值小时与整期各有自己的值，
  所以两个位置的数字不同是正常的：W46 dist_1 = 峰值小时 86.79% vs 整期 74%）。
- 只有老库还没有这一列时，才回退到 `round(placeholderRatio × gameCnt) × 100 / peakOnlinePlayers` 推导；
  该推导依赖「哨兵值恒为 100 且每行计 1 人」，**只对 `dist_peak` 行成立**，整期汇总行不会走推导（返回 `null`）。
- 阈值建议：`placeholderPlayerRatio > 0.5` 标「该分型人数含水，占比仅作参考」；
  `placeholderRatio > 0.5` 标「该分型多数游戏无真值」（两者不要共用同一句话）。

W45 的 `peakHour` 是 `2026-11-08 20`（576 款 / 125,697 人），不是概览表的 133,480：
后者来自 11-07 20 点，那天的明细已被清理，不参与分型。

与 `report-metric-spec.json` 的对应关系：KPI key 完全一致；spec 里的 `qualityFlags`
就是本接口顶层的 `flags`（同一组 `{level,item,label,detail,action}` 对象）。

部署后一条命令验收（只读，失败时非 0 退出，可当发布门禁）：

```bash
# 默认打 http://127.0.0.1:8000（与本文档其他地方一致；服务改端口时用 --base 覆盖）
python scripts/smoke_periodic.py
python scripts/smoke_periodic.py --base http://10.0.0.5:8000 --period monthly
```

### 4. 列出表 — `GET /api/v09/data/tables`

MySQL 下执行 `SHOW FULL TABLES`，返回表名数组，方便排查「接口查的表到底在不在」。

### 5. 健康检查

| 路径 | 说明 |
|------|------|
| `GET /health` | 进程状态 + 数据库配置是否齐全，**不连库** |
| `GET /health/db` | 真正执行 `SELECT 1`，连不上返回 503 |

### 6. 访问统计 — `POST {API_STATS_PREFIX}/hit`、`GET {API_STATS_PREFIX}/week`、`GET {API_STATS_PREFIX}/summary`

给 `everything-ui` 的「本周 PV/UV」组件用，主前缀 `/api/v09/stats`（与 `/api/v09/data` 并列），
并额外挂一份 `/api/stats` 别名 —— 前端默认就请求 `https://horgrix.com/api/stats`，nginx 按原路径转发即可。

上报一次浏览（`visitor_id` 强烈建议带上）：

```bash
curl -X POST http://127.0.0.1:8000/api/v09/stats/hit \
  -H "Content-Type: application/json" \
  -H "User-Agent: Mozilla/5.0 ..." \
  -d '{"path": "/taptap", "visitor_id": "3f2b9c1e-8d4a-4b77-9c02-7b1e5a6d9f10", "referrer": "https://horgrix.com/business"}'
# {"code":0,"message":"success","data":{"counted":true,"identity":"cid"}}
```

读本周统计（老契约字段不变，另附 `identities`，周一 00:00 起，Asia/Shanghai）：

```bash
curl http://127.0.0.1:8000/api/v09/stats/week
# {"code":0,"message":"success","data":{"week_start":"2026-10-26","pv":1234,"uv":86,
#   "top_paths":[{"path":"/taptap","pv":620,"uv":41}],
#   "identities":{"sso":1100,"cid":120,"fp":14}}}
```

读任意窗口 + 曲线（`?range=week|7d|30d|today|all`，默认 `week`）：

```bash
curl 'http://127.0.0.1:8000/api/v09/stats/summary?range=7d'
# {"code":0,"message":"success","data":{
#   "range":"7d","start":"2026-10-29","end":"2026-11-04","granularity":"day",
#   "week_start":"2026-11-02","pv":412,"uv":97,
#   "series":[{"date":"2026-10-29","pv":58,"uv":21}, ...],
#   "top_paths":[{"path":"/taptap","pv":221,"uv":48}],
#   "identities":{"sso":380,"cid":28,"fp":4}}}
```

口径与行为：

- **时区**固定 `Asia/Shanghai`，所有窗口按自然日对齐；`week` = 本周一 `00:00:00` ~ 现在，`7d`/`30d` = 今天在内的最近 7/30 个自然日，`today` = 今天 00:00 起，`all` = 明细中最早一条起
- **PV** = 明细行数，每次上报算 1 次，**服务端不按时间窗口去重**（防连点交给前端路由级 1.5s 去重）
- **UV** = 窗口内 `COUNT(DISTINCT visitor)`；`series` 里是天（月）内的独立访客数，逐日 UV 相加不等于窗口 UV，属正常
- **访客身份**（按优先级）：
  1. **SSO 登录用户** —— 站点整站走 oauth2-proxy + Keycloak 时，nginx 把 `X-Auth-Request-Email`（以及 `-Preferred-Username` / `-User`）透传进来，服务端只存 `sha256('sso:' + 身份 + 盐)` 前 32 位，`identity: "sso"`。同一账号换设备、换 IP、换浏览器都算一个人
     - **优先用 email，而不是 Keycloak 的 `sub`**：`sub` 按 realm/client 生成，同一个人的 dev、prod 两个 client 下 `sub` 不同，UV 会把一个人算成两个；email 跨 client 稳定。没有 email 时才依次回落 `preferred-username` → `sub`
  2. 请求体里的 `visitor_id` —— 前端第一方匿名 ID（8~64 位 `[A-Za-z0-9_-]`，建议 localStorage 里的 uuid4），存 `sha256('cid:' + id + 盐)` 前 32 位，`identity: "cid"`
  3. 两者都没有：回退 `sha256(IP + UA + 盐)` 前 32 位，`identity: "fp"`
  
  原 IP / 原 UA 都不入库。用 `sso`/`cid` 是为了避开两个老问题：移动网络 IP 漂移把一个人算成多个 UV，同一出口 NAT 又把多人并成 1。
  信任边界在边缘 nginx（`uvicorn` 开了 `proxy-headers`，`request.client.host` 是真实访客 IP，靠对端 IP 判断不可靠）：
  受保护站点的 `location /api/stats/` 用 `proxy_set_header X-Auth-Request-Email $sso_email;`（以及 `-User $sso_user`）**覆盖**这两个头（均来自 `auth_request_set`），
  其它不鉴权的入口显式 `proxy_set_header X-Auth-Request-User "";` 置空，防止伪造。
  需要 oauth2-proxy 开 `set_xauthrequest = true`，否则只有 `sub`、没有 email。
- **不计数**：空 User-Agent、Bot/爬虫 UA（含 `python-requests`、`curl/`、`Go-http-client` 等）、`?no_track=1`、`X-No-Track` 头
- `path` 服务端会再归一化一次（去 query/hash，限长 128），`title` 只校验不入库
- **无数据**时 `pv`/`uv` 返回 `0`、`top_paths` 返回 `[]`、`series` 补齐窗口内全部桶（值为 0），不会是 `null`
- `series` 的 `date` 默认 `YYYY-MM-DD`；窗口超过 180 天时 `granularity=month`，`date` 为 `YYYY-MM`
- `range` 取值非法返回 **HTTP 400 / code 40000**，不静默回落（避免前端参数写错却看不到）
- 依赖缺失（没配盐 / 表不存在 / 写库连不上）时接口都**降级为空数据、返回 200**，不向前端报 5xx —— 前端拿不到就隐藏组件
- `top_paths` 按 PV 倒序，最多 3 条；窗口统计带 `API_STATS_CACHE_SECONDS`（默认 45 秒）进程内缓存，按 `range` 分别缓存，新上报即整体失效
- `identities` = 窗口内 PV 按身份来源拆分（`sso` / `cid` / `fp`），三项**恒存在**、值不会是 `null`，三项之和等于 `pv`；前端拿它算百分比就能看出身份有没有降级成浏览器标识。`/week` 与 `/summary` 都带

首次使用先建表（DDL 在 `sql/visit_event.sql`，可重复执行）：

```bash
python scripts/init_visit_event.py --check   # 只看表在不在
python scripts/init_visit_event.py          # 建表
```

### 6.1 v1 → v2 表结构（一次性，会清空历史）

v2 相比 v1 只多一列 `visitor_src`（`cid` / `fp`），并去掉写入时的 30 分钟去重，
所以旧结构的表无法直接沿用。确认可以清空历史数据后：

```bash
python scripts/reset_visit_event.py        # 体检：打印现有行数与列
python scripts/reset_visit_event.py --yes  # DROP 并按 v2 重建（历史访问数据全部丢失）
```

`API_STATS_SALT` 与访客身份相关：换盐等于换 UV，生产上别随手改。

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
│   ├── stats.py           # 访问统计：访客身份、时间窗口、series 聚合、缓存、清理
│   ├── schemas.py         # 请求/响应模型
│   ├── errors.py          # 错误码与统一响应
│   └── routers/
│       ├── data.py        # 数据接口
│       └── stats.py       # 访问统计接口
├── sql/visit_event.sql    # 访问统计建表 DDL（v2）
├── scripts/init_visit_event.py  # 建表脚本（--check 只检查）
├── scripts/reset_visit_event.py # v1→v2 重建（--yes 会清空历史）
├── tests/                 # 用临时 SQLite 验证接口契约
├── .env.example
├── requirements.txt
└── pyproject.toml         # pytest / ruff 配置
```

---

## 测试与检查

```bash
pip install -r requirements-dev.txt
pytest          # 32 个用例（含跨域预检/响应头、PV/UV 上报、窗口与 series 聚合），用临时 SQLite 覆盖接口契约与安全校验
ruff check app tests
```

SQL 级测试用 SQLite 跑（走 `API_DATABASE_URL` 覆盖），因此**不需要本地 MySQL 也能验证接口行为**；连真实 MySQL 时需要自己配好 `HK_DB_MYSQL_*` 后访问 `/health/db` 确认。

---

## 部署提示

- 用只读 MySQL 账号（`GRANT SELECT`）最稳；确需写入再把 `API_ALLOW_WRITE` 打开
- 前置 Nginx / 网关做 HTTPS 与访问控制：这个接口能执行任意 `SELECT`，不要裸奔在公网
- `API_CORS_ORIGINS` 上线后改成前端具体域名
- 多 worker 部署时每个进程各自持有一个连接池（默认 5 常驻 + 10 溢出），注意给数据库留够连接数

### 访问统计上线步骤

1. 用**写权限账号**建表：`python scripts/init_visit_event.py`（或手动执行 `sql/visit_event.sql`）
2. `.env` 里配 `API_STATS_SALT`（随机串，改动即等于重置 UV 口径）
3. 写库账号与只读账号不同时，补 `API_STATS_MYSQL_USER` / `API_STATS_MYSQL_PWD`
4. nginx 转发（公网站点 horgrix.com 的同源方案，前端零改动）：
   ```nginx
   # /etc/nginx/conf.d/horgrix.com.conf，加在 location /api/ 之前或之后均可（前缀最长匹配）
   location /api/stats/ {
       proxy_pass http://127.0.0.1:8000;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
   }
   ```
   走 `api.horgrix.com` 的方案则加 `location /api/v09/stats/ { proxy_pass http://127.0.0.1:8000; ... }`，
   前端把 `VITE_STATS_BASE_URL` 指到 `https://api.horgrix.com/api/v09/stats`（跨域已放行）
5. 重启服务后验证：
   ```bash
   curl -X POST https://<域名>/api/v09/stats/hit -H "Content-Type: application/json" \
     -H "User-Agent: Mozilla/5.0 (compatible; check)" -d '{"path":"/deploy-check"}'
   curl https://<域名>/api/v09/stats/week
   ```
   再访问一次站点，确认 `pv` 增长、`top_paths` 出现真实路径
