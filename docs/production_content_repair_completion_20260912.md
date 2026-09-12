# 生产内容通路修复记录（2026-09-12）

基线：`1856153298856bdcb6eb7a8c6ad62f0727ac9683`，修复保留在当前未提交工作区。
本文取代二次审查中的“尚未实施”状态；审查原文及此前 246 项结果保留为历史证据。
用户要求先改代码、最后验收，本轮遵循此顺序。真实小说产物暂不可用。

## Memory 的三个关键修复

- `memory_gateway.py` 将 grounded block/span 重新按当前 commit、snapshot 和 cutoff 解析，
  保留 facet 对应关系，写入冻结的 Writer evidence selection。默认检索和混合检索共用此出口。
- `production_bootstrap.py` 给 Need planner 注入生产模型网关；问题正文包括结构化计划要求。
  `paired_controller.py` 按请求公平分配路由预算并传入 reranker，避免后面的问题饿死。
- 普通 Curator 的整章四条上限已移除。`ordinary_curation.py` 按源正文分段并逐页抽取，
  单次响应最多四条，满页强制续页；宿主汇总整章所有操作后再绑定证据、验证和原子提交。
  每批检查覆盖情况；不能闭合、无进展或超出续页预算时明确失败，不默默丢掉余下变化。
  World 输入采用相关工作集，模型可请求精确身份查找，最终验证仍使用完整 World。

## 二次审查逐项收口

| 审查项 | 实施结果与主要代码 |
|---|---|
| P1-01 Writer 技能不可选 | 生产允许场景、大改及六类写作方法；Registry 提供实际用途、适用条件和检查点；Cognition 按模式保留核心、限制少量可选方法 |
| P1-02 技能旧协议冲突 | 重写全部 38 个方法文件；输出协议由宿主负责；普通 Writer 可请求 Memory，大改须交完整稿；Planner Inquiry/执行阶段按实际选择加载；Editor lenses 纳入注册与哈希校验 |
| P1-03 旧计划残留 | materializers 按同层覆盖范围退役旧节点及失效后代/依赖，保留已提交前缀；清理对应未来 goal 和里程碑；检查父子范围 |
| P1-04 要求阶段语义 | entry conditions 只约束进入阶段；invariants 跨章保持；exit outcomes 到范围末端必须完成；Writer 展开依赖及其完成证据 |
| P1-05 整章四条变化 | 普通变化分段、分页、覆盖闭合及整章聚合；保留逐页调用和来源回执；截断有单独紧凑重试身份与累计预算 |
| P1-06 义务身份映射 | 接受计划生成稳定 milestone ID；Curator 使用受限目录报告原 ID 的观察；正文证据写入 World 的同一身份；到期里程碑无完成证据则阻止提交，保留时间锁 |
| P1-07 Editor 错用 Writer 设想 | Context 保留来源权限与可验证引用；工作计划不算事实；supported 只能引用经过源验证的内容；avoided 必须声明非计划必需并给出正文证据 |
| P1-08 恢复重跑 Memory | ProductionWritingRequestFactory 先恢复冻结请求并校验当前 Canon、配置和字段绑定，再进入新请求路径；完成的模型调用按身份、请求哈希和原始工件重放 |
| P2-09 Memory Focus 猜 ID | task_focus 使用章节范围和祖先链选择计划；Need prompt 包含完整要求；不靠 ID 正则或固定截取前八个节点 |
| P2-10 修复状态丢失 | 持久化局部修复/大改/复审前沿、完整历史、计数和模型记录；所有 gateway 共用单轮调用预算，内部重试也计费；独立 Editor 通过后才结算技能完成检查点 |
| P2-11 lookahead 越卷与误提升 | 后台窗口和前台使用同一卷范围；需要新卷时由前台先规划；候选根据 Text/World/Plan/Profile 根变化重新判断有效性 |
| P2-12 长跑输入与缓存增长 | Curator 使用有界 World 工作集及显式查找；检索缓存最多保留两个 commit；生产语义判断使用已配置的 implementation endpoint |

另修复：Plan Reviewer 保持 HUMAN_REQUIRED 严重度并展开对照来源；Planner 校验作者、父计划、
Canon、Reviewer 等来源身份，不能只靠模型自报 provenance。模型调用取消与图抽取子任务失败时
清理剩余任务和调用槽；取消恰好发生在 lease 交付后也会释放容量。恢复 Context 时不再用较旧的
周期检查点覆盖较新的工作流检查点，避免丢失大改指令。

## 最终离线验证

最终定向回归 **375 项通过（61.68 秒）**；变更 Python 文件 Ruff 检查通过，
50 个变更源码文件 mypy 检查通过。全部 pytest 命令设置 `NOVEL_AGENT_FORBID_MODEL_CALLS=true`，使用 `--no-cov`。
Stage 0–5 的 574 份导出 schema 已同步，并逐份与相应导出器的模型 schema 比较一致。
测试范围为 19 个相关单元、集成和契约文件，包含生产装配及 CLI 的假端点完整章节提交。
本轮不运行正式 100% 分支覆盖率门槛，也没有更改该阈值。

新增回归直接覆盖：六条变化跨两次响应全部进入证据绑定操作、末页覆盖不足拒绝、长正文完整
分段、工作集外实体精确查找、到期里程碑原 ID 及正文证据、重启后完成调用零成本重放、跨
gateway 的调用预算、lease 交付后取消释放容量、单次一个模型调用的局部修复与大改恢复。
生产工厂用真实类型的冻结 checkpoint 验证恢复不再调用 Memory 生成器。

复查入口：

- `tests/unit/test_production_memory_closure.py`
- `tests/unit/test_production_content_repairs.py`
- `tests/unit/test_stage5_production_factories.py`
- `tests/unit/test_production_assembly_one_chapter.py`
- `tests/integration/test_writer_context_loop.py`
- `tests/integration/test_stage5_creative_runtime.py`

## 仍需真实运行确认的边界

这次验证证明程序流转与失败条件，不证明模型提取完整、计划内容具体或小说好看。
覆盖率字段、里程碑语义判断和 Editor 判定仍可能被模型误报，精确引用只能证明来源存在。
真实 hybrid 的服务、索引、召回及重排效果仍需配置后实测；CLI 保留显式后端选择，不自动启动服务。
大改阶段仍使用已冻结的 Memory，暂不重新进入交互检索。

lookahead 暂以四类完整根作为依赖集合，根变化会保守地重规划；这牺牲部分后台复用率，避免
把旧候选误认成仍然有效。续页、调用和修复预算仍有明确边界，耗尽时会让出或阻塞并保留证据。
里程碑台账复用 Canon 的 Plan 意图和 World 观察，不增加第二份独立事实数据库。
长篇持续生成的成本、恢复能力和文学质量，须后续用真实成品及完整链路记录验收。
