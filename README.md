# 数据分析工作台

面向企业数据分析师与数据产品经理的一站式数据分析工作台。提供指标口径管理、聚合查询、分析实验设计、数据服务发布与应用集成的完整链路。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标口径管理 | `indicator` | 业务指标 | 指标编号、指标名称、计算公式 |
| 分析数据源 | `datasource` | 数据源连接 | 连接编号、连接名称、数据库类型 |
| 聚合查询配置 | `query` | 查询模板 | 模板编号、模板名称、查询语句 |
| 分析实验管理 | `experiment` | 分析实验 | 实验编号、实验名称、分析数据集 |
| 分析数据集 | `dataset` | 数据集 | 数据集编号、数据集名称、来源表名 |
| 可视化图表配置 | `chart` | 图表配置 | 图表编号、图表名称、图表类型 |
| 分析报告管理 | `report` | 分析报告 | 报告编号、报告标题、关联数据集 |
| 数据服务发布 | `service` | 数据服务 | 服务编号、服务名称、输出数据集 |
| 数据看板 | `dashboard` | 看板面板 | 面板编号、面板名称、布局配置 |
| 分析维度管理 | `dimension` | 分析维度 | 维度编号、维度名称、维度表名 |
| 全局筛选器 | `filter` | 筛选器 | 筛选器编号、筛选器名称、关联字段 |
| 定时分析任务 | `schedule` | 定时任务 | 任务编号、任务名称、分析类型 |
| 数据导出配置 | `export` | 导出模板 | 模板编号、模板名称、数据源绑定 |
| 查询缓存管理 | `cache` | 缓存规则 | 规则编号、缓存目标、缓存时间 |
| 分析模型管理 | `model` | 分析模型 | 模型编号、模型名称、模型类型 |
| 分析操作日志 | `log` | 操作日志 | 日志编号、操作类型、操作对象 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 模型部署兼容与降级

分析模型的版本切换遵循以下链路：**数据集字段版本 → 模型版本快照 → 部署/降级事务 → 数据服务调用清单**。
兼容规则集中在 `backend/app/services/compatibility.py`，业务动作在 `backend/app/services/model.py`。

### 兼容规则

1. **输入字段必须存在**：模型版本声明的输入字段若已不在当前数据集字段列表中，判定为「字段缺失」，阻断部署与请求。
2. **数据集版本不能超前来**：版本快照记录的数据集版本必须能在数据集字段版本历史中找到，且不能新于当前版本。
   旧于当前版本但字段仍被覆盖时只记为警告。
3. **参数配置必须与版本记录一致**：部署时显式覆盖的参数与目标版本快照不一致即阻断；仅与上一生效版本不同只记为警告。
4. **字段新增不阻断**：数据集新增模型未使用的字段只记为警告，模型仍兼容。

### 降级规则

- 只从版本历史里挑选**严格早于当前生效版本**、标记为稳定、且按上述规则兼容的版本；
  默认目标是模型记录的 `上一稳定版本`，不会越过稳定版本继续向下。
- 当前已处于可兼容稳定版本时，重复降级只做幂等确认，不再切换。
- 部署、降级、调用触发降级都在整库快照保护下执行，任何失败/超时都会原子回滚到原版本。
- 支持 `request_id` 幂等台账：同一请求重复到达只执行一次；超时后的重试回到原版本，不重复切换。
- 生效版本会同步写入模型详情、部署记录和数据服务清单（`生效模型版本` / `服务调用状态`）。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/model?keyword=&status=&version=&order=&sort=` | 按模型编号检索、部署状态筛选、版本先后定位 |
| GET | `/api/model/{id或模型编号}` | 模型详情，含生效版本、字段差异和兼容报告 |
| GET | `/api/model/{id}/compatibility` | 字段差异与各版本兼容检查结论 |
| GET | `/api/model/{id}/deployments` | 部署、降级、下线记录 |
| GET | `/api/model/differences` | 兼容差异清单，筛选条件与模型列表一致 |
| POST | `/api/model/{id}/deploy` | 部署前兼容检查；不兼容时阻止并给出可兼容稳定版本 |
| POST | `/api/model/{id}/fallback` | 降级到上一稳定版本；重复/超时重试不重复切换 |
| POST | `/api/model/{id}/invoke` | 模拟调用；旧字段请求默认拒绝，`mode=fallback` 时切到稳定版本 |
| POST | `/api/dataset/{id}/actions` | 带 `字段列表` 时发布新数据集版本，刷新依赖模型与服务调用状态 |
| GET | `/api/service?version=` | 服务清单按生效模型版本定位，实时同步模型生效版本 |

联调辅助字段（仅用于故障演练）：`simulate_timeout` / `simulate_failure` 会让部署或降级走原子回滚路径；
`request_id` 用于验证幂等。

### 测试

```bash
cd backend && .venv/bin/python -m unittest discover -s tests -v
```

领域：数据工程
子方向：数据分析与数据应用
