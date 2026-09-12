# ADR-0011：使用 DSH 自定义模式承载 Novel Agent 执行

- Status：**proposed；尚未切换生产或替代 accepted ADR**
- Date：2026-09-12
- Scope：T7，Novel 应用 profile、角色 presets、领域桥接与会话所有权
- Revises if accepted：ADR-0007 的会话投影/压缩实现归属；ADR-0010 中已迁移角色的 leaf 技术选择
- Preserves：Novel 流程语义、信息隔离、Plan authority、Acceptance / Canon / Settlement 不变量
- Detail：[配置、实际接入点与 G0～G3](../deepseek_harness_t7_spike_design_20260912.md)

## Context

用户希望 DSH 提供完整 Agent 外壳，Novel 增加整体流程、特化 Skill 和领域工具，同时保留 DSH
上下文压缩。仅用 Python SDK 包装几次模型调用，无法充分复用其原生模式和长期会话机制。

DSH 已有名为“创造模式”的 `cordis` preset，提供自定义 Agent 的组合编写、运行时检查和
插件实验能力，可作为开发 Novel presets 与插件的现成入口；运行 profile 使用验证后的固定组合。

锁定 DSH `c291e7961a515f6d7af9304e7fd1d257929aef26` 的源码确认：`agent-presets` 通过
`agent.cordis.yml` 定义角色组合，应用 profile 定义共享宿主服务；核心已有 create/resume，
可由原生插件调用。模式只能在空会话切换，minimal preset 没有完整 compaction 配置。

Novel 当前业务流程分散在 CreativeRuntime、PlanningContextLoop、WriterContextLoop 和领域
服务中。WriterCognitionService 绕过共享 Runner 直接调用 ModelGateway，接入不能只替换 Runner。

## Proposed decision

1. 新增一个 `dsh-novel` bundle，安装进从 Web 模板建立的 `novel` profile。复用 DSH UI、
   模型、会话、工具和 Skill registry、日志、压缩与恢复；不 fork DSH 内核。
2. 由 Novel 提供 Director、Planner、PlanReviewer、Writer、Editor presets。不同角色创建
   独立会话，同一创作任务内保留多轮会话；不通过继承 Writer 历史得到独立审查。
3. 少量 TypeScript host plugin 与现有 Python domain worker 通过双向 RPC 相连。Python
   业务状态机继续决定分层规划、Memory 条件、审查、修复、接受与提交；TS 管理 DSH 执行，
   不再维护第二套章节调度状态。
4. 以 PlanningLoopRequest/Result、WritingLoopRequest/Result 为迁移边界，提取可复用领域
   步骤为工具后端。由 DSH 执行模型/工具轮转；已迁移路径退休重复的历史拼装、压缩和通用轮转。
5. 从首个完整角色原型起启用 DSH compaction。Novel 编译有限、有来源的当前约束保护区；
   DSH 管理日志化 prompt 与会话压缩，定制 summarize 内容而复用既有压缩发布机制。
6. Skill 从原 registry 导出不可变快照，工具 schema 从领域契约映射，Python 继续完整验证。
   身份、fence、basis、阶段权限来自宿主绑定，模型无直接 Canon mutation 工具。
7. DSH SessionEventLog 作为执行证据，由 Novel 事件引用；Candidate receipt、Acceptance、
   Commit 仍属于 Novel。覆盖每次 LLM 调用的准入与记账，包括 compaction 和失败尝试。
8. 首轮仅生成候选与审查报告。G0 验证原生组合，G1 验证实际多轮角色与压缩，G2 验故障恢复，
   G3 比较收益并验证隔离完整业务链；通过后才决定生产 canary。
9. Memory 作为宿主必须提供的领域服务，保留主动 Need 准备、领域检索、证据充分性、增量正文
   交付、审稿反馈与接受后的原子记忆更新；模型工具只是其入口之一。Memory 内部的检索 controller
   不属于要退休的通用 Writer loop。迁移验收增加 Memory 能力消融与实际正文效果对照。

## Relationship to existing decisions

ADR-0007 的 provenance、关键约束保护、工具边界、恢复和可审计原则继续保留。拟变更的是
DSH 路径的具体会话投影和机械压缩归属：由 DSH 负责，Novel Context View 保留领域 seed 与
来源投影；不得同时对同一角色历史运行两套 compactor。其原先的安全 cut、CAS、hard-limit
拒绝与 cache/prefix 行为需要与 DSH 对照，差异必须取得证据并在接受本 ADR 时明确。

ADR-0010 的 Temporal 外层目标不被一次 DSH PoC 自动取消；外层 durable lifecycle 与角色
执行是两项决定。采用 DSH 的角色不再叠加仅承担同样 Agent loop 的 LangGraph leaf。
当前 PG Runtime 在外层迁移前仍是唯一生产调度器。接受本 ADR 时须明确修订的角色范围，
避免旧目标要求与新执行路径并行约束同一个 loop。

## Alternatives considered

- **仅自定义 persona / Skill 文件**：能改变行为指导，不能保持完整业务状态机、工具权限、
  类型校验、成本归属和恢复协议；不足以满足本次要求。
- **Python SDK 作为主入口**：适合客户端或窄实验，但目前对恢复、历史和控制的暴露不足。
  主路径采用原生 host service，不先为 SDK 补齐全部服务端能力。
  SDK 也能加载自定义 profile/bundle；它与插件机制并不互斥，两种入口都能保留 Python Memory。
  此选择基于角色会话与应用整合需求，不意味着原生插件天然拥有更高的记忆或小说质量。
- **DSH 原生 profile + presets + Python 领域服务**：推荐，能够保留既有业务资产并复用外壳。
- **所有 Novel 逻辑迁移 TypeScript/Cordis**：可能作为长期实现选择，但本次没有必要，变更
  面显著更大。
- **保留完整旧 loop，再套 DSH loop**：会重复上下文、重试和恢复所有权，不能作为最终形态。

## Consequences

需要维护一个集成包、双向协议、schema/Skill 导出和版本绑定，并对真实生产 caller 做有范围
的重构。收益必须表现为通用执行代码的减少、压缩/恢复可用性与可接受的质量和成本。

DSH 仍处于 Developer Preview，必须锁定版本。插件可组合并不自动保证任意领域合同，
尤其是共享 preset 的会话隔离、最终结果落盘、旧版本恢复、预算与独立审查，需要实际验收。

## Acceptance

本文仍为 proposed。文档引用核验不等于 G0～G3 通过。接受该决定需具备实际模式装配、
多轮工具流程、压缩后约束交付、故障对账、预算、信息隔离，以及被替换代码范围的证据。
DSH session idle 或输出合法 JSON 均不等于小说质量达标或允许 Commit。
