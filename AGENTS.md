# AGENTS.md

本文件为在本仓库中工作的 AI/自动化代理提供项目上下文与协作规则。修改前请先阅读 `README.md`、`docs/zh_cn/功能说明.md`、`docs/zh_cn/开发相关.md` 以及本文件。

## 项目概览

MaaGakumasu 是基于 MaaFramework 的《学園アイドルマスター》自动化助手，采用 `JSON + 自定义逻辑扩展` 的开发模式。项目主要通过图像识别、OCR、YOLOv11 深度学习模型和模拟控制完成游戏日常、商店、社团、工作、竞赛与自动培育等任务。

目标运行环境以 Windows 为主，推荐 MuMu 模拟器 12，分辨率基准为 `1280x720 (240DPI)`。DMM 版和插件版汉化已适配，但部分组合仍未完全测试。

## 当前进度

最近更新以 `assets/resource/Changelog.md` 的 v1.4.0 公告和最近提交为准；`README.md` 中“等待 NIA 适配”等描述可能滞后。

已实现的主要功能包括：

- 启动游戏、领取活动费、邮箱礼物、任务奖励、每周免费礼包。
- 竞赛挑战，支持指定挑战、自动选择、无编队时自动编队。
- 社团互动，支持自动或指定请求。
- 安排工作，支持领取奖励、自动或指定偶像、指定时长。
- 商店购买，支持扭蛋、金币、AP 购买和自动免费刷新。
- 自动培育处于测试阶段，支持初 `REGULAR/PRO/MASTER`、NIA `PRO/MASTER`、指定偶像、自动选择、体力药、道具、卡片选择优先级、跟随老师建议、初流程失败重试和中断继续。
- Mirror 酱更新、插件版汉化、DMM 版适配、支援卡库存识别、i18n 繁体适配。

近期自动培育重点更新：

- NIA 培育流程已上线，任务配置中通过 `培育难度` 选择 `初` 或 `NIA`。
- 新增考试失败自动重试开关 `启用培育失败重试`，当前覆盖初流程的 `ProduceFailedFlag`。
- 新增 `跟随老师的建议` 开关；事件选择逻辑优先参考老师建议，其次检查 SP 课程，再按角色状态、属性阈值、偏好和随机选择机制决策。
- 培育行动优先级、选秀逻辑、工作类型自动选择、商店购买流程、投票阈值、颜色识别和 `homeflag` 黑白模板识别都有近期修复。
- 培育界面资源图片在 `assets/resource/base/image/produce/` 有最近更新。

待实现或未完全完成的内容包括：

- 初 `LEGEND` 培育适配。
- 更多语言与更多自动培育样本覆盖。

截至最近更新本文件时，工作区存在未提交修改：

- `assets/resource/Changelog.md`

不要覆盖或回退这些文件中的现有改动，除非用户明确要求。

## 目录职责

- `agent/`：Python 自定义逻辑扩展，供 MaaFramework 的 Custom recognition/action 调用。
- `agent/custom/action/produce.py`：自动培育事件、商店、选项等自定义动作逻辑，近期改动集中在行动优先级和 NIA 选择策略。
- `assets/resource/base/pipeline/`：MaaFramework 任务流水线。自动培育通用核心逻辑在 `Produce.json`，NIA 相关流程在 `ProduceNIA.json`，共用节点在 `ProduceUtils.json`。
- `assets/resource/base/image/` 或相邻资源目录：模板匹配、图像识别所需素材。
- `assets/data/`：结构化数据，例如偶像卡片数据 `idols_cards.json`。
- `assets/tasks/`：MFA/MaaFramework 任务入口与选项定义。培育任务入口在 `assets/tasks/produce.json`，中文任务配置在 `produce_cn.json`。
- `assets/lang/`：界面与任务选项翻译。新增任务选项时同步 `zh-CN` 和 `zh-Hant` 等已有语言。
- `assets/resource/Changelog.md`：发布给用户看的资源更新公告；当前内容已进入 v1.4.0 说明。
- `docs/zh_cn/`：中文用户与开发文档。
- `tools/`：维护脚本，例如 README 中提到的偶像素材或卡片数据更新脚本。
- `debug/`：运行日志和调试输出，不应作为功能改动的一部分提交。
- `deps/`、`install/`：依赖和打包相关内容，修改时需确认发布影响。

## 开发环境

- Python 版本：`>=3.12`。
- Python 依赖：`maafw`、`loguru`、`Pillow`。
- 可选开发依赖：`pytest>=7.0`、`ruff>=0.1.0`。
- Node 侧仅用于工具链，当前 `package.json` 包含 `prettier-plugin-multiline-arrays`。
- Python 包版本信息在 `pyproject.toml`，当前仍为 `1.3.8`；用户可见资源公告已更新到 `assets/resource/Changelog.md` 的 `v1.4.0`。

常用检查命令：

```powershell
python -m py_compile agent
python -m pytest
python -m ruff check .
npx prettier --check "**/*.{json,yml,yaml}"
npx maa-tools check
```

如果本地缺少测试目录或依赖，说明无法完整执行对应检查即可，不要为了通过检查凭空创建无关测试。

## 代码与格式约定

- Python 代码遵循 `pyproject.toml` 中 Ruff 配置：目标版本 `py312`，行宽 `144`，启用 import 排序规则。
- JSON/YAML 使用 Prettier 配置：默认缩进 4 空格，YAML 缩进 2 空格，JSON 覆盖配置使用 tab。
- Markdown 文档遵循 `docs/.markdownlint.yaml`，但根目录 `AGENTS.md` 主要服务代理协作，优先清晰准确。
- 修改 JSON、JSONC 或流水线文件时保持原有排序、注释风格和缩进风格；不要做无关格式化。
- 新增用户可见文案时优先使用中文；涉及游戏内名称时保留日文原名，并在已有数据结构支持时补充中文字段。

## MaaFramework 流水线规则

- 先理解节点的 `recognition`、`action`、`next`、`timeout`、`pre_delay`、`post_delay`、`post_wait_freezes` 与 `focus`，再改流水线。
- `DirectHit` 节点通常用于流程入口或无条件跳转，不要随意替换成模板识别。
- `[JumpBack]` 节点用于在循环中回退重试，修改 `next` 顺序时要考虑优先级和误触风险。
- `TemplateMatch` 应明确模板路径、ROI、阈值和必要的匹配方法。新增模板时使用与现有资源一致的分辨率基准。
- `OCR` 只在文本稳定、语言明确时使用；游戏 UI 文案变动风险较高时优先保留模板或自定义识别。
- `Custom` recognition/action 名称必须与 `agent/` 中实现一致，参数结构要向后兼容。
- 自动培育相关改动风险较高。修改 `Produce.json` 时重点验证：
  - 入口与中断继续流程：`Produce`、`ProduceLoop`、`ProduceSkipPreparation`、`ProduceEntry`。
  - 难度入口：`初` 走 `ProduceEntry`，`NIA` 走 `ProduceEntryNIA`；不要把 NIA 覆盖项误合到初流程。
  - 准备阶段：难度、偶像、支援、回忆、道具选择。
  - 培育阶段：事件选择、卡牌选择、饮料、道具、商店、强化、考试失败和结束流程。
  - 失败处理：初流程的 `ProduceFailedFlag` 可根据 `启用培育失败重试` 跳转到重试或停止流程；NIA 流程使用 `ProduceNIAFailedFlag`，当前失败后停止任务。
  - NIA 事件参数：每张卡片通过 `ProduceChooseNIAEventFlag.custom_action_param` 设置 `effect`、`first`、`second`，字段顺序和语义都要保持一致。
  - 弹窗和通用按钮处理：不要扩大 ROI 到容易误触的位置。

## 任务配置规则

- 培育任务定义在 `assets/tasks/produce.json`，中文版本在 `assets/tasks/produce_cn.json`；新增或重命名选项时两边都要同步。
- 当前培育选项包括 `培育难度`、`培育偶像`、`培育次数`、`使用体力药`、`使用道具`、`跳过选择偶像`、`启用自动回忆`、`启用关注租借`、`启用培育失败重试`、`跳过准备阶段`、`卡片选择优先级`、`跟随老师的建议`。
- `培育难度` 下 `初` 支持 `REGULAR/PRO/MASTER`，`NIA` 支持 `PRO/MASTER`。
- 任务选项通过 `pipeline_override` 调整节点属性；改选项时必须检查被覆盖节点在 `Produce.json`、`ProduceNIA.json` 或 `ProduceUtils.json` 中是否存在且语义匹配。
- `preset.json` 的一键培育默认仍以 `初` `PRO` 为主；新增默认项前先确认不会增加普通用户误触或长流程失败风险。

## 数据与资源规则

- `assets/data/idols_cards.json` 包含 SSR/SR/R 卡片数据和保存时间。更新时保持字段名称一致，包括 `卡片名称`、`偶像名称`、`歌曲名称`、`偶像中文`、`歌曲中文`、`推荐效果`、`体力`、`Vo`、`Da`、`Vi`、`奖励加成`、`登场日期`。
- 卡片或素材更新优先使用 `tools/` 下已有脚本，不要手工批量改写大数据文件，除非用户明确要求。
- YOLOv11 数据集当前基于 README 与开发文档记录：
  - `cards` 集用于出牌识别，样本约 902 份。
  - 早期用于上课和冲刺选项识别的 `button` 集已废弃，相关按钮识别已改为普通模板匹配。
- 新增图像素材时应说明来源、截图环境和分辨率。不要提交游戏资源本体之外的非必要大文件。

## 测试与验证

改动完成后，根据影响范围选择验证：

- Python 自定义逻辑：至少运行 `python -m py_compile agent`，有测试时运行 `python -m pytest`。
- 流水线或资源：运行 `npx maa-tools check`，并在可能时进行实际 MaaFramework 调试。
- JSON/YAML：运行 Prettier 检查或格式化。
- 自动培育：需要真实设备或模拟器长流程验证；如果无法运行，必须在交付说明中明确未做实机验证。

调试时优先查看 `debug/maa.log`。自动培育一次通常约 30 分钟，会产生大量日志；不要提交日志文件。

## 协作注意事项

- 不要回退用户已有修改。当前工作区若有不相关改动，保持原样。
- 不要在未确认的情况下调整发布、安装、依赖打包或 Mirror 酱相关配置。
- 不要把 README 中标注为测试阶段的自动培育描述成稳定功能。
- README、功能说明与 Changelog 若存在冲突，先检查最近提交和 `assets/tasks/produce.json`；当前 NIA 状态应以 v1.4.0 Changelog 和任务配置为准。
- 不要改变项目许可证、免责声明或商业用途限制。
- 需要联网查询 MaaFramework、MFAAvalonia、Mirror 酱或 OpenAI 等外部信息时，优先使用官方文档，并在回复中说明来源。
- 对用户报告的运行问题，优先索要或检查 `debug/maa.log`、模拟器类型、分辨率、系统平台、游戏版本、是否 DMM/插件版汉化。
