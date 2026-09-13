# v24 G0 启动记录与阻塞点（2026-09-13）

对应指导：`docs/yujin_jiuxu_next_stage_execution_guidance_20260913.md` 第 12.1 节（整合与冻结）、
第 12.2 节（八卷状态变化）、第 12.3 节（各阶段出口）。

> 后续进展见第 9 节：v24 已跑完 G0 的 STORY 层并提交，ARC_VOLUME 层暴露了一个
> 审校器可自填宿主判定字段的真实缺陷，已修复（`70ad5ff`）。

## 1. 本轮到达的位置

G0 已完成**身份选择、现场冻结、preflight、Genesis prepare 与 Genesis commit**，
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

### 1.1 Genesis commit 已真实落库（核对结果）

失败点在 `bootstrap-commit` 内部**很靠后**：`ProductionNovelBootstrap.commit()`
先完成作者批准与 Genesis commit，之后才为 `real_hybrid` 装配检索、再写
policy/request/runs 三个描述符。因此报错时数据库侧已经提交。

用只读查询核对（表名以 `src/novel_agent/adapters/postgres/models.py` 为准，
commit 表是 `project_commit` 而非 `commit`）：

| 表 / 列 | 值 |
|---|---|
| `project.current_commit_id` | `sha256:ae7e86a8304941edb08476ec37727a4349d7efbe55536cb248aa1266f1a7d22e` |
| `project.created_at` | 2026-09-13 13:53:16.697589 GMT |
| `project_commit` 行数 | 1（该项目的 Genesis，`base_commit_id` 为 NULL，manifest 1168 字节） |
| `author_approval` | `bootstrap-approval.55536cb248aa1266f1a7d22e`，`status=approved`，作者 `author.hjcui98`（Genesis commit 内部走 `GenesisCoordinator.commit`，不写 `commit_receipt`，该表为空属正常） |
| `runtime_task_projection` | `run.yujin-jiuxu.v24` 任务数 **0**（未创建任何任务，未派发任何模型调用） |

`project.current_commit_id` 与候选 manifest 哈希相等，即
`state/genesis-preview.json` 里审阅过的那份 Genesis 就是已提交的那份。

**因此精确的停止点是**：Genesis 已提交且持久；`state/` 缺
`policy.json` / `request.json` / `runs.json`，检索部署未记录。

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

两个检查严格区分，不要混用（`scripts/native_models.py::main`）：

| 命令 | 检查内容 | 本沙箱结果 |
|---|---|---|
| `verify` | 只读锁文件与模型目录 | exit **0** |
| `health` | `assert_model_service`：PID/start_time/命令 + health payload | exit **2**，`embedding model process identity does not match its PID record` |
| `status` | 同样按 PID 记录判定 | `"running": false`（两项都是） |

也就是说：**HTTP 健康检查全部通过，身份检查失败**。

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

### 4.0 宿主侧已解除（2026-09-13 22:1x）

宿主 shell 上 `native_models.py health` 返回 **exit=0**（`make models-up` 重建了 PID 记录），
`20_bootstrap.sh` 因此通过了检索装配前的全部检查：preflight 全 ok、
`bootstrap-prepare` 完成 2 次真实模型调用（`prompt_chars=21751 / 19432`，`max_tokens=48000`）。

随后在**同一个脚本的下一行**撞上第二个、属于编排自身的缺陷：

```
File ".../cli.py", line 625, in main
    _write_json_once(
RuntimeError: runtime CLI refuses to overwrite receipt:
    .../yujin-jiuxu-v24/state/genesis-prepared.json
```

`_write_json_once` 是刻意的写一次保护（防止覆盖已冻结的回执），
但 `20_bootstrap.sh` 每次无条件下调 `bootstrap-prepare`，于是
**prepare 成功之后、描述符冻结之前**的任何失败都会让 bootstrap 无法再入：
第二次执行必死在回执守卫上，而绕开它只能删冻结工件或再花两次模型调用重算一份已经正确的文档。
这与 4.1 是同一类缺陷（编排没有为自身的半完成状态留再入路径），只是位置更靠前。

### 4.1 半初始化状态的再入修复（本轮已改）

Genesis commit 与描述符冻结**不在同一个事务**里：先初始化项目，再装配检索，
最后才写 policy/request/runs。旧脚本用三条 `test ! -e` 挡住重复提交，
于是"项目已提交、描述符缺失"这个中间态**无法再入**——重跑被脚本自己拒绝，
只能手工改数据库。这正是本轮 v24 卡住后暴露出来的真实缺陷。

`yujin-jiuxu-v24/commands/12_commit_genesis.sh` 已改为按描述符完整性分支：

| `policy`/`request`/`runs` 状态 | 行为 |
|---|---|
| 三者都不存在 | 正常首次提交 |
| 三者都存在且非空 | 允许重入，Genesis 由 `GenesisCoordinator.commit` 幂等重放；装配成功后再覆写描述符 |
| 部分存在，或存在但为空 | 报错退出（exit 2），不猜测、不修补 |

分支逻辑已用四种状态（none / all / partial / empty）逐一实测，
结果分别为 0 / 0 / 2 / 2。Genesis 的幂等性来自
`src/novel_agent/services/bootstrap_workflow.py::GenesisCoordinator.commit`：
`initialize_project` 抛 `ProjectAlreadyExistsError` 时改为比对当前
manifest 是否逐字相同，相同则返回既有 `commit_id` 并把
`idempotent_replay` 置为真；不同则 fail closed。

### 4.2 prepare 半完成状态的再入修复（本轮已改）

`20_bootstrap.sh` 在 `state/genesis-prepared.json` 已存在时不再重跑 prepare，
改为调用 `commands/verify_prepared.py` 证明那份冻结准备仍然可复用：

| 检查 | 不通过时 |
|---|---|
| 回执可读、`artifact` 字段可解析 | 拒绝复用 |
| `validation_status == passed` | 拒绝复用 |
| `artifact.media_type` 等于 prepared 契约 | 拒绝复用 |
| `approval_request.project_id` 等于本 run 的 project | 拒绝复用 |
| 工件能按内容地址从对象库读回并校验 | 拒绝复用 |

五条任一不满足即 exit 1，bootstrap 停止，**不删除也不改写**任何冻结工件。
对当前 v24 的准备件实测通过：

```json
{"artifact_id": "sha256:ead3e04d3b2ecdaaaab008150d6d0f32d34a81a852fed3e05203943faf26ed38",
 "byte_length": 111576, "project_id": "project.yujin-jiuxu.v24",
 "reusable": true, "validation_status": "passed"}
```

拒绝路径也逐条实测（错 project / validation_status=failed / 工件缺失 / 回执不可读），
四条都是 exit 1。

## 5. 解除后可直接续跑的命令

```bash
cd /home/cuihengjia/agent/novel/NS/yujin-jiuxu-v24
bash commands/20_bootstrap.sh          # 再入：Genesis 幂等重放 + 检索装配 + 描述符冻结
bash commands/13_start_run.sh          # 创建初始任务，不派发模型请求
bash commands/60_run_stage.sh g0 60    # 受版本管理的 G0 驱动（证据化停止点）
```

不需要先清理数据库：v24 的 Genesis 已经提交且与 preview 一致，
`20_bootstrap.sh` 会走 4.1 的幂等重放分支。

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
  本轮完成到 **Genesis commit 已落库**；运行描述符冻结与检索部署记录尚未发生，
  因此还没有可派发的 run（`runtime_task_projection` 中 v24 任务数为 0）。
- 本轮**没有**产生 v24 的后续规范提交（commit / projection）——
  只有 Genesis 那一笔，它就是当前 `project.current_commit_id`。
- 上述阻塞是**工具环境**限制，不改变指导中的任何验收门槛；
  也没有以此为由跳过 G0 的任何一步。

## 8. 本轮改动的文件

| 文件 | 改动 |
|---|---|
| `yujin-jiuxu-v24/commands/12_commit_genesis.sh` | 三条 `test ! -e` 守卫改为按描述符完整性分支，使半初始化状态可再入（见 4.1） |
| `yujin-jiuxu-v24/commands/20_bootstrap.sh` | prepare 回执已存在时改为校验后复用，不再无条件下调（见 4.2） |
| `yujin-jiuxu-v24/commands/verify_prepared.py` | 新增：证明冻结准备件仍按内容地址可解析，五条检查任一不过即拒绝复用 |
| `yujin-jiuxu-v24/commands/60_run_stage.sh` | 同步 `495e553` 的阶段驱动修复 |
| `scripts/60_run_stage.sh` | 每条命令的结果改从自身 stdout 捕获；`roots` 读失败只记一次；预检不再误报失败 |
| `tests/unit/test_stage_driver_exit_contract.py` | 16 → 18 条；新增「失败只记一次」与「快照来自命令自身输出」两条回归 |
| `docs/remediation/v24_g0_start_and_blocker_20260913.md` | 补记 Genesis 已落库、精确停止点、两处再入修复 |

v24 的 `objects/`、`state/`、`logs/`、`receipts/` 等运行产物按仓库 `.gitignore` 不入库。

## 9. v24 实际跑进 G0 之后（2026-09-13 22:4x–23:0x）

宿主侧解除阻塞后，v24 一路跑到了指导要的层级，并在这条路上暴露了第二个真实代码缺陷。

### 9.1 已完成的部分

| 步骤 | 证据 |
|---|---|
| 描述符冻结 | `state/policy.json` / `request.json` / `runs.json` 于 22:42 写入；`request.basis_commit = sha256:ae7e86a8…` 与 preview 一致；检索部署 `real_hybrid` @ 8081/8082 |
| 输入工件 | 2 份都能按内容地址读回（20405 / 392 字节） |
| STORY 计划 | 4 个 slice、4 次真实模型调用后产出候选；`plan-proposal` `sha256:dc3f8639…`（15808 字节，6 个条目），独立审校 `decision=accept`、`issues=[]`、`verification_failures=[]` |
| 作者裁决 | 接受（`accept-plan`），随后 `plan.commit` 与 `projection` 均 succeeded |
| 计划承诺与锁一致 | `plan.story.reveal_obligation` 逐字写入作者的 `not_before_chapter` 350 / 401 边界 |

这一层回答了指导里最要紧的问题：ARC_VOLUME 路径上计划与审校都能给出**可结算**的判定，
不是 v23 那种无法核验的自由文本要求。

### 9.2 ARC_VOLUME 暴露的缺陷（已修复 `70ad5ff`）

接受 STORY 后生成 ARC_VOLUME 任务
`run.yujin-jiuxu.v24.plan.arc-volume.g0`，首次尝试 **21 秒、0 次模型调用**即
`outcome=suspended`、`failure_class=leaf_review_required`，任务停在
`blocked / leaf_review_required`；`runtime classify` 给出
`repair_owning_module`（"确定性，重试无法改变结果"）——分类是正确的。

宿主合成的终态事件 `terminal.review_required` 的 `payload` 是空的，
所以诊断码（`REVIEWER_CONTRACT_FAILURE` 等）在产物里看不到。
改从对象库找到审校器留下的 draft（`sha256:d6171b21…`）才看到真因：

```
issues=0, decision=accept, memory_gap_questions=[]
verification_failures=[ 3 条 ]   ← 全是模型自己写的自然语言
  a1 问题 '每卷是否严格对应100章' 在大纲中已明确 … 无需作为未决事实询问
  a2 问题 '第四卷前半（201-350章）是否仅允许埋设…' 存在事实错误 …
  a3 问题 '第三碎片获取是否严格限定在第三卷（201-300章）内完成' …
```

根因链条：

1. `PlanReviewDraft` 的结构化输出 schema 由模型生成，因此
   `verification_failures` 字段**暴露给了审校器**；而 `plan_reviewer_v1.md`
   从未要求该字段，它按注释是**宿主**对自己核验结论的记录。
2. `apply_host_plan_review_constraints` 在 `target_kind is not PLAN_PROPOSAL`
   时于第一个守卫直接 `return draft`（INQUIRY 审校就走这条）。
3. 于是模型自填的值原样存活，而 `PlanReviewer.invoke` 只看
   `if draft.verification_failures:` 就抛 `PlanReviewerInvocationError`。

即**被审方可以自己填"宿主判定"字段，并据此把整条 run 停成 blocked**。
修复：在任何目标类型上，宿主合成/核验前先清空该字段，
使"宿主尚未核验"成为唯一可达的核验前状态。

验证：新增 2 条回归
（`tests/unit/test_plan_review_citation_verification.py::test_a_reviewer_cannot_author_the_hosts_own_verdict`、
`::test_a_plan_proposal_citation_failure_is_still_the_hosts_to_report`），
**无修复时第一条失败**（模型文本存活），修复后 26 条全过；
全量确定性套件 **74 failed / 3385 passed**，与 N0 基线逐节点 diff 只少
`test_checked_in_stage2_schemas_match_models`，无新增失败。


### 9.3 ARC_VOLUME 暴露的第二个缺陷：宿主 advisory 被当成"缺条目"（已修复 `613f55c`）

修掉 9.2 之后，ARC_VOLUME 继续推进，attempt 1–4 分别是
`leaf_review_required`（9.2 那个）、一次无失败类、两次 `provider_transient`；
attempt 5 失败为 `validation_rejected`，`runtime classify` 给出
`repair_owning_module`（确定性）。驱动日志里的 `reason_code` 就是缺陷自述：

```
composed plan item set does not match the authorised scope:
  missing ['plan-issue.draft.e49b3ae95f21509138cd2233.0', ...]
```

根因链条（**N3 自己的代码**）：

1. 宿主把它的 advisory 发现登记为
   `ReviewIssueKind.UNRESOLVED_SCOPE_MISSING`，`affected_item_ids` 用的是
   **未决事项的 id**（`plan-issue.draft.…`），不是提案条目的 id。
2. 该发现的英文摘要里含有 "missing"（`…declares no affected_chapters…`），
   于是 `_issue_operations` 的措辞启发式把这条**阻断性 advisory** 读成了
   "有条目缺失"，为它授权 `ADD`。
3. `revision_scope` 因此把 `plan-issue.…` 放进 `scope.additions`；
   而 `plan-issue.…` 永远不可能成为提案条目，`validate_composed_proposal`
   于是要求合成结果包含一个不存在的 id，**对正确输出判为越界**。
4. 每次重试都精确复现（确定性），`validation_rejected` 记入任务预算。

修复：`ADD` 只对**能成为条目**的 id 生效——`plan-issue.` 是未决事项的保留命名空间，
任何措辞都不再能把 advisory id 变成可新增条目。`REMOVE` 的对称风险不成立
（它要求 "duplicate" 标记，而该宿主发现的文案里没有），已在测试中说明。

验证：新增 `test_a_host_advisory_finding_never_authorises_adding_an_advisory_id`，
**无修复时失败**（`additions` 里多出 3 个 `plan-issue.` id），修复后
`tests/unit/test_plan_composition_source_proof.py` 37 条全过；
全量确定性套件 **74 failed / 3386 passed**，与 N0 基线逐节点 diff 只少
`test_checked_in_stage2_schemas_match_models`，无新增失败。

### 9.4 ARC_VOLUME 暴露的第三个缺陷：模型把八卷放进只属于 bootstrap 的字段（已修复 `a306b25`）

修掉 9.3 之后重试，attempt 6 是 `provider_transient`，attempt 7 的新结局是
`PLANNER_STRUCTURED_OUTPUT_REJECTED`（日志 reason code），而任务被结算成
`blocked / basis_changed`。**这个失败类映射本身也是错的**（见下），真正的证据在
`model_call_ledger` 里：

```
model-request.…attempt-3412927ede153537.plan_revision.2   status=validation_rejected
validation_error: [{"type":"value_error","loc":[],
  "msg":"Value error, only PROJECT_BOOTSTRAP may emit bootstrap intent/strategy"}]
```

把该次调用的原始响应读出来核对，模型的输出**几乎完全正确**：

```
mode = arc_volume        project_intent_items = 8 个      plan_items = 0
每个条目：kind=arc_volume, payload.plan_level=arc_volume, 带 chapter_start/chapter_end
          （vol-1 … vol-8，8 卷，正是 G0 要的八卷）
```

即模型把合法的八卷计划放进了 `project_intent_items`（只允许 PROJECT_BOOTSTRAP 使用的
容器），而 `PlannerProposalDraft.validate_mode_output` 因此判它为
"bootstrap intent/strategy"，**对正确的输出判错**，每次重试精确复现。

这个模型行为**早已被承认并修过一次**：同一处验证器里就有一段把
`project_intent_items` 迁回 `plan_items` 的有界别名修复，但它被写死为
`self.mode is AgentMode.CHAPTER_SET`，于是 ARC_VOLUME 上的同一类错误没有被覆盖。

修复：把该别名从"仅 CHAPTER_SET"推广到**以条目规划的模式**
（`STORY` / `ARC_VOLUME` / `CHAPTER_SET`），并保留两种已承认的形状：

| 形状 | 来源 |
|---|---|
| `kind="goal"` + 整数 `chapter_index` + 非空 `summary` | 原有的 CHAPTER_SET 目标形状 |
| 条目自带 `plan_level` 且与 `kind` 一致 | 本次 v24 八卷形状 |

边界仍然收紧：`PROJECT_BOOTSTRAP` 明确不在允许集合内；真正的 Genesis 条目是
`kind="plan"` 且**没有** `plan_level`（已从冻结的 `genesis-prepared.json` 核对），
两种形状都满足不了，所以 bootstrap 内容无法借此绕过模式检查。

验证：

- 用**真实被拒响应**逐一核对：修复后 `plan_items=8`、`project_intent_items=0`、
  levels 全为 `arc_volume`；
- 新增两条回归
  （`test_an_arc_volume_revision_lands_in_plan_items_not_bootstrap_intent`、
  `test_genuine_bootstrap_intent_is_still_refused_outside_bootstrap`），
  **无修复时第一条失败**；同时保住了原有的 CHAPTER_SET 别名测试与
  "bootstrap intent 仍被拒"的负例；
- 全量确定性套件 **74 failed / 3388 passed**，与 N0 基线逐节点 diff 只少
  `test_checked_in_stage2_schemas_match_models`，无新增失败。

#### 附带发现：失败类映射与事实不符（**未修**）

`planning_context_loop` 把 `StructuredGenerationExhausted` 映射成
`PlanningLoopTerminal.BLOCKED`，而 `_planner_failure` 只有
`WAITING_INPUT` / `REVIEW_REQUIRED` / `SUSPENDED` 三个分支，其余全部落到
`BASIS_CHANGED`——于是"模型结构化输出被拒"被记成"基线漂移"，
分类器据此给出 `stop_dependent_work`，方向完全相反（实际应当重试）。
`domain/runtime.py` 里本来就有更贴切的 `LEAF_SCHEMA_REJECTED`
（`retryable=True`、`resume_from=LATEST_SETTLED`）。这属于 N4「重试分类」的
同类问题，但**改动失败类映射会影响面更广**，本轮不动，记录在此待评估。

### 9.5 ARC_VOLUME 暴露的第四个缺陷：advisory 列表没有归属（已修复本轮）

修掉 9.4 后 attempt 8 真正跑完了规划循环（6 次模型调用，含一次 `plan_revision.2`），
但 `compose_scoped_revision` 判它越界：

```
reason_code: "composed plan changed its unresolved issues"
```

这次不是模型的问题，是**范围控制自身的不一致**：

- `_compose_items` 对**条目内未授权字段**的处理是"冻结为父提案的字节"（正确）；
- 但它返回的是 `revised.model_copy(update={"items": ...})`，**`unresolved` 直接跟着修订版走**；
- 而 `validate_composed_proposal` 又要求 `composed.unresolved == parent.unresolved`。

于是只要规划器刷新了 advisory 列表（它在每次修订里都会），合成结果必然"改变了
unresolved"，**任何刷新 advisory 的修订都永远无法合成**——即使八卷本身完全正确。
而 `PlanRevisionScope` 里根本没有承载 advisory 的字段：宿主明明把
`BLOCKING_UNRESOLVED[...]` 之类的发现挂在 `plan-issue.` id 上，这份信息却被丢掉了。

修复：把 advisory 纳入与条目字段相同的规则，并让范围显式承载它。

| 改动 | 内容 |
|---|---|
| `PlanRevisionScope.advisory_ids` | 新增。`revision_scope` 把以 `plan-issue.` 开头、被阻断发现点名的 id 收进这里，不再误当条目 |
| `_compose_unresolved` | 未被点名的 advisory 保留父提案字节；被点名的可被重新表述；父提案没有、但被点名的新 advisory 可以进入 |
| `validate_composed_proposal` | 由"必须逐字等于父提案"改为"未被点名的必须在场且逐字相同；无权新增" |

验证：

- 用**真实的一对提案**核对（父 8 卷/6 advisory、修订 8 卷/4 advisory）：修复前合成被拒，
  修复后合成成功且 `unresolved` 逐字冻结为父提案；
- 新增两条回归
  （`test_a_revised_advisory_list_cannot_change_the_composed_plan_by_itself`、
  `test_a_named_advisory_may_be_restated_and_an_unnamed_one_may_not_be_dropped`），
  **无修复时两条都失败**；
- 全量确定性套件 **74 failed / 3390 passed**，与 N0 基线逐节点 diff 只少
  `test_checked_in_stage2_schemas_match_models`，无新增失败。
