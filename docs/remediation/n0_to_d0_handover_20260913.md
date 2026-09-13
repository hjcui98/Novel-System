# 《余烬九序》N0—D0 执行汇总与 G0 起点（2026-09-13）

对应指导：`docs/yujin_jiuxu_next_stage_execution_guidance_20260913.md`。
本文件是该指导 N0—D0 阶段的执行索引与交接起点，逐包细节见各自的交付记录。

## 1. 一句话状态

**工程修复完成，真实推进未验证。** N0—N4 已按指导实现并留下定向与全量证据；
D0 已用冻结工件证明"准确审校 → 范围内修订 → 复审 → 正式物化预检"这条链可以收敛并物化。
G0 尚未开始，八卷、正式义务和首批五章计划**没有任何后续提交**。

## 2. 提交序列

| 提交 | 内容 | 记录 |
|---|---|---|
| `0b62f00` | 接手基线（指导记载的核对基线） | — |
| `fc25330` | N1：审校意见绑定当前候选 | `n1_delivery_20260913.md` |
| `0041caf` | N2：已接纳义务窗口进入生产审校 | `n2_delivery_20260913.md` |
| `e7b9705` | N3：修订范围、宿主合成来源证明、进展身份 | `n3_delivery_20260913.md` |
| `2625cdb` | 同上，交付记录 | — |
| `90f1533` | N4：attempt/ledger 分类、驱动退出码、阶段停止 | `n4_delivery_20260913.md` |
| `a1e8e1f` | D0：冻结候选的确定性与真实诊断 | `d0_delivery_20260913.md` |
| `5cddd89` / `19e1556` | D0 交付记录与源码提交固定 | — |

N0 基线记录见 `n0_baseline_20260913.md`；基线失败节点清单见
`tmp/evidence/n0_baseline_failures_0b62f00.txt`（工作树内，未提交）。

## 3. 各包实际改动与可观察变化

### N1 审校引用核验

- 生产路径：`PlanReviewerAgent.review` → `apply_host_plan_review_constraints`，
  **所有返回路径共用同一份已核验问题集合**。
- 结构化定位：先按 `affected_item_ids` 找到条目，再在该条目 payload 内解析
  `a.b[0].c` 形式路径（不使用 `eval`），引文只在被引字段的值中核验。
- 文本/数值/集合/缺失字段分开处理；缺四个引用字段之一即判为审校缺陷。
- 修订指令改由已核验问题派生，失实要求不再随自由文本进入 Planner。
- 拦不住的引用触发 `PlanReviewerInvocationError`，运行停在 `REVIEW_REQUIRED`。

### N2 义务窗口接线

- `_accepted_obligations` 成为唯一读取者，同一次读取同时给出义务 ID 目录与各自窗口。
- `not_before_chapter`（开启锁）约束阶段早边界，`due_chapter` 约束晚边界，
  `target_chapter_start` 只在无开启锁时充当早边界，不压成一对通用数字。
- 三态明确：目录不可读（`OBLIGATION_CATALOGUE_UNKNOWN`）/ 目录可读且空（按 ID 不存在判）/
  义务存在但无时间约束（报"无法核验"而不是通过）。
- 合法 `setup` 保持豁免；自身描述就在宣称披露动作的条目不再买到该豁免。
- prompt 与 temporal lens 中"改标签不影响任何机检字段"的失实描述已改正。

### N3 范围、来源证明与进展

- 合成移入 `domain/plan_composition.py`，loop 与 materializer 共用一份实现。
- 范围只来自已核验阻断问题；`modify`/`add`/`remove` 分开；未点名条目逐字取父候选；
  未授权新增不进结果；未授权删除被恢复；点名条目内只有具名 payload 键可变。
- 合成候选获得自己的 `PlannerExecutionResult`，带 `composition_proof` 与
  `raw_plan_proposal`；materializer 保留直接匹配，并为合成候选**重新推导**后精确比较。
- 进展身份 = 类型 + 条目 + 字段 + 约束；值单独保存；checkpoint 前沿另带
  `attempted:` 段，使 A→B→A 的振荡被识别为已尝试过的问题。

### N4 分类、退出码与阶段停止

- `runtime classify --task-id` 读最后一次已结算 attempt 的规范 `failure_class`
  与该 attempt 的 effect ledger，经 `failure_policy` 给出处置动作；
  `block_cause` 只用于报告"存在但不是规范分类"。
- 未结算发送 > 已落盘响应 > 分类本身；未知分类报 `undetermined`。
- `scripts/60_run_stage.sh`：无管道、无 `|| true`、收尾失败计入退出码；
  阶段按 `runtime roots` 的已提交卷/章数证明出口。

### D0

- 冻结候选的真实状态被核正：**没有机械缺陷**，旧审校被拒的真正原因是引用失实。
- 确定性链完整跑通，含篡改 proof 被拒。
- 真实模型（8003）审校返回 **ACCEPT，0 条阻断意见**，`verification_failures` 为空。

## 4. 验证口径与结果

| 检查 | 结果 |
|---|---|
| 冻结候选真实审校（`d0.review.json`） | `ACCEPT`，0 阻断，0 核验失败，usage 10094/449/0，费用可得性 unknown |
| 确定性全量 `tests/{unit,contract,integration}`（排除 model/integration 标记） | **74 failed / 3354 passed** |
| 与 N0 基线逐节点 diff | 只消除 `test_checked_in_stage2_schemas_match_models` 一项，**无新增失败** |
| 本阶段新增确定性用例 | N1 24 项、N2 19 项、N3 36 项、N4 35 项、D0 2 项 |
| Ruff | 本阶段触及文件全部通过 |
| MyPy | 本阶段新增源文件无错误；`materializers.py`/`cli.py` 的其余报告为基线既有（`git stash` 对照确认） |
| `make quality` / `make integration` | **未运行**（仓库仍有 74 项历史失败） |

失败节点清单：`tmp/evidence/n0_baseline_failures_0b62f00.txt` 与 N4/D0 记录中引用的 diff。

## 5. 保留的现场

- v23 全部原始对象、日志、检查点、模型响应**未改动**；诊断只写自己的对象存储。
- 根工作区 `/home/cuihengjia/agent/novel/NS` 的其他任务修改**未触碰**。
- 冻结的 v23 驱动脚本未改，作为证据保留。
- `.conda-env` 共享环境未改；所有命令显式 `PYTHONPATH="$PWD/src"`。
- 未提交密钥、`.env`、私有小说内容或运行产物；`tmp/` 与 `volumes/` 沿用 `.gitignore`。

## 6. 已知限制（进入 G0 前应知）

1. **G0 未开始**：无八卷节点、无章节目标、无正式义务、无正文提交。
   "工程修复完成"不等于任何 G0/G1/G2 通过。
2. **真实义务目录仍为空**：v23 全部 World 快照 `obligations` 为空，
   依赖义务窗口的 `serves` 检查尚未被真实数据驱动过。
3. **真实链只跑了一次 review**：没有真实 Planner 修订。
   "真实模型完成一次限定修订"只有确定性证据。
4. **驱动未在断点数据库上实跑**：只验证了控制流、退出码与门限。
5. **v20/v21 残留 `RUNNING`/旧 `REQUESTED` 未处置**；N4 会阻止盲重试，但清理需运行时入口。
6. **阶段门限只证明根与章节数**，不替代指导第 12.3 节的逐项 G0 证据核验
   （逐条义务映射、时间锁回读、独立 review/accept/commit/projection）。
7. `make quality` 与 `make integration` 未运行；历史 74 项失败未处理，
   其中是否有关键路径项需在 G0 前按节点复核。

## 7. 下一步建议

按指导顺序，进入 G0 前建议先补两件**未验证项**，再做单次冻结：

1. 用真实模型补一次限定修订（真实 Planner 修订 + 真实复审），
   使 D0 的"真实链路"完整，而不只有确定性证据。
2. 在断点数据库上按 N4 的 `runtime classify` 核对 v20/v21/v23 的
   `RUNNING`/`REQUESTED` 残留，用运行时核对/取消/恢复入口处置，保留终态原因。

随后按指导第 12 节执行单次冻结与 G0：新 project/run 选未占用身份、独立目录、
记录源码 SHA 与 prompt/skill 指纹、`bootstrap` 与 `advance` 从同一受信配置读取 endpoint，
预检不只看 health。G0 出口按第 12.3 节核验后再派发 Writer。
