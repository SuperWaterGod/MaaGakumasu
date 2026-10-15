# 训练与实验代码

`research/` 集中保存训练、策略实验、样本验证和可复用回归代码。当前培育实验在 `produce/`，尚未实现策略模型训练。

## 职责边界

| 目录 | 内容 |
| --- | --- |
| `agent/` | 客户端动作、采集监听、后台识别及运行日志所需代码 |
| `tools/` | 项目构建、CI、翻译、正式数据与资源同步等维护代码 |
| `research/produce/` | 独立采样、离线报告、证据重放、模板取样和合成数据回归 |
| `debug/` | 本地截图、日志、数据集、实验输出和一次性排查脚本，Git 忽略 |

实验代码可以调用 `agent` 的读取器和数据结构，客户端不能反向依赖 `research`。`agent/produce/analysis.py` 同时用于运行时后台识别，继续归客户端管理；技能卡和奖励库同步脚本生成正式资源，继续放在 `tools/`。`research/` 不进入现有 Agent 发布包，无需修改打包配置。

## 培育实验入口

以下命令从仓库根目录执行，`python` 可换成 beta 的内置 Python 路径：

```powershell
python research/produce/report_produce_session.py <会话目录> --capture-root <采集目录> --output debug/research/produce/report
python research/produce/report_produce_session.py <会话根目录> --all --capture-root <采集目录> --output debug/research/produce/summary
python research/produce/report_decision_capture.py <单个采集目录>
python research/produce/replay_reward_effects.py <单个采集目录> --database assets/data/produce_skill_cards.json
python research/produce/build_buff_templates.py --capture-root <采集目录> --preview debug/research/produce/buff_preview.png
python research/produce/test_reporting.py
```

`capture_battle.py` 是显式启动的只读 ADB 旁路采样入口，参数见 `--help`；不会因生成报告或运行测试而启动。它使用可选开发依赖 OpenCV，模板取样使用 Pillow，均不增加客户端运行依赖。详细操作见 [出牌数据采集](../docs/zh_cn/出牌数据采集.md)。

回归测试使用合成事件和文件，不连接设备，临时素材写入 `debug/research/produce/tests/`，校验路径后由测试清理。已有轮次及历史派生结果保持原位。迁移前 `tools/` 中的六个实验入口已移到本目录，统一使用新路径，不保留转发文件。
