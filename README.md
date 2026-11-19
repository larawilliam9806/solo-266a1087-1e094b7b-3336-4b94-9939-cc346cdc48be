# 哨点病例脱敏副本发布服务

研究方按年龄段和邮区分析哨点病例。病例清单以整批方式提交，平台按 k-匿名
规则生成**逐行脱敏副本**对外供数；清单一旦修订，旧副本立即撤下，不再供数。

各邮区人口不同，单看病例数难以比较。平台同时提供**人口表登记**与
**人口关联报告**：数据员为指定病例版本整表登记分年龄、分邮区的人口，
报告在可见分组上给出病例数、人口数与每万人报告病例数。

技术栈：FastAPI + SQLite（WAL），支持 `docker compose` 一键启动。

## 快速开始

```bash
docker compose up --build        # 服务监听 http://localhost:8000
```

本地开发（Python 3.11+）：

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
APP_DB_PATH=./app.db .venv/bin/uvicorn app.main:app --reload
```

运行测试：

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -q
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/submissions` | 整批提交新清单（≤40 条） |
| GET  | `/api/publication` | 公开下载当前发布的脱敏副本与参数 |
| GET  | `/api/status`      | 当前发布状态（published / withdrawn / empty） |
| GET  | `/api/submissions` | 提交历史（仅元数据，不含原始病例） |
| POST | `/api/population`  | 为指定病例版本整表登记/替换人口表 |
| GET  | `/api/population?version=` | 查询某病例版本当前登记的人口表 |
| GET  | `/api/report`      | 当前副本可见分组的病例数与人口关联报告 |
| GET  | `/health`          | 健康检查 |

### POST /api/submissions

```json
{
  "k": 3,
  "cases": [
    {"case_id": "A1", "age": 34, "zip": "10001", "disease": "流感"},
    {"case_id": "A2", "age": 36, "zip": "10002", "disease": "哮喘"}
  ]
}
```

**合法性校验**（任一不满足即 HTTP 422，且不影响已发布的旧版本）：

- 整批 1～40 条；`k` 为 2～5 的整数；
- `case_id` 批内唯一、非空；`age` 为 0～100 的整数；
- `zip` 为五位数字；`disease` 非空。

**有效提交的处理**（在单个事务内完成）：

1. 立即撤下旧脱敏副本（删除旧副本行、清空发布指针）；
2. 在 9 种参数组合（年龄段 5/10/20 岁 × 邮区保留前 3/2/0 位）中寻找可行发布：
   - 每个可见（年龄段 × 邮区）组合须 ≥ k 条且含 ≥ 2 种病种；
   - 不达标组合只能整组抑制，抑制总数 ≤ 2 条，且至少保留一个组合；
   - 优先保留更多病例；并列时依次选更细的年龄段、更长的邮区前缀；
3. 可行则发布新版本（HTTP 201，`status=published`，含参数与被抑制病例号）；
   不可行则保持撤下（HTTP 201，`status=withdrawn`，附原因与逐组合诊断）。

### GET /api/publication

- 已发布：HTTP 200，返回同一完整版本的 `version`、`k`、`params`
  （`age_band_years`、`zip_prefix_digits`）、计数与逐行副本；
  副本每行只含泛化字段与病种，如 `{"age_band": "30-39", "zip": "100**", "disease": "流感"}`；
- 已撤下/无数据：HTTP 404，附撤下原因。

### POST /api/population

为某个**已存在的病例版本**整表登记人口（重新登记在单事务内整表替换）：

```json
{
  "version": 1,
  "rows": [
    {"age_lo": 20, "age_hi": 22, "zip_prefix": "111", "population": 1200},
    {"age_lo": 23, "age_hi": 24, "zip_prefix": "111", "population": 800}
  ]
}
```

每行的含义与约束（任一不满足返回 HTTP 422，且不影响上次登记）：

- `age_lo`/`age_hi`：0～100 岁内的**闭区间**年龄，且 `age_lo <= age_hi`；
- `zip_prefix`：0～5 位邮编前缀；传 `null`（或缺省）表示**空前缀 = 全部邮区**，
  非空时为 1～5 位数字串；
- `population`：非负整数人数（允许为 0）；
- 表内至少一行；各行的覆盖范围（年龄闭区间 × 邮编集合）**不得重叠**
  （年龄相交且邮编前缀互为前缀即判重叠，重叠行会在错误信息中定位行号）；
- `version` 必须是已存在的病例提交版本，否则 HTTP 422。

成功返回 HTTP 201，含人口表自身版本号 `pop_version`、行数与总人数。
校验或保存失败时**沿用上次登记**（替换在单事务内进行，异常即回滚）。

`GET /api/population?version=N` 返回该病例版本当前登记的人口表；
尚未登记时 `pop_version` 为 `null` 并附 `missing_population_table` 原因。

### GET /api/report

仅基于**当前匿名副本**生成报告；没有当前副本（撤下/从未发布）时不生成，
返回 HTTP 404 及原因。报告逐组列出副本中的可见分组（年龄段 × 邮区前缀）
及该组病例数，并在顶部标明成对数据的两个版本号：

- `case_version`：报告所用病例副本版本；
- `pop_version`：报告所用人口表版本（该病例版本未登记人口表时为 `null`）。

只有当**若干完整人口行恰好拼成该组的年龄和邮区范围、且总人数大于零**时，
才返回 `population` 与 `cases_per_10000`（= 病例数 ÷ 人口数 × 10000）：

- 匹配按源数据 **0～100 岁**口径进行（如 5 岁段的 `100-104` 标签按单龄
  100 岁匹配）；人口行**不允许拆开估算**；
- 判定基于矩形面积：与该组相交的人口行必须完整落在组边界内，
  且这些互不重叠行的覆盖面积之和恰好等于分组面积。

无法计算人口指标时，该组的 `population`/`cases_per_10000` 为 `null`，
并给出原因码与中文说明：

| `reason_code` | 含义 |
|---|---|
| `missing_population_table` | 缺人口表：当前病例版本尚未登记人口表 |
| `coverage_insufficient` | 覆盖不足：人口行无法恰好拼满该组的年龄与邮区范围 |
| `population_spans_groups` | 人口口径跨组：存在跨越本组边界的人口行，不能拆行估算 |
| `zero_population` | 总人数为零：拼成该组的人口行总人数为零 |

**版本隔离**：病例换版后只使用为新版登记的人口表；旧版本的人口表保留在库中
可供 `GET /api/population` 审计查询，但不会参与新版报告。

## 一致性设计

- **不过期发布**：发布在提交请求内同步完成，写路径持全局可重入锁并以
  `BEGIN IMMEDIATE` 单事务提交，发布与再次提交交错时不会出现过期清单；
- **不撕裂下载**：读路径持同一把锁，副本行与参数始终来自同一完整版本；
- **报告成对一致**：报告在同一把锁内读取发布指针、副本行与人口登记，
  更正与查询交错时，其中的病例与人口必为查询期间同一时刻有效的一对数据，
  并各自标明版本号；
- **人口登记原子替换**：整表替换在单事务内完成，校验或保存失败即回滚，
  沿用上次登记；人口表按病例版本绑定，换版不串用；
- **重启不恢复**：发布指针持久化于 SQLite（`service_state.published_version`），
  旧副本行在撤下时即物理删除，重启后已撤下副本不可恢复；
- **非法不影响旧版**：所有合法性校验在写库之前完成（Pydantic，HTTP 422）。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `APP_DB_PATH` | `./app.db`（容器内 `/data/app.db`） | SQLite 数据库路径 |

## 目录结构

```
app/
  main.py        FastAPI 入口与路由
  models.py      请求模型与合法性校验
  generalize.py  k-匿名泛化与最优参数选择
  population.py  人口行重叠校验与可见分组精确拼配
  store.py       SQLite 持久化、发布状态机、人口登记与报告
tests/
  test_api.py            原有发布/撤下/并发测试
  test_population_unit.py 人口拼配核心逻辑单元测试
  test_population_api.py  人口登记与报告 API 测试
Dockerfile / docker-compose.yml / requirements.txt
```
