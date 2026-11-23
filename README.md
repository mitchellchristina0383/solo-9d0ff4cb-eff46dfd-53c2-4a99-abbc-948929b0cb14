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
```

## 使用流程

1. `POST /sessions` — 登记有序提交列表，返回 `session_id`。
2. `POST /sessions/{id}/claims` — 执行器领测（body: `{"executor": "..."}`）。
   返回要测的提交；`commit` 为 null 时看 `reason`：
   `waiting_for_reports`（范围内待测提交已派完，等已领回报）、
   `completed`、`contradiction`。
3. `POST /sessions/{id}/reports` — 回报 `pass` / `fail` / `build_failure`。
4. `GET /sessions/{id}` — 嫌疑范围、各提交状态、在途领取、矛盾、结论。
5. `GET /sessions/{id}/result` — 只看结论。

## 实现要点

- **核心逻辑**（`app/bisect.py`，纯 Python、无线框架依赖）+ **API 层**
  （`app/main.py`，FastAPI）。会话状态保存在内存，每个会话一把锁，
  领取是原子操作。
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

- `pytest tests -q`：**12 passed**。覆盖：完整二分定位、两台执行器并行
  领测且每条只派一次（含 8 线程压测核心类）、回报乱序（较晚提交先回报、
  较早提交落出范围后仍被接受核对）、构建失败（边界不动、留作嫌疑、结论
  给出剩余嫌疑）、矛盾（列出两条提交、停止派测、矛盾前已领回报仍受理）、
  完成等待在途回报、输入校验（未领取即回报 409、重复回报 409、未知会话
  /提交 404、重复或过短提交列表 422）。
- 实机冒烟（`uvicorn app.main:app`）：`GET /docs` 与 `/openapi.json`
  均 200；`scripts/demo.sh` 两台执行器并行、含一次构建失败，正确定位
  `c9`；curl 复现矛盾场景，返回矛盾两条并停止派测。
- `docker-compose.yml` 已做结构校验；**本环境无 Docker 守护进程，
  `docker compose up` 未实际执行**，镜像构建步骤为标准
  `pip install -r requirements.txt` + `uvicorn`，与本地冒烟所用命令一致。

## 余项

- 会话存内存，重启即失；多副本部署需换共享存储（如 Redis/Postgres）。
- 无认证鉴权，执行器标识为自报字符串。
- 并发执行器数量未设上限（需求为两台，实现不限制）。
- 矛盾检测以首个矛盾对为准，后续矛盾对不展开列举。
- 派测策略为中点优先；对构建失败率高的历史可换更复杂的选点策略。
