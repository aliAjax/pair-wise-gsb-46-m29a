# 急救车调度与目的地分流

纯Python标准库实现的急救车调度与目的地分流原型，使用SQLite持久化，HTTP接口由`http.server`提供。
支持**调度员席位认领**与**换班交接**：同一时刻一单只归一人，交接确认前后权限自动切换，服务重启后可接着办。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、优先级评分、能力匹配、车辆冲突和冲突检查。
- `src/repository.py`：SQLite建表、原子认领/交接事务和查询。
- `src/service.py`：用例编排、席位认领、换班交接、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：席位与换班交接演示页面。
- `tests/`：完整流程、规则计算、失败场景、席位交接与持久化测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8322
```

默认端口为`8322`，默认数据库位于项目目录。服务启动时自动建表（旧库自动补齐席位字段）。

## 身份头

除`/health`和`/`外，请求需提供：

- `X-User-Id`：账号（调度员工号，如`d-old`）
- `X-Role`：角色，席位相关操作必须为`dispatcher`
- `X-Org`：所属机构（如`east`），认领/交接按机构隔离

## 席位与换班规则

1. **认领**：调度员只能认领**本机构未结束**（非closed/cancelled）且**尚未有人负责**的任务。
2. **同一时刻一单只归一人**：认领是数据库原子操作（`BEGIN IMMEDIATE` + 条件更新），两人同时抢单只有一人成功；认领后只有负责本人能办理，其他人只读。
3. **发起交接**：仅当前负责人可发起，接班人必须是同机构调度员且不能是本人；一单同时只允许一笔待确认交接。
4. **确认前**：接班人不能办理，原调度员**继续负责并可办理**；原负责人可撤销。
5. **确认后**：负责人原子切换为接班人，**旧账号只能查看**（办理返回403并说明现任负责人）。
6. 接班人可**拒绝**（任务仍归原负责人，可重新发起）；所有决定不可重复处理。
7. 负责人、待确认交接、办理经过（含认领、发起、确认、拒绝、撤销）均落审计表并持久化，重启后可继续。

## 主要接口

记录类（原有）：

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情，含`owner_id`和`pending_handover`。
- `GET /api/records/{id}/audit`：办理经过（审计时间线）。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，机构取自`X-Org`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`；调度员须为当前负责人。

席位与交接（新增）：

- `GET /api/tasks?scope=unclaimed|mine|open`：本机构未结束任务：待认领 / 我负责的 / 全部。
- `POST /api/records/{id}/claim`：认领席位。
- `POST /api/records/{id}/handovers`：发起交接，请求体`{"to_user":"d-new","to_org":"east(可选)","note":"夜班交接"}`。
- `GET /api/handovers?direction=incoming|outgoing&status=pending`：待我确认 / 我发起的交接。
- `GET /api/handovers/{id}`：交接单详情（仅交接双方可查）。
- `POST /api/handovers/{id}/accept`：接班人确认（负责人切换）。
- `POST /api/handovers/{id}/decline`：接班人拒绝，请求体`{"reason":"..."}`。
- `POST /api/handovers/{id}/revoke`：原负责人撤销。

错误统一为`{"error":"...","message":"具体原因"}`：重复认领、跨机构认领/交接、负责人不符、非指定接班人、重复处理等都会给出明确中文原因（403/409/422）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、并发认领、重复/跨机构认领、交接确认前后权限切换、拒绝/撤销、重复决定以及重启持久化和旧库迁移。
