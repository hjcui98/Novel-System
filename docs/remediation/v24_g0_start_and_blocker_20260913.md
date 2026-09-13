# v24 G0 启动记录与阻塞点（2026-09-13）

对应指导：`docs/yujin_jiuxu_next_stage_execution_guidance_20260913.md` 第 12.1 节（整合与冻结）、
第 12.2 节（八卷状态变化）、第 12.3 节（各阶段出口）。

## 1. 本轮到达的位置

G0 已完成**身份选择、现场冻结、preflight 与 Genesis prepare**，
在"检索装配后的运行描述符冻结"这一步被环境条件阻断。

| 步骤 | 结果 |
|---|---|
| v24 身份可用性 | 已核对：`project.yujin-jiuxu.v24` 无 project 行、无 run 任务 |
| v24 工作区 | `/home/cuihengjia/agent/novel/NS/yujin-jiuxu-v24`，独立 `objects/`、`state/`、`logs/`、`receipts/`、`output/`、`evidence/` |
| 作者输入 | `brief.md` 与 v23 逐字相同；`planning-locks.json` 仅改 `project_id`（见第 3 节） |
| 冻结配置 | `state/frozen-configuration.json`：源码 SHA、branch、dirty、PYTHONPATH、30 个 prompt 指纹、38 个 skill 指纹、10 个 migration、manifest/brief/locks 哈希、profile 与检索 URL |
| 源码 SHA | `8e4b06341838…`，工作区**干净**（dirty=false） |
| preflight | 通过：postgres/opensearch/minio/8003/embedding/reranker 端口、profile 模型列表、集群健康、`doctor` |
| Genesis prepare | **成功**：2 次真实模型调用，输出 `validation_status=passed`，16 个 plan node、46 个 world entity、45 个 world state、8 条 `unresolved_world`、3 条 `demoted_future_states` |

Genesis prepare 的输出本身值得记录：它把"长程真相属于后期主线"写进了
`plan.bootstrap.005`，并把 6 条后续卷次内容降级为 `unresolved_world`，
与作者锁的 350 / 401 边界一致。

## 2. 阻塞点（环境，非代码）

`20_bootstrap.sh` 在 Genesis commit 之后的检索装配步骤失败：

```
src/novel_agent/runtime/real_hybrid.py:112  native_models.assert_model_service(embedding_model)
scripts/native_models.py:487                NativeInfraError:
    embedding model process identity does not match its PID record
```

### 2.1 核对结果

| 项 | 值 |
|---|---|
| 8081 health payload | `status=ok, kind=embedding, model=BAAI/bge-m3, revision=5617a9f6…, profile=…;device=cpu;dtype=float32;…` — **与锁文件完全一致** |
| 8082 health payload | `status=ok, kind=reranker, model=BAAI/bge-reranker-v2-m3, revision=953dc6f6…` — **与锁文件完全一致** |
| `tmp/native-models/run/embedding.json` | `pid=150547`，记录时间 **8月31日** |
| `tmp/native-models/run/reranker.json` | `pid=806551`，记录时间 **8月18日** |
| v23 bootstrap | **18:03 成功**（日志含 "recorded retrieval deployment real_hybrid"），说明当时检查可通过 |

即：服务本身健康且身份正确，**只有 PID 记录过期**。

### 2.2 根因

DSH 的 bash 工具把每次调用放进独立 PID 命名空间：

```
$ ps -p 1 -o pid,comm
      1 bwrap
$ ps -eo pid,comm --no-headers
      1 bwrap
      2 bash
```

因此：

- 8081/8082 上的检索服务在本会话里**不可见**，也无法发信号；
- 本会话内启停"替换服务"有两个后果：
  - 在同一端口启动会被"端口被未知进程占用"拒绝（因为原服务占用且我无法停止它）；
  - 换到空闲端口可以启动，但**进程随该次 bash 调用结束而消失**。
    实测：在 8281 上成功启动过 embedding 服务并写下有效 PID 记录
    （`pid=6, port=8281`，`assert_model_service` 当时通过），该次调用结束后
    8281 已无监听、pid 6 不可见。这正说明进程无法跨 bash 调用存活。

结论：**任何依赖"读取运行中服务 PID/start_time/完整命令"的生产检查，
在本工具的沙箱内都无法满足。** 这不是代码缺陷——v23 于 18:03 在同一代码上通过了同一步。

### 2.3 未做的处置

- **没有**保留任何沙箱内替换服务（已核对 8281/8382 无监听）；
- **没有**留下伪造的 PID 记录：原先被移开的两个记录已按原字节恢复
  （`cmp` 逐字节相同），`make models-health` 仍报同一条错误；
- **没有**放宽 `assert_model_service` 这个 fail-closed 身份检查；
- **没有**改动 `.env`、`compose.yaml` 或其他工作区。

诊断期间把两个原始记录另存了一份到
`tmp/native-models/evidence-20260913/*.foreign-record.json`（内容与恢复后的记录相同）。
这个目录属于运行产物、受 `.gitignore` 约束，可随时删除。

## 3. 作者锁的改写与来源证明

v24 复用 v23 的作者输入，但锁文档自身携带 `project_id`，与该 run 不一致时
`bootstrap-prepare` 会正确拒绝（第一次启动即报
"author planning locks belong to another project: project.yujin-jiuxu.v23"）。

因此只改了 `project_id` 一个字段，并删除 `root_hash` 让加载器按内容重新推导：

| 项 | 值 |
|---|---|
| 源 | `yujin-jiuxu-v23/input/planning-locks.json`，sha256 `237eb336a3bf1cefce488075…` |
| 目标 | `yujin-jiuxu-v24/input/planning-locks.json`，sha256 `8d4652bfdecc8440c39016a…` |
| 改写范围 | **仅 `project_id`**；`locks`、`schema_version`、`story_title` 逐字相同（代码内断言） |
| 六条锁边界 | 101 / (90,100) / 101 / (201,300) / 350 / 401 —— 与 v23 完全一致 |

来源证明：`yujin-jiuxu-v24/state/planning-locks-provenance.json`。
**没有放宽任何作者锁**：六条锁的 ID 与全部时间边界逐条核对未变。

## 4. 如何解除阻塞

任一即可（都需要一个能看见宿主机进程的上下文）：

1. 在该上下文中执行 `bash yujin-jiuxu-v24/commands/20_bootstrap.sh`，
   使其中的检索装配能读到 8081/8082 服务的真实 PID 记录；
2. 或在该上下文中先 `make models-down && make models-up`，让 PID 记录与运行进程重新一致
   （注意 `models-down` 只对"PID 记录精确匹配的本用户进程"发信号，
   当前过期的记录无法匹配，所以这一步同样需要在能看见这些进程的上下文里做）。

两者都需要**在沙箱之外**执行；沙箱内无法完成第 2 步（见 2.2）。

## 5. 解除后可直接续跑的命令

```bash
cd /home/cuihengjia/agent/novel/NS/yujin-jiuxu-v24
bash commands/20_bootstrap.sh          # genesis prepare + commit + 描述符冻结
bash commands/13_start_run.sh          # 创建初始任务，不派发模型请求
bash commands/60_run_stage.sh g0 60    # 受版本管理的 G0 驱动（证据化停止点）
```

`60_run_stage.sh` 已复制到 v24 的 `commands/`，与
`.worktrees/yujin-unified-remediation/scripts/60_run_stage.sh` 同一份实现
（提交 `90f1533`），可定位 SHA、退出码真实、按已提交卷/章数证明阶段出口。

## 6. 本轮的确定性证据

- 新增 `tests/model/test_yujin_d0_real_review.py::test_d0_the_real_reviewer_emits_findings_the_host_can_act_on`
  （提交 `8e4b063`）：记录真实审校对冻结候选的判定结构——
  `decision=accept`、阻断意见 0 条、可执行意见 0 条、`verification_failures` 为空。
  这条结果回答的是 G0 是否可能收敛：ARC_VOLUME 路径上审校会给出**可结算判定**，
  而不是 v23 那种"无法核验的自由文本要求"。
- 全量确定性套件：**74 failed / 3360 passed**，与 N0 基线逐节点 diff 只消除
  `test_checked_in_stage2_schemas_match_models` 一项，无新增失败。

## 7. 限制与边界

- **G0 未完成**：没有八卷节点、没有正式义务、没有章节计划提交。
  本轮只完成到 Genesis prepare；Genesis commit 与运行描述符冻结尚未发生。
- 本轮**没有**产生 v24 的规范提交（commit / projection）。
- 上述阻塞是**工具环境**限制，不改变指导中的任何验收门槛；
  也没有以此为由跳过 G0 的任何一步。
