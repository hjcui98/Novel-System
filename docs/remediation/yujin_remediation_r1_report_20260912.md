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
| R1.4 waiver 回执真实性 | 未实现：宿主仍会接受模型自造的 `waiver_ref` 字符串 | 第 1 章豁免必须由宿主在 canonical 正文为空时生成并可回读；其它章 `NOT_REQUIRED` 需真实审批回执 |
| R1.5 advisory 逐条处理 | 未实现：3 条揭露疑问仍是 `UNSPECIFIED / blocking=false` | 语义不确定保留独立审查结果与精确影响范围；不笼统降级 |
| A04/A05 | 依赖 R1.4/R3 | 随 R3 完成 |

## 5. 回归与失败身份

受影响测试集合：8 个文件、115 项，其中 4 项失败**与整合前基线逐项同名**（`test_stage5_production_factories.py` 的既有失败），本次未新增失败。
命令：

```text
PYTHONPATH=<worktree>/src NOVEL_AGENT_FORBID_MODEL_CALLS=true \
  .conda-env/bin/pytest -q -p no:cacheprovider --no-cov <files>
```

Ruff 在本次改动行上干净（`plan_reviewer.py`/`materializers.py` 仅保留整合前既有的 E501/RUF001），
`domain/obligation_contract.py` 严格 MyPy 无错误。
