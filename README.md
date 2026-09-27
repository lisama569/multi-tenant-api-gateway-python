# 多租户请求治理网关（Python / FastAPI）

在最小 FastAPI 服务之上实现一套与接口层解耦的多租户请求治理能力：

1. **租户识别与权限范围校验**（凭据、scope、跨租户边界）
2. **按租户的配额与突发限流**（固定窗口 + 1 秒滑动突发，原子判定）
3. **写操作幂等与审计追溯**（租户命名空间幂等键、内容冲突检测、结构化审计）

治理逻辑全部位于 `app/governance.py`，不依赖 Web 框架；接口层
（`app/routes.py`）只做编排，策略可按租户数据演进、可替换存储实现。

---

## 1. 租户模型

租户由 `app/governance.py::Tenant` 描述，示例目录见 `app/tenants.py`：

| 租户 ID    | API Key       | 权限范围                 | 配额        | 突发     |
|------------|---------------|--------------------------|-------------|----------|
| tenant-a   | `key-a-123`   | `items:read`, `items:write` | 5 次 / 60 秒 | 5 次/秒  |
| tenant-b   | `key-b-456`   | `items:read`, `items:write` | 3 次 / 60 秒 | 3 次/秒  |
| tenant-ro  | `key-ro-789`  | `items:read`（只读）     | 10 次 / 60 秒 | 10 次/秒 |

- 凭据通过请求头 `X-API-Key` 传递；系统按 key 反查租户。
- 每个资源都带 `tenant_id` 归属，**列表只返回本租户资源**，单资源访问会做
  归属比对；任何租户都无法读取或影响其他租户的数据。
- 幂等键按 `(租户 ID, Idempotency-Key)` 命名空间隔离，跨租户不互相可见。

## 2. 接口

| 方法 & 路径            | 所需 scope     | 幂等 | 说明                     |
|------------------------|----------------|------|--------------------------|
| `GET  /`               | 无             | 否   | 既有骨架行为，保持兼容   |
| `GET  /api/items`      | `items:read`   | 否   | 列出本租户资源           |
| `POST /api/items`      | `items:write`  | 可选 | 创建；`Idempotency-Key` 头开启幂等 |
| `GET  /api/items/{id}` | `items:read`   | 否   | 获取单个资源，含租户边界校验 |

接口层的判定顺序固定：

```
凭据识别 → 权限范围 → 配额/突发限流 → 幂等区段 → 业务副作用
```

任何拒绝都发生在副作用之前。

### 请求示例

```bash
# 创建（带幂等键）
curl -s -X POST http://127.0.0.1:8000/api/items \
  -H 'X-API-Key: key-a-123' \
  -H 'Idempotency-Key: order-2026-001' \
  -H 'Content-Type: application/json' \
  -d '{"name":"demo","data":"v1"}'

# 重放（相同键+相同内容 → 返回首次结果，不产生新副作用）
curl -X POST http://127.0.0.1:8000/api/items \
  -H 'X-API-Key: key-a-123' -H 'Idempotency-Key: order-2026-001' \
  -H 'Content-Type: application/json' -d '{"name":"demo","data":"v1"}'
```

## 3. 配额与限流语义

- **固定窗口配额**：窗口从该租户窗口内首个请求开始计时，长度为
  `window_seconds`。窗口内计数达到 `quota_limit` 后，后续请求被拒绝；
  到达窗口边界（`now >= window_start + window_seconds`）时计数整体重置。
  边界行为由测试锁定：`t+59s` 拒绝、`t+60s` 放行且剩余额度正确。
- **突发限流**：额外维护一个 1 秒滑动窗口，每秒最多 `burst_limit` 个请求。
- **原子性**：所有“检查—拒绝/变更计数”都在引擎的同一把锁内完成，因此
  并发突发不会出现计数漂移或超额放行；被拒请求不消耗任何额度。
- 配额先于突发判定，拒绝原因稳定可解释。
- 被接纳的请求通过响应头回报额度：
  - `X-Quota-Remaining`：窗口剩余配额
  - `X-Quota-Reset-At`：窗口重置时间（UTC ISO-8601）
  - `X-RateLimit-Remaining`：当前突发窗口剩余次数
- 被拒绝的请求在错误体 `details` 中给出 `quota_used / quota_limit /
  quota_remaining / reset_at / retry_after_seconds`，并带 `Retry-After` 头。

## 4. 幂等语义

- 写请求携带 `Idempotency-Key` 头即开启幂等；不携带则不去重。
- 请求内容用规范化 JSON 的 SHA-256 指纹标识：
  - **同键 + 同内容**：首次 `201`，之后重放返回首次响应（`200`，
    响应头 `X-Idempotent-Replay: true`），**不产生重复副作用**。
  - **同键 + 不同内容**：`409 idempotency_conflict`，明确拒绝，不执行。
  - **首请求仍在处理中并发重放**：`409 replay_in_progress`，带
    `retry_after_seconds`，可稍后重试。
- 占位与登记在同一把锁内完成，并发重复提交最多只有一个产生副作用。

## 5. 审计

每次治理判定都写入 `AuditEntry`（`app/audit.py`）：

```json
{
  "timestamp": "...UTC ISO...",
  "tenant_id": "tenant-b",
  "action": "tenant_boundary",
  "decision": "deny",
  "reason": "cross_tenant_access",
  "request_summary": {"resource_tenant_id": "tenant-a"}
}
```

`InMemoryAuditSink` 为线程安全内存存储（deque，默认 1024 条），同时输出
结构化 JSON 日志（logger 名 `governance.audit`）；通过 `AuditSink` 协议
可无侵入替换为持久化实现。凭据只记录 SHA-256 指纹前缀，不落完整密钥。

## 6. 错误分类

统一响应体：`{"error": {"code", "message", "tenant_id"? , "details"?}}`

| code                     | HTTP | 含义 / 可区分依据                         |
|--------------------------|------|-------------------------------------------|
| `missing_credentials`    | 401  | 未提供 `X-API-Key`（含空值）              |
| `invalid_credentials`    | 401  | key 不存在或已吊销                        |
| `insufficient_scope`     | 403  | 已认证但缺少所需 scope（details 给出所需/持有 scope） |
| `cross_tenant_access`    | 403  | 访问归属其他租户的资源（details 给出双方租户 ID） |
| `quota_exceeded`         | 429  | 固定窗口配额耗尽（含剩余额度与重试时机，`Retry-After`） |
| `rate_limited`           | 429  | 超过每秒突发上限（含重试时机）            |
| `idempotency_conflict`   | 409  | 幂等键被复用于不同请求内容                |
| `replay_in_progress`     | 409  | 相同幂等请求仍在处理中（并发重放）        |
| `not_found`              | 404  | 资源不存在                                |
| `validation_error`       | 422  | 请求体校验失败                            |

> 注意 401 的两类错误（缺少 / 无效）、403 的两类错误（权限不足 / 跨租户）
> 与 409 的两类幂等错误 code 均互不相同，调用方可以精确区分处理。

## 7. 本地验证

```bash
# 运行服务（默认 127.0.0.1，端口 0 = 由系统分配；可自行改端口）
uv run python main.py
# 或
uvicorn main:app --reload

# 运行全部测试（23 个）：覆盖越权、窗口边界、并发竞争、幂等重放与冲突
uv run pytest
# 查看请求输入与判定依据的详细日志
uv run pytest -s
# 交互式 API 文档
# 打开 http://127.0.0.1:8000/docs
```

测试文件：

- `tests/test_authz.py`：凭据缺失/无效、权限不足、跨租户越权、租户隔离
- `tests/test_quota.py`：固定窗口边界、被拒不计费、配额头、租户配额隔离、
  并发突发与并发配额消耗不超额
- `tests/test_idempotency.py`：重放无重复副作用、键内容冲突、无键不去重、
  键的租户隔离、并发重复提交

测试通过可注入的 `FakeClock` 对窗口边界做确定性断言，并用线程池构造真实
并发竞争；每个用例都会打印请求输入与审计判定轨迹。
