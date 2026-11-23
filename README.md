# CI Bisect Service

用二分定位引入 CI 故障的提交。测试负责人提交同一分支按顺序排列的提交号
（首 = 已知通过，尾 = 已知复现），两台执行器并行领测，服务随回报收缩嫌疑
范围，最终给出引入故障的提交号或剩余嫌疑。

## 运行

```bash
docker compose up --build        # 服务起在 http://localhost:8000
# Swagger UI（试领测/回报/查结果）: http://localhost:8000/docs
docker compose --profile test run --rm tests   # 在容器内跑测试
```

本地（无 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --port 8000
pytest tests -q
bash scripts/demo.sh             # 对运行中的服务做端到端演示
bash scripts/demo_retry.sh       # 领测丢失后的请求号重试演示（见下）
```

## 使用流程

1. `POST /sessions` — 登记有序提交列表，返回 `session_id`。
2. `POST /sessions/{id}/claims` — 执行器领测，body：
   `{"executor": "...", "request_id": "可选请求号"}`。
   返回要测的提交；`commit` 为 null 时看 `reason`：
   `waiting_for_reports`（范围内待测提交已派完，等已领回报）、
   `completed`、`contradiction`。
   - **`request_id` 可选**：带上后 `(executor, request_id)` 成为幂等键，
     用于领测响应丢失时的重试（语义见下）。不带时保持老行为，每次调用
     都是一次新领测，老客户端无需改造。
3. `POST /sessions/{id}/reports` — 回报 `pass` / `fail` / `build_failure`。
4. `GET /sessions/{id}` — 嫌疑范围、各提交状态、在途领取（含各自的
   `request_id`）、矛盾、结论。
5. `GET /sessions/{id}/result` — 只看结论。

## 领测重试（请求号幂等）

问题背景：执行器发出领测后若没收到响应，重发请求可能领到另一条提交，
而它仍在等第一次领测的回报，会话随之挂死。修复后执行器为每次领测自选
一个请求号（字符串），规则如下：

- **同号重试、原提交仍待回报**：返回原提交，响应带 `replayed: true`，
  在途名单不新增条目。即使该提交已落出嫌疑范围、或会话已因矛盾停派/
  完成，也一样找回原提交。
- **同号并发领测**：同一会话、同一执行器、同一请求号的并发调用只派一次，
  其余调用拿到同一条提交的回放（会话锁串行化）。
- **原提交已回报**：重试返回 `reason: "already_reported"`，并在 `detail`
  中指明是哪条提交、结果是什么；在途名单保持不变。
- **此前没有派出过提交**（首调拿到的就是无派测应答）：无派测结果不入
  幂等台账，同号重试按当前派测状态重新判断（例如先 `waiting_for_reports`
  后 `completed`）。
- **新请求号**正常领新提交；不同执行器用相同请求号互不影响；不带
  `request_id` 的老格式照旧每次新派。

### 在 /docs 里走一遍

1. `docker compose up --build`，打开 http://localhost:8000/docs。
2. `POST /sessions` 建会话；`POST /sessions/{id}/claims`，请求体填
   `{"executor": "executor-1", "request_id": "req-007"}`，记下返回的
   `commit`（`replayed` 为 false）。
3. 再次点 Execute 发**同样的请求体**：拿回同一个 `commit`，
   `replayed: true`，`GET /sessions/{id}` 的 `in_flight` 仍只有一条。
4. 用该 `commit` 调 `POST /sessions/{id}/reports` 回报（如 `fail`）。
5. 第三次用 `req-007` 领测：返回 `reason: "already_reported"`，
   `detail` 指明该提交已回报；在途名单不变。
6. 换一个 `request_id`（如 `req-008`）可正常领到新提交。
7. `GET /sessions/{id}/result` 查看结案结论。

脚本化的完整演示（含并发重发、无派测不缓存）：
`bash scripts/demo_retry.sh`。

## 实现要点

- **核心逻辑**（`app/bisect.py`，纯 Python、无线框架依赖）+ **API 层**
  （`app/main.py`，FastAPI）。会话状态保存在内存，每个会话一把锁，
  领取是原子操作。
- **幂等领测**：`BisectSession` 内维护 `claim_keys` 台账，键为
  `(executor, request_id)`，只记录真正派出的提交下标。领测入口先查台账：
  命中且原提交仍 `claimed` 就回放原提交（不写在途名单）；已回报则返回
  `already_reported`；未命中才走原派测逻辑。无派测应答不建台账记录，
  同号重试实时重判。所有判断都在同一把会话锁内完成，天然防并发重复派测。
- **嫌疑范围** = `(known_good, known_bad]`：通过上移下界，复现下移上界。
  领测只在范围内、且从未派过的提交中选取（取离中点最近者，两台执行器
  自然错开）；每条提交全程只派一次。
- **构建失败**：不移动边界，提交留在范围内作为嫌疑；范围无法继续收缩时
  进入结论，剩余嫌疑 = 范围内构建失败的提交 + 首个已知复现提交。
- **范围外回报照收**：已派出的测试即使后来落出范围，回报仍被接受、核对
  并照常参与边界更新与矛盾检测。
- **矛盾**：某较早提交复现而较晚提交通过（`known_good` 越过
  `known_bad`）时，会话置为 `contradiction`，记录相矛盾的两条提交并
  停止派测；矛盾前已领的测试仍可回报。
- **完成条件**：范围内没有可再缩小范围的待测提交，且所有已领测试均已
  回报。结论为唯一嫌疑（`faulty_commit`）或剩余嫌疑列表（`suspects`，
  此时 `faulty_commit` 为 null）。

## 验证结果

- `pytest tests -q`：**29 passed**（原 12 个 + 新增 17 个，见
  `tests/test_idempotent_claims.py`）。新增覆盖：同号重放返回原提交、
  16 线程同号并发只派一次（核心类）与 12 线程同号 HTTP 并发只派一次、
  原提交落出嫌疑范围后重试仍找回且在途唯一、矛盾停派后新号停派而旧号
  仍回放在途提交、回报后同号重试返回 `already_reported` 且在途名单不变
  （含 build_failure 与他人在途并存两种情形）、无派测应答不缓存
  （`completed` 与先 `waiting_for_reports` 后 `completed` 两种）、新请求号
  正常领新、同号不同执行器互不影响、老格式（无 request_id）行为不变、
  空 `request_id` 返回 422。并发用例已连跑多轮无 flaky。
- 实机冒烟（`python -m uvicorn app.main:app`）：`GET /docs` 与
  `/openapi.json` 均 200，OpenAPI 版本 1.1.0、含 `request_id`/`replayed`
  字段与 `already_reported` 原因说明。
- `bash scripts/demo_retry.sh` 全流程通过：同号重发拿回同一提交、4 个
  并发重发在途仍只有一条、回报后重试得到 `already_reported`、无派测号
  重试随状态从 `waiting_for_reports` 变为 `completed`、最终
  `/result` 正确结案；`bash scripts/demo.sh`（老客户端路径）仍正确
  定位 `c9`。另以独立 urllib 脚本（`python -I` 运行）验证落出范围、
  矛盾停派、空请求号 422 等 HTTP 边界场景，全部通过。
- `docker-compose.yml` 通过 YAML 结构校验并新增健康检查；**本环境无
  Docker 守护进程，`docker compose up` 未实际执行**，镜像构建步骤为
  标准 `pip install -r requirements.txt` + `uvicorn`，与本地冒烟所用
  命令一致。

## 余项

- 会话存内存，重启即失；多副本部署需换共享存储（如 Redis/Postgres），
  幂等台账 `claim_keys` 同样需要随之持久化。
- 无认证鉴权，执行器标识为自报字符串；幂等键的信任范围因此也是
  `(executor, request_id)`，恶意方知道执行器名和请求号即可重放查询。
- 并发执行器数量未设上限（需求为两台，实现不限制）。
- 矛盾检测以首个矛盾对为准，后续矛盾对不展开列举。
- 派测策略为中点优先；对构建失败率高的历史可换更复杂的选点策略。
- 幂等台账随会话终身保留、不做淘汰；长会话、海量请求号时会持续占内存，
  后续可加 TTL/容量上限。
