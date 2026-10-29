# 训练与实验代码

`research/` 集中保存训练、策略实验、样本验证和可复用回归代码。当前培育实验在 `produce/`，尚未实现策略模型训练。

## 职责边界

| 目录 | 内容 |
| --- | --- |
| `agent/` | 客户端动作、采集监听、同步识别及运行日志所需代码 |
| `tools/` | 项目构建、CI、翻译、正式数据与资源同步等维护代码 |
| `research/produce/` | 独立采样、离线报告、证据重放、模板取样和合成数据回归 |
| `../MaaGakumasuLab/debug/` | 实验截图、数据集、回归验证和一次性排查脚本，Git 忽略；客户端正常运行原始记录留在客户端 |

实验代码可以调用 `agent` 的读取器和数据结构，客户端不能反向依赖 `research`。`agent/produce/analysis.py` 同时用于运行时同步识别，继续归客户端管理；技能卡和奖励库同步脚本生成正式资源，继续放在 `tools/`。`research/` 不进入现有 Agent 发布包，无需修改打包配置。

## 培育实验入口

以下命令从仓库根目录执行，`python` 可换成 beta 的内置 Python 路径：

```powershell
python research/produce/report_produce_session.py <会话目录> --output ../MaaGakumasuLab/debug/research/produce/report
python research/produce/report_produce_session.py <会话根目录> --all --output ../MaaGakumasuLab/debug/research/produce/summary
python research/produce/report_decision_capture.py <单个采集目录>
python research/produce/build_buff_templates.py --capture-root <采集目录> --preview ../MaaGakumasuLab/debug/research/produce/buff_preview.png
python research/produce/analyze_choices.py <已验收报告根目录> --output ../MaaGakumasuLab/debug/research/produce/baseline
python research/produce/test_reporting.py
```

`capture_battle.py` 是显式启动的只读 ADB 旁路采样入口，参数见 `--help`；不会因生成报告或运行测试而启动。它使用可选开发依赖 OpenCV，模板取样使用 Pillow，均不增加客户端运行依赖。详细操作见 [出牌数据采集](../docs/zh_cn/出牌数据采集.md)。

回归测试使用合成事件和文件，不连接设备，临时素材写入 `../MaaGakumasuLab/debug/research/produce/tests/`，校验路径后由测试清理。已有轮次及历史派生结果保持原位。迁移前 `tools/` 中的六个实验入口已移到本目录，统一使用新路径，不保留转发文件。

`analyze_choices.py` 读取已经验收的 `*/data/report.json`，输出 `baseline.json` / `baseline.md`。仅纳入正常结束且 `complete=true` 的轮次，重复会话去重、跨会话重复分段排除；按剧本、难度、配置偶像、歌曲与完整代码指纹分组。事件按候选出现的决策次数、奖励按每页唯一库条目统计选择频率，预览重试不增加分母。身份未知、选择未核对和未采集字段分别保留，不把选择输入当作实际获得。

描述性对照可按已记录的相同任务配置和策略源码指纹寻找样本，完整采集版本仍单列。该对照只分解端点评分与属性差异，不把随机候选、战斗过程或未知状态的影响归因于某次选择，也不生成策略训练标签或修改客户端策略。输入报告路径、文件 SHA 与原报告来源保存在输出中；实际轮次的分析结论只写入 `../MaaGakumasuLab/debug/`。

单轮回放和审计实现已下沉到 `agent/produce/reporting.py`、`audit.py`，本目录保留命令入口与跨轮统计；只读取当前 schema 4 会话格式。截图关闭时不保存全图，无法事后重新 OCR，但结构化派生结果仍可用于报告和描述性统计。

客户端只保存原始事件、合并识别结果流和用户回放，不自动写重复展开的 `data/report.json`。先显式运行 `report_produce_session.py` 导出研究报告，再使用 `analyze_choices.py`。同步识别结果为 `analysis/results.jsonl`，事件引用同流中的已有结果；没有每帧 JSON 和独立 `index.jsonl`。

## 保留与清理

- `report_produce_session.py`：整轮导出与可选结算重读，复用客户端报告核心。
- `produce_reporting.py`、`analyze_choices.py`：跨轮汇总和描述性基线分析；不是客户端的第二套回放。
- `capture_battle.py`、`report_decision_capture.py`：独立 ADB 旁路采样及其单段报告，后者仍由前者调用，不能直接删除。
- `build_buff_templates.py`：正式识别模板的离线取样工具。
- `test_recording.py`、`test_reporting.py`：可复用回归，保留。
- 旧奖励效果重放工具及客户端加载分支已删除：当前奖励只识别身份，不采集效果正文；历史实验产物不删除。

源码指纹字段仅在研究输入实际提供时用于分组；缺失时标为未知，不能用当前源码替历史记录补造版本，也不能据此声称已完成可信策略基线。

## 回归测试能证明什么

| 范围 | 具体检查 | 不能替代的验证 |
| --- | --- | --- |
| 记录与会话 | 目录、续跑、零值/未知、写盘异常、截图开关、图像引用 | 实机中断/重启及连续多轮 |
| 识别与缓存 | 合成边框、四候选、恢复选中、模板冲突、损坏、分类及变体分组 | 各设备真实画面与活动布局覆盖 |
| 日志 | 字符串含标题/详情/箭头、不会拆成多行、Pipeline 操作节点配置完成提示 | MFA 实际渲染、颜色、折叠、按钮打开 |
| 报告与审计 | 输入事件转为表格、图片折叠标记、缺口、重复事件、不同会话隔离 | Markdown 阅读器实际折叠行为 |
| 研究统计 | 分母去重、未知项保留、样本排除规则 | 策略收益或真实游戏效果 |

测试以合成事件、合成图像和模拟 Context 为主，部分模板/编解码测试调用真实 MaaFramework。子测试是同一用例的参数组合，不是多轮实机测试。测试通过只说明上述检查通过，不证明全部 UI、原生 Context 生命周期和长期运行行为正常。实机截图离线重放与真实设备运行须单独报告。
