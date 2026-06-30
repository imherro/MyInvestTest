# MyInvest20260601

这是一个用于辅助 A 股投资决策的 Codex 项目。项目目标不是让 AI 无脑荐股，而是把市场研究、主线判断、ETF/个股分析、仓位建议、操作建议和复盘沉淀成可审查、可追踪、可协同的工作流。

## 新电脑如何开始

1. 克隆项目到本地。
2. 先阅读 [项目记忆](docs/PROJECT_MEMORY.md)，了解已经确定的策略、规则和历史决议。
3. 阅读 [数据源与权限](docs/DATA_SOURCES.md)，确认本地数据权限配置。
4. 运行 `python scripts/project_check.py`，检查本地 `.env`、JSON 文件和研究产物命名状态。
5. 再阅读 [协作规则](docs/WORKFLOW.md) 和 [每日流程标准](docs/DAILY_PROCESS.md)，按固定流程继续工作。
6. 每次和 Codex 形成新的重要决议后，更新 `docs/PROJECT_MEMORY.md` 或对应研究文件。
7. 每次更新后提交 Git commit，保持多台电脑之间的上下文一致。

## 核心原则

- 研究成果先固化，再基于固化结果生成操作建议。
- 市场仓位、主线研究、ETF 研究、个股研究、组合分析、操作建议要分模块进行。
- 盘前执行检查只做执行门禁和盘中监控清单，不替代操作建议。
- Codex 给出的每个操作建议都必须能追溯到前置研究结论。
- 任何买入、加仓、减仓、卖出建议都必须包含理由、失效条件和复盘入口。
- 新结论不能覆盖旧结论，必须留下变更记录。

## 重要文件

- [docs/PROJECT_MEMORY.md](docs/PROJECT_MEMORY.md)：项目长期记忆，记录策略框架、已确定规则、历史决议和错误教训。
- [docs/MODULES.md](docs/MODULES.md)：模块架构，定义每个投资研究模块的职责、输入、输出和边界。
- [docs/RUNBOOK.md](docs/RUNBOOK.md)：日常运行手册，说明盘前、盘中、盘后、周末如何运行项目。
- [docs/DAILY_PROCESS.md](docs/DAILY_PROCESS.md)：每日流程标准，说明哪些任务每天做、哪些只读取、哪些放到周末或单独会话。
- [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)：数据源与权限，记录 Tushare 等数据源的使用规则和本地配置方式。
- [docs/FILE_NAMING.md](docs/FILE_NAMING.md)：文件命名与版本规则，要求研究产物使用日期加时间戳，并默认读取最新版本。
- [docs/WORKFLOW.md](docs/WORKFLOW.md)：多电脑协同、Codex 使用、更新和提交规则。
- [scripts/project_check.py](scripts/project_check.py)：本地质量检查脚本，检查 `.env` 配置状态、JSON 可解析性和研究文件时间戳命名。

## 当前阶段

当前已经建立核心模块和首批研究产物。后续重点是按每日流程运行，并逐步补齐 ETF/个股档案、组合分析和复盘记录。

## 协作同步规则

每完成一次任务后，同步到 GitHub 仓库 `imherro/MyInvestTest`。同步时不要提交 `.env` 或任何本地密钥文件。

## 国证自由现金流全收益指数图

运行：

```bash
pip install -r requirements.txt
python scripts/plot_free_cash_flow_index.py
```

程序默认只绘制 `480092.CNI` 国证自由现金流全收益指数，并在收盘价曲线上标记高低交替的波段拐点：

- 主曲线：`480092.CNI` 收盘价
- 拐点折线：按时间连接有效高拐点和有效低拐点
- 有效高拐点：候选高点必须高于前一个有效高点才确认
- 有效低拐点：候选低点必须低于前一个有效低点才确认
- 未突破前一个同类拐点时，只更新当前趋势的末端极值，不新增拐点

输出文件在 `output/`：

- `480092_CNI_new_high_low.html`：波段拐点标记折线图
- `480092_CNI_daily.csv`：日线数据，含 `turning_high` 和 `turning_low` 标记列
- `480092_CNI_record_points.csv`：所有波段拐点记录点

为兼容旧浏览器路径，程序也会覆盖生成 `a_share_free_cash_flow_total_return.html` 和 `a_share_free_cash_flow_total_return.csv`。
