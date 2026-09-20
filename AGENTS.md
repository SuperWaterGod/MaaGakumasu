# AGENTS.md

本文件为在本仓库中工作的 AI/自动化代理提供项目上下文与协作规则。修改前请先阅读 `README.md`、`docs/zh_cn/功能说明.md`、`docs/zh_cn/开发相关.md` 以及本文件。

## 项目概览

MaaGakumasu 是基于 MaaFramework 的《学園アイドルマスター》自动化助手，采用 `JSON + 自定义逻辑扩展` 的开发模式。项目主要通过图像识别、OCR、YOLOv11 深度学习模型和模拟控制完成游戏日常、商店、社团、工作、竞赛与自动培育等任务。

目标运行环境以 Windows 为主，推荐 MuMu 模拟器 12，分辨率基准为 `1280x720 (240DPI)`。DMM 版和插件版汉化已适配，但部分组合仍未完全测试。

## 当前进度

最近更新以 `assets/resource/announcement/01_更新公告.md` 的 v1.5.1 公告和最近提交为准。

已实现的主要功能包括：

- 启动游戏、领取活动费、邮箱礼物、任务奖励、每周免费礼包。
- 竞赛挑战，支持指定挑战、自动选择、无编队时自动编队。
- 社团互动，支持自动或指定请求。
- 安排工作，支持领取奖励、自动或指定偶像、自动或指定时长。
- 商店购买：金币扭蛋按各类型硬币数量判定（仅勾选且数量 ≥ 10 才购买），金币兑换与 AP 兑换支持按列表勾选，免费刷新自动识别并使用。
- 自动培育处于测试阶段，支持初 `REGULAR/PRO/MASTER`、NIA `PRO/MASTER`、指定偶像、自动选择、自动支援卡选择、体力药、道具、卡片选择优先级、跟随老师建议、培育失败重试（初 + NIA）、试镜难度降低、试镜手动接管和中断继续。
- 偶像之路自动挑战，支持自动编队、自动战斗与失败重试（暂未适配汉化版，仅官服/DMM）。
- Mirror 酱更新、插件版汉化、DMM 版适配、支援卡库存识别、i18n 繁体适配。

近期自动培育重点更新：

- NIA 培育流程已上线，任务配置中通过 `培育难度` 选择 `初` 或 `NIA`。
- 新增考试失败自动重试开关 `启用培育失败重试`，覆盖初流程 `ProduceFailedFlag` 和 NIA 流程 `ProduceNIAFailedFlag`。
- 新增 `启用试镜难度降低` 选项（NIA 模式），支持降低一档或两档，检测到锁定图标后自动降档。
- 新增 `跟随老师的建议` 开关；事件选择逻辑引入优先级系统，提取基类 `ProduceChooseEventBase`，增加事件保底选择机制。
- 培育行动优先级、选秀逻辑、工作类型自动选择、投票阈值、颜色识别和 `homeflag` 黑白模板识别都有近期修复。
- 剧本选择重构为模板匹配（替代 OCR），支持 Ranking/NIA/HIF 三种剧本；饮料识别升级为多模板匹配。
- NIA 流程入口 `ProduceEntryNIA` 与 `ProduceGuideEntry` 新增 `[JumpBack]ProduceChooseStrengthenFlag` 回跳，识别到强化选择标志时回到对应处理节点，避免流程中断。
- 培育界面资源图片在 `assets/resource/base/image/produce/` 有最近更新。

v1.5.1 改动（已写入 v1.5.1 公告，细节以提交为准）：

- 商店金币扭蛋：数量面板改为一次 OCR 全量识别并按格子中心归类到各扭蛋类型；购买循环改为 `remaining` 驱动，仅勾选且硬币 ≥ 10 的类型参与比对，识别匹配到即购买并移除候选，避免重复购买；横幅改用 OCR 识别标题文字（日文/简中双候选），活动扭蛋以「期限」定位，已删除弃用的扭蛋横幅模板。
- 商店每日兑换：推荐物品改用 OCR 识别「おすすめ/推荐」并将点击位置下移到物品图标；免费刷新改用 OCR 识别「無料/免费」并限定刷新按钮区域；修正角色碎片商品配置错误；商店购买日志统一商品显示名并仅在命中时输出。
- 安排工作：自动时长按迷你演唱会/直播活动拆分为 `WorkTimeAutoShowFlag`、`WorkTimeAutoLiveFlag` 两个节点，修正复用节点导致的日志与实际设置不一致；修正寻找笑脸时滑动幅度不足、最右侧角色识别不到的问题；一键日常预设的迷你演唱会/直播活动时长默认改为「自动」。
- 社团互动：调整已请求标记的识别阈值（`0.95` → `0.93`），提高 DMM 端识别成功率。
- 自动培育：新增篠澤広(め) 卡片数据与分支，日文/中文任务的筱泽广默认卡片由「ガラクタロード/荆棘之路」切换为「め/Me」；`ProduceNIA` 的 `next` 也加入 `[JumpBack]ProduceChooseStrengthenFlag`，进一步避免强化选择环节中断。
- 偶像卡识别：`agent/custom/reco/produce.py` 新增 `normalize_text`，相似度比较前先做 NFKC 归一化并只保留字母、数字、假名与汉字，抹平 `℃`/`°C`、`･`/`・` 等符号差异；阈值抽为 `SIMILARITY_THRESHOLD`。
- 启动：`agent/main.py` 读取 `interface.json` 版本改为依次尝试传入路径、`./interface.json` 与 `./assets/interface.json`，兼容仓库调试与发布包两种布局；读取失败统一捕获异常并回退 `unknown`；MaaFW 库版本日志级别由 `debug` 提升为 `info`。

待实现或未完全完成的内容包括：

- 初 `LEGEND` 培育适配。
- 更多语言与更多自动培育样本覆盖。

截至最近更新本文件时，工作区存在未提交修改（v1.5.1 发版准备）：

- `assets/interface.json`
- `assets/resource/announcement/01_更新公告.md`
- `README.md`

不要覆盖或回退这些文件中的现有改动，除非用户明确要求。

## 目录职责

- `agent/`：Python 自定义逻辑扩展，供 MaaFramework 的 Custom recognition/action 调用。
- `agent/custom/action/produce.py`：自动培育事件、商店、选项等自定义动作逻辑，近期改动集中在事件优先级系统、`ProduceChooseEventBase` 基类提取、保底选择机制和试镜难度降低。
- `assets/resource/base/pipeline/`：MaaFramework 任务流水线。自动培育通用核心逻辑在 `Produce.json`，NIA 相关流程在 `ProduceNIA.json`，共用节点在 `ProduceUtils.json`。
- `assets/resource/base/image/` 或相邻资源目录：模板匹配、图像识别所需素材。
- `assets/data/`：结构化数据，例如偶像卡片数据 `idols_cards.json`。
- `assets/tasks/`：MFA/MaaFramework 任务入口与选项定义。培育任务入口在 `assets/tasks/produce.json`，中文任务配置在 `produce_cn.json`。
- `assets/lang/`：界面与任务选项翻译。新增任务选项时同步 `zh-CN` 和 `zh-Hant` 等已有语言。
- `assets/resource/announcement/01_更新公告.md`：发布给用户看的版本更新公告；当前内容已进入 v1.5.1 说明。
- `assets/resource/announcement/`：客户端资源公告与首次使用欢迎弹窗。文件按名称数字前缀排序展示（`01_更新公告.md`、`02_常见问题.md`、`03_免责声明与使用须知.md`、`04_欢迎使用.md`、`05_功能介绍.md`）；`04_欢迎使用.md` 被 `assets/interface.json` 的 `welcome` 字段引用；`05_功能介绍.md` 为功能说明.md 的简化版公告，冲突时以功能说明.md 与任务配置为准；`02_常见问题.md` / `03_免责声明与使用须知.md` 为长期静态公告。
- `docs/zh_cn/`：中文用户与开发文档。
- `tools/`：维护脚本，例如 README 中提到的偶像素材或卡片数据更新脚本。
- `debug/`：运行日志和调试输出，不应作为功能改动的一部分提交。
- `deps/`、`install/`：依赖和打包相关内容，修改时需确认发布影响。

## 开发环境

- Python 版本：`>=3.12`。
- Python 依赖：`maafw`、`loguru`、`Pillow`。
- 可选开发依赖：`pytest>=7.0`、`ruff>=0.1.0`。
- Node 侧仅用于工具链，当前 `package.json` 包含 `prettier-plugin-multiline-arrays`。
- Python 包版本信息在 `pyproject.toml`，当前仍为 `1.3.8`；用户可见资源公告已更新到 `assets/resource/announcement/01_更新公告.md` 的 `v1.5.1`。

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
- Markdown 文档没有强制 lint 配置（`docs/.markdownlint.yaml` 已删除），保持与现有文档一致的简洁风格；根目录 `AGENTS.md` 主要服务代理协作，优先清晰准确。
- 修改 JSON、JSONC 或流水线文件时保持原有排序、注释风格和缩进风格；不要做无关格式化。
- 新增用户可见文案时优先使用中文；涉及游戏内名称时保留日文原名，并在已有数据结构支持时补充中文字段。

## MaaFramework 流水线规则

- 先理解节点的 `recognition`、`action`、`next`、`timeout`、`pre_delay`、`post_delay`、`post_wait_freezes` 与 `focus`，再改流水线。
- `DirectHit` 节点通常用于流程入口或无条件跳转，不要随意替换成模板识别。
- `[JumpBack]` 节点用于在循环中回退重试，修改 `next` 顺序时要考虑优先级和误触风险。
- `TemplateMatch` 应明确模板路径、ROI、阈值和必要的匹配方法。新增模板时使用与现有资源一致的分辨率基准。
- `OCR` 只在文本稳定、语言明确时使用；游戏 UI 文案变动风险较高时优先保留模板或自定义识别。
- 商店识别已统一改为 OCR：扭蛋横幅、免费刷新、推荐物品同时配置日文与简中汉化候选，并可用 `replace` 纠正 OCR 易错字；改动文案时保持两套候选同步。
- `Custom` recognition/action 名称必须与 `agent/` 中实现一致，参数结构要向后兼容。
- 自动培育相关改动风险较高。修改 `Produce.json` 时重点验证：
  - 入口与中断继续流程：`Produce`、`ProduceLoop`、`ProduceSkipPreparation`、`ProduceEntry`。
  - 难度入口：`初` 走 `ProduceEntry`，`NIA` 走 `ProduceEntryNIA`；不要把 NIA 覆盖项误合到初流程。
  - 准备阶段：难度、偶像、支援、回忆、道具选择。
  - 培育阶段：事件选择、卡牌选择、饮料、道具、商店、强化、考试失败和结束流程。
  - 失败处理：初流程的 `ProduceFailedFlag` 和 NIA 流程的 `ProduceNIAFailedFlag` 均可根据 `启用培育失败重试` 跳转到重试或停止流程。
  - 试镜难度降低：NIA 模式通过 `ProduceMirrorFlag` 节点的 `custom_action_param` 控制降档逻辑，`启用试镜难度降低` 选项覆盖该参数。
  - NIA 事件参数：每张卡片通过 `ProduceChooseNIAEventFlag.custom_action_param` 设置 `effect`、`first`、`second`，字段顺序和语义都要保持一致。
  - 弹窗和通用按钮处理：不要扩大 ROI 到容易误触的位置。

## 任务配置规则

- 培育任务定义在 `assets/tasks/produce.json`，中文版本在 `assets/tasks/produce_cn.json`；新增或重命名选项时两边都要同步。
- 当前培育选项包括 `培育难度`、`培育偶像`、`培育次数`、`使用体力药`、`使用道具`、`跳过选择偶像`、`启用自动支援卡`、`启用自动回忆`、`启用关注租借`、`启用培育失败重试`、`跳过准备阶段`、`卡片选择优先级`、`跟随老师的建议`；`NIA` 难度下另有 `启用试镜难度降低` 与 `试镜手动接管`。
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
- README、功能说明与更新公告若存在冲突，先检查最近提交和 `assets/tasks/produce.json`；当前 NIA 状态应以 v1.5.1 更新公告和任务配置为准。
- 不要改变项目许可证、免责声明或商业用途限制。
- 需要联网查询 MaaFramework、MFAAvalonia、Mirror 酱或 OpenAI 等外部信息时，优先使用官方文档，并在回复中说明来源。
- 对用户报告的运行问题，优先索要或检查 `debug/maa.log`、模拟器类型、分辨率、系统平台、游戏版本、是否 DMM/插件版汉化。
