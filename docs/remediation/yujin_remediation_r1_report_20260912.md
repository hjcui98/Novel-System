# R1 执行证据（声明 / 动作 / 宿主 Review / 责任表对账）

日期：2026-09-12（+08:00）
范围：方案第 6 节 R1、第 8 节 A01–A05
状态：R1.1、R1.2、R1.3 已实现并取得证据；R1.4（waiver 回执）与 R1.5（advisory 逐条处理）未完成。

## 1. v6 阻塞根因（真实产物定位）

| 失败面 | 冻结事实 | 触发点 |
|---|---|---|
| 八卷责任表未编译 | ARC_VOLUME 候选 8 个 item 共声明 16 条 `obligation_plan`；WorldRoot `6912843c` 的 `obligations` 为 **0** | binder 的 `declaration_keys` 不含 `obligation_plan`，整张表被忽略 |
| 五章动作不可读 | 5 个 chapter_goal 的 `obligation_actions` 是自然语言字符串，如 `"setup: 确认残星纹为断序星纹 (setup_window: 1-30)"` | `materializers.py` 把字符串当 `StableId` 解析，commit 才抛错 |
| 宿主 Review 未拦截 | 该候选 host review 得到 `accept / 0 issues` | `plan_reviewer` 只校验 `history_retrieval`，不校验动作/责任表形状 |
| 历史决策是伪豁免 | 五章全部 `NOT_REQUIRED` + `waiver_ref="first_chapter_waiver_extension"`（模型自造）；第 1 章无 `history_retrieval` | 宿主只做 pydantic 形状校验，不核对 waiver 来源 |
| unresolved 语义降级 | 3 条揭露疑问为 `UNSPECIFIED / blocking=false` | 未区分“语义不确定”与“硬约束冲突” |

## 2. 已实现

### 2.1 统一规范化归属 `domain/obligation_contract.py`

- 章窗解析（`1-50` / 单章 / 反序与中文一律拒绝）；
- 动作枚举 `SETUP/PROGRESS/PAYOFF/DEFER`，统一映射旧式 `setup:` 写法；
- 旧 `obligation_plan` → 声明编译，保留 `source_form`/`source_ordinal`/三个窗口的来源记录；
- **每个不可解释项产出 discrepancy，不丢弃、不按名称近似匹配**；
- 缺 `payoff_window` 的责任被拒（无可观察完成边界）；
- 自由文本动作被拒并给出可操作信息。

### 2.2 binder 与校验共用同一契约

- `_bind_obligation_declarations` 通过 legacy 分支编译 `obligation_plan`；
- `_validate_obligation_references` 与 binder 使用同一 `compile_obligation_actions`；
- 下层（CHAPTER_SET/CHAPTER/SCENE）声明 durable 责任表仍被拒绝；
- 自审发现并修复的缺陷：`obligation_plan` 同时出现在通用 key 循环与 legacy 分支会导致同一责任按不同 ordinal 追加两次（16 条变 32 条、身份互不相关）。现已只在 legacy 分支编译，并有回归测试固定“混合形态身份唯一”。

### 2.3 宿主 Review 拦截

新增 `ReviewIssueKind.OBLIGATION_CONTRACT`，在 ACCEPT 前覆盖：

| 代码 | 场景 |
|---|---|
| `OBLIGATION_ACTION_UNREADABLE` | 自由文本章动作、缺 `expected_delta`、非法动作/窗口 |
| `OBLIGATION_PLAN_FORBIDDEN` | 章节/场景层声明 durable 责任表 |
| `OBLIGATION_PLAN_UNREADABLE` | 责任条目缺 summary/kind/窗口，或窗口不可解析 |
| `OBLIGATION_DECLARATION_UNREADABLE` | `obligation_declarations` 非对象列表 |

## 3. 证据

### A01 —— v6 原六条字符串动作

单元与宿主 Review 两层均拒绝：`compile_obligation_actions` 返回 discrepancy，`plan_reviewer` 给出 blocking issue → `REVISE`，候选不再进入 accept/commit。
测试：`tests/unit/test_obligation_contract.py`、`tests/unit/test_plan_review_obligation_contract.py`。

### A02 —— 八卷 16 条旧责任表对账（真实产物）

命令（只读对象库，无模型调用，无写入）：

```text
PYTHONPATH=<worktree>/src python3 scripts/audit_v6_responsibility_binding.py \
  --object-store /home/cuihengjia/agent/novel/NS/yujin-jiuxu-v6/objects \
  --proposal sha256:362b7626fe852428176b509dc1674cd281a802a04fceafc1c548f7cfe9152475 \
  --world sha256:6912843c3ae9dfaa9303ad4d4a1c86ea8db793b67e45d610603cf49495c3dd0d
```

结果：`declared 16 → compiled 16`，8 个 item 全部有绑定，每个 item `declared == bound`，
写出 1 个 WorldRoot artifact，`0 untraced`。身份形如
`obligation.vol_01.0.objective`、`obligation.vol_08.1.objective`，`not_before` 分别为
1/101/201/301/401/501/601/701，与八卷时间锁一致。
完整输出：工作区 `tmp/yujin-remediation-20260912/R1/a02-v6-responsibility-binding.txt`。

**A02 暴露的数据问题（留给 v7 规划与 R2/R6 处理）**：`vol_07.0` 与 `vol_08.0` 的
`summary` 完全相同（“陆沉舟获取陆远作为封印者的实质线索”），即同一责任在相邻两卷各声明一次。
本轮按“身份 = item+ordinal+kind”各自编译，不猜同一性、不擅自合并；v7 重建规划时应消除该重复，
否则同一责任会出现两个身份（对应第 9 节“无同义重复身份”门槛）。

### A03 —— 合法链路与非法输入

- 合法：声明 + 结构化动作 → 绑定成功（`test_valid_structured_action_binds_after_the_declaration_is_compiled`）；
- 未知 ID：`obligation action references an undeclared obligation`；
- 偷建：CHAPTER_SET 声明责任表被拒；
- 非法动作：`REVEAL` 等不在枚举内一律拒绝。

## 4. R1 未完成项

| 项 | 现状 | 下一步 |
|---|---|---|
| R1.4 waiver 回执真实性 | **已完成**（见第 6 节） | — |
| R1.5 advisory 逐条处理 | 未实现：3 条揭露疑问仍是 `UNSPECIFIED / blocking=false` | 语义不确定保留独立审查结果与精确影响范围；不笼统降级 |
| A05（未来章规划时无正文） | 部分：宿主任仍需按执行时 canonical 历史检索 | 随 R3 完成 |

## 5. R1.4 waiver 真实性（本轮追加完成）

冻结 v6 候选把第 2—5 章标为 `NOT_REQUIRED`，`waiver_ref="first_chapter_waiver_extension"`——
宿主从未签发过的字符串，而 host review 当时接受了它。

实现：

- `domain/retrieval_decision.py` 成为宿主签发豁免的唯一归属：
  `FIRST_CHAPTER_WAIVER_REF`、`HOST_ISSUED_WAIVER_REFS`，以及 `waiver_is_host_issued`；
- host review 新增两条 blocking 规则：
  `HISTORY_WAIVER_UNVERIFIED`（waiver_ref 非宿主签发）、
  `HISTORY_WAIVER_INAPPLICABLE`（把首章豁免用于第 1 章以外的章节）；
- `writer_readiness` 新增 `HISTORY_WAIVER_NOT_APPLICABLE`：canonical Text basis 已有正文时，
  首章豁免不再成立；Stage 3 writer 工厂传入真实的 basis 信号
  （`any(chapter.blocks for chapter in text.chapters)`）。

证据：`tests/unit/test_history_waiver_authenticity.py` 用 v6 原始 decision payload 断言 REVISE；
`tests/unit/test_writer_history_retrieval_gate.py` 断言同一 waiver 在空 basis 下不报该码、
在已有正文时报错且 `ready is False`。两文件共 11 项通过。

## 6. 回归与失败身份（R1 全部增量）

`tests/unit tests/contract`：**76 failed / 2937 passed / 1 skipped**；
失败身份集合与整合前基线**逐项完全相同**（0 新增、0 修复），本次新增 61 项通过测试。
命令与清单见工作区 `tmp/yujin-remediation-20260912/R0|R1/`。

