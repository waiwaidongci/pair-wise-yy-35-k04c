# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`（含按时间排列的`corrections`更正历史）
- `POST /api/items/{id}/records`
- `GET /api/items/{id}/corrections`
- `POST /api/items/{id}/corrections`，剂量师提交`{"new_quantity": ..., "reason": ...}`
- `POST /api/items/{id}/transition`，必须提交`expected_version`；关闭曾升级的事件时健康物理师必须提交`closure_note`
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

### 剂量更正规则

- 仪器复测或录入纠错由**剂量师**提交更正（`new_quantity`新读数 + `reason`说明）；事件关闭后不能再更正。
- 原始读数保留在`quantity`/`original_quantity`，每次更正（`previous_quantity`→`new_quantity`、说明、剂量师、UTC时间）追加在`dose_corrections`中，详情和审计链全程可见。
- 列表与详情的`current_quantity`取最近一次更正值；`priority`、`deadline_hours`、`escalation_required`一律按当前读数与阈值之比重新计算。
- 更正不改变状态、不回退流程（状态机只进不退）。一旦曾触发升级，`escalation_locked`永久置位；即使更正后比值降回线下，关闭时仍必须由健康物理师填写`closure_note`在关闭说明中处置，说明随关闭审计事件留痕。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
