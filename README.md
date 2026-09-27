# 急救车调度与目的地分流

纯Python标准库实现的急救车调度与目的地分流原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、优先级评分、能力匹配、车辆冲突和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8322
```

默认端口为`8322`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数；每条记录含`owner_id`/`owner_org`与`pending_handover`。
- `GET /api/records/{id}`：记录详情（含负责人与待确认交接）。
- `GET /api/records/{id}/audit`：审计时间线（办理经过）。
- `GET /api/records/{id}/handovers`：该任务的全部交接记录。
- `GET /api/handovers/pending`：与当前账号相关（发起或接班）的待确认交接。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，机构取`X-Org`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/claim`：调度员认领本机构未结束任务（席位）。
- `POST /api/handovers`：负责人发起换班交接，请求体为`{"record_id":1,"to_user":"bob"}`。
- `POST /api/records/{id}/handovers/{hid}/confirm`：接班人确认接管。
- `POST /api/records/{id}/handovers/cancel`：确认前原负责人撤销交接。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 席位认领与换班交接规则

- 调度员先认领本机构未结束任务；同一时刻一单只归一人，认领带数据库条件更新，
  并发抢占时只有一人成功，其余收到`conflict`（HTTP 409）。
- 重复认领、跨机构认领、任务已结束均返回409并说明原因。
- 机构内的调度员写操作（派车、出发、取消）必须是当前负责人；未认领、非负责人返回403并说明原因。
  急救员、院方等现场角色不受席位限制；`admin`可代管。
- 发起交接后、接班人确认前，负责人不变，原调度员继续办理；重复发起被拒绝。
- 接班人确认后负责人切换，旧账号再写操作返回403「只能查看」，GET查看不受影响。
- 任务结束（closed/cancelled）时待确认交接自动作废。
- 负责人、交接记录、办理经过全部持久化在SQLite，服务重启后可继续确认与办理。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及席位重复/跨机构认领、
交接确认前后权限、并发抢占唯一赢家和重启后续办。
