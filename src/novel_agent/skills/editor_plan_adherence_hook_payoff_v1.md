# Editor 视镜：大纲依从与伏笔回收（plan adherence / hook-payoff）

强制执行当期章节的规划约束与未来锁定的剧情承诺。

The editor must account for future-locked obligations; they remain locked until their declared chapter boundary.

- `mandatory_constraints`（强制约束）与 `forbidden_reveals`（禁止揭露）为硬性标准。凡这些字段所禁止的回收、揭露或解决，均构成阻断性问题。
- 未来锁定的剧情承诺当前只可执行铺垫（SETUP）或推进（PROGRESS）；严禁在 `not_before_chapter` 之前执行解决（RESOLVE）或回收（PAYOFF）。
- 切勿仅因章节中提及了某项长程伏笔，就将其视为已完成回收。
