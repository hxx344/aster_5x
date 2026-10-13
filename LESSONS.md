# Lessons from merged work

这里保存 aster_5x 的合并经验。改动前先读本文件。每条先给加粗祈使规则，再给证据（SHA / 现象），结尾 `Seen N× (日期…)`。满 3 个不同复盘日迁入 [docs/code-standards.md](docs/code-standards.md) 并在此留 Moved 索引。

## Frontend performance

**周期成交历史与 quality 面板在 props 未变时 memo，避免父级无关刷新拖垮列表。**

- 证据：`dc998c59` memoize `cycle-trades-panel`；`92aee006` memoize `cycle-quality-history`。
- Seen 1× (2026-10-13)

## Theme / docs

本窗口另有 `83025c1e`（深色主题），纯样式，无缺陷类教训。

## Moved to the code standards

尚无。
