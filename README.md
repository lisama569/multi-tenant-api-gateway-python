# 多租户请求治理网关（Python / FastAPI）

在原有 FastAPI 服务基础上，实现面向多租户调用的请求治理：
**租户识别与边界、配额限流、写操作幂等、审计追溯**。
治理逻辑独立于接口层（`app/governance.py`），可按租户策略独立演进。

## 1. 租户模型

- 凭据通过请求头 `X-API-Key` 提供，一把凭据唯一归属一个租户。
- 每个租户有自己的权限范围（scope），如 `items:read`、`items:write`、
  `quota:read`、`audit:read`。
- 所有数据路径都带租户段 `/tenants/{tenant_id}/...`；
  治理层会校验“凭据归属租户 == 路径租户”，任何租户都无法读取或影响
  其他租户的数据。对不存在的租户统一按越权处理，不暴露其存在性。
- 幂等键空间按租户隔离：`(tenant_id, idempotency_key)` 才是唯一键。
- 种子数据见 `app/api/deps.py`：

  | 凭据 | 租户 | 权限 | 配额 |
  |---|---|---|---|
  | `ak-tenant-a` | tenant-a | items:read/write | 3 次 / 60 秒 |
  | `ak-tenant-b` | tenant-b | items:read | 2 次 / 60 秒 |
  | `ak-tenant-c` | tenant-c | items:read/write、quota:read、audit:read | 100 次 / 60 秒 |

## 2. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 公开接口，保持既有行为 `{"Hello": "World"}`，不鉴权、不占配额 |
| GET | `/tenants/{tid}/items` | 读取本租户条目（需要 `items:read`，消耗 1 配额） |
| POST | `/tenants/{tid}/items` | 创建条目（需要 `items:write`，消耗 1 配额） |
| GET | `/tenants/{tid}/quota` | 查看本租户配额快照（需要 `quota:read`，**不**消耗配额） |
| GET | `/tenants/{tid}/audit` | 查看本租户相关审计记录（需要 `audit:read`） |

POST 支持可选的 `Idempotency-Key` 请求头：
- 同一 `(租户, 键)` + 相同请求内容：第一次执行真实副作用，后续请求直接
  返回首次响应（响应体带 `"replayed": true`），不重复写数据、不再扣配额；
- 同一键被不同请求内容复用：返回 `409 idempotency_conflict`，
  details 中同时给出存储指纹与本次指纹；
- 首次请求业务失败时会释放该键，允许同内容重试。

## 3. 配额与限流语义

采用**按租户独立的固定窗口**：

- 窗口为 `[started_at, started_at + window_seconds)`；
- 只有被**接受**的请求才计数；被拒绝的请求不计数（也不产生副作用）；
  幂等重放不计数；
- “检查额度 → 计数 → 生成快照”在同一把锁内原子完成，
  突发并发下不会出现计数漂移或超额放行（有 20 并发抢 2 名额的测试）；
- 到达窗口右边界（`now >= started_at + window_seconds`）立即开新窗口、
  计数归零；边界之前的最后一刻仍受旧窗口约束；
- 被拒绝时返回 `429 quota_exceeded`，details 说明：
  `limit / used / remaining / window_resets_at / retry_after_seconds`。

## 4. 错误分类

所有治理错误返回统一结构：

```json
{"error": {"code": "...", "message": "...", "details": {...}}}
```

| code | HTTP | 触发条件 |
|---|---|---|
| `missing_credential` | 401 | 未提供 X-API-Key |
| `invalid_credential` | 401 | 凭据不存在/已吊销 |
| `cross_tenant_access` | 403 | 凭据租户与路径租户不一致（含目标租户不存在） |
| `insufficient_scope` | 403 | 凭据缺少所需 scope，details 给出 required/granted |
| `quota_exceeded` | 429 | 窗口额度用尽，details 给出剩余额度与可重试秒数 |
| `idempotency_conflict` | 409 | 幂等键被不同请求内容复用 |
| `idempotency_in_flight` | 409 | 相同键请求仍在处理中（等待超时） |
| `invalid_request` | 400 | 请求体校验失败 |
| `internal_error` | 500 | 未预期内部错误 |

## 5. 审计

- 每次经过治理层的请求都会写一条审计：租户、凭据片段（脱敏）、
  方法、路径、目标租户、幂等键、判定结论（`allow/deny/replay`）、
  原因码（如 `quota_exceeded`、`idempotent_replay`）与关键细节。
- 默认每条记录以一行 JSON 输出到 stderr（前缀 `AUDIT`），同时保留在
  内存中，可经 `/tenants/{tid}/audit` 查询。
- 查询按租户隔离：租户能看到自己的全部判定，以及针对自己的越权拒绝。

## 6. 本地验证

```bash
# 安装依赖（含 dev 组的 pytest/httpx）
uv sync --group dev

# 运行全部测试（21 个），-s 可查看请求输入与判定依据日志
.venv/bin/python -m pytest -s

# 启动服务
.venv/bin/python main.py            # 或 uv run uvicorn app.main:app
```

手动验证示例：

```bash
# 无凭据 -> 401 missing_credential
curl -s localhost:8000/tenants/tenant-a/items

# 跨租户 -> 403 cross_tenant_access
curl -s -H 'X-API-Key: ak-tenant-b' \
  localhost:8000/tenants/tenant-a/items

# 幂等：第二次提交重放首次结果，不产生第二条数据
for i in 1 2; do
  curl -s -X POST -H 'X-API-Key: ak-tenant-a' \
       -H 'Idempotency-Key: order-1' -H 'Content-Type: application/json' \
       -d '{"name":"demo"}' localhost:8000/tenants/tenant-a/items
done

# 第 4 个请求 -> 429 quota_exceeded，body 含 remaining 与 retry_after_seconds
```

## 7. 代码结构

```
app/
  errors.py        # 错误码、统一异常与错误体
  tenants.py       # 租户/凭据模型、认证、租户边界与 scope 授权
  quota.py         # 固定窗口配额：原子检查+消耗、可注入时钟
  idempotency.py   # 幂等登记/重放/冲突/进行中等待
  audit.py         # 线程安全审计日志
  governance.py    # 治理门面：编排认证授权→幂等→配额→审计（与接口解耦）
  api/deps.py      # 种子数据与依赖注入
  api/routes.py    # 薄路由层
  main.py          # 应用组装与异常处理
tests/             # 越权、窗口边界、并发竞争、幂等重放/冲突、审计
```
