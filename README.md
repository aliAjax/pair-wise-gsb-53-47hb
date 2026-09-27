# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限和材料完整性和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 补件停表

- `request_evidence`（专员）：登记`evidence_request_day`（请求日）、`allowed_days`（补件天数）和`pause_clock`（是否停表）。停表时冻结`days_remaining`并保存到`paused_remaining_days`，等待回应期间`overdue`恒为`false`。已有待回应补件时重复发起会被拒绝。
- `respond`（申请人回应）：停表中的案件按`回应日 + 保留剩余天数`重算`deadline_day`，停表区间追加到`clock_pauses`并写入审计详情。
- `withdraw_evidence`（主管）：必须填写`withdraw_reason`，案件回到`submitted`并恢复原`deadline_day`，按撤回日重算剩余天数，之后可继续办理。
- 记录详情（`GET /api/records/{id}`）展示`pause_clock`、`paused_remaining_days`、`evidence_due_day`、`clock_pauses`等字段；审计时间线（`GET /api/records/{id}/audit`）在`changes`/`previous`中展示停表前后值、补件截止日和撤回原因。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
