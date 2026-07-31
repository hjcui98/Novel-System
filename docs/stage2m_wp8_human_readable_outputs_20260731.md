# Stage 2M WP8 人类可读产物入口

> 状态：`DIAGNOSTIC_DISPLAY`
>
> 日期：2026-07-31
>
> 结论边界：本展示层不改变 Gate M4 `HOLD`、Gate M5 `INCOMPLETE` 或 deterministic 默认决定。
> 十个旧 case 均缺少当前 formal schema 的六个 identity/budget 字段，且
> `scenario_run.completed=false`；它们不是 P3 或 Gate M4 正式产物。

机器审计产物仍保留在两个 `qwen36_wp8_v1_20260731` 目录中。为便于策划、作者、评审和
项目负责人直接阅读，现已从冻结 artifact 生成 Markdown 展示层：

- [总入口与十点索引](../reports/stage2m/writer_context_benchmark/human_readable_wp8_20260731/README.md)
- [APC C20](../reports/stage2m/writer_context_benchmark/human_readable_wp8_20260731/author_plan_conditioned/C20.md)
- [APC C40](../reports/stage2m/writer_context_benchmark/human_readable_wp8_20260731/author_plan_conditioned/C40.md)
- [APC C60](../reports/stage2m/writer_context_benchmark/human_readable_wp8_20260731/author_plan_conditioned/C60.md)
- [APC C80](../reports/stage2m/writer_context_benchmark/human_readable_wp8_20260731/author_plan_conditioned/C80.md)
- [APC C95](../reports/stage2m/writer_context_benchmark/human_readable_wp8_20260731/author_plan_conditioned/C95.md)
- [VAC C20](../reports/stage2m/writer_context_benchmark/human_readable_wp8_20260731/visible_at_cutoff/C20.md)
- [VAC C40](../reports/stage2m/writer_context_benchmark/human_readable_wp8_20260731/visible_at_cutoff/C40.md)
- [VAC C60](../reports/stage2m/writer_context_benchmark/human_readable_wp8_20260731/visible_at_cutoff/C60.md)
- [VAC C80](../reports/stage2m/writer_context_benchmark/human_readable_wp8_20260731/visible_at_cutoff/C80.md)
- [VAC C95](../reports/stage2m/writer_context_benchmark/human_readable_wp8_20260731/visible_at_cutoff/C95.md)

每个 checkpoint 文件依次提供：

1. checkpoint、freeze、代码和公式身份；
2. 实际发布的 A/B/C arm 及可比状态；
3. 人类快速摘要；
4. Writer 实际收到的完整 `rendered_context`；
5. 逐 Gold 的目标事实、必要性、状态、缺失 component 和主失败层；
6. Writer Context 中每个 ledger ID 对应的章节、span、支持状态与 source commit。

展示文件可由以下命令从冻结对象重新生成：

```bash
.conda-env/bin/python scripts/render_stage2m_human_outputs.py
```

脚本为了读取旧 schema，只在内存中构造 diagnostic-only 身份占位值并关闭 formal
aggregation。它不会回写、补齐或提升原始 JSON；新 P3 必须由当前 runner 原生产生完整
字段和 lifecycle-closed report。
