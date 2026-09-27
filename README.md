# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限、材料完整性、冲突检查和补件停表（tolling）规则。
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

补件要求发出后案件原期限默认继续计算；传入`pause_clock: true`可在补件期间停表：

- `request_evidence`（case_officer）：登记`evidence_request_day`（请求日）和`allowed_days`（补件天数），`pause_clock`为真时冻结剩余天数`deadline_day - 请求日`，`overdue=false`，等待期间不计逾期。已有待回应补件时不能重复发起（状态机保证）。请求日晚于原期限时不允许停表。
- `respond`（legal_rep）：回应日不得早于请求日、晚于补件截止日；停表案件按冻结剩余天数重算新期限`response_day + 剩余天数`，并把停表区间（请求日、停表开始、补件截止日、恢复日、停前/恢复后期限）写入`payload.clock_stoppages`和审计详情。
- `withdraw_evidence`（supervisor）：撤回补件要求必须提供`withdraw_reason`，案件退回`submitted`并恢复原期限（若恢复时已超过原期限则标记逾期），恢复原因写入停表区间和审计时间线；撤回后可重新发起补件。

详情接口返回`clock_paused`、`deadline_day_before_pause`、`remaining_days_at_pause`、`evidence_due_day`、`clock_stoppages`等字段；审计时间线在`details.clock`中展示停表前后期限、补件截止日和恢复原因。演示页面支持查看和操作上述流程。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
