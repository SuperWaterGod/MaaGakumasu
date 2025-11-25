<!-- markdownlint-disable MD033 MD041 -->

<p align="center">
  <img alt="LOGO" src="https://github.com/user-attachments/assets/d22b9752-5b71-4423-95c7-30f9eb0b2923" width="256" height="256" />
</p>

<div align="center">

# MaaGakumasu

基于全新架构的 **学園アイドルマスター(学マス)** 小助手。图像技术 + 模拟控制 + 深度学习，解放双手！  
由 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 强力驱动！

✨ 如果喜欢 MaaGakumasu，欢迎在项目右上角点亮 Star 支持 ✨
</div>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blueviolet">
  <img alt="CodeFactor" src="https://www.codefactor.io/repository/github/superwatergod/maagakumasu/badge">
  <img alt="Yolo" src="https://img.shields.io/badge/Yolo-v11-blue">
  <br>
  <img alt="license" src="https://img.shields.io/github/license/SuperWaterGod/MaaGakumasu">
  <img alt="commit" src="https://img.shields.io/github/commit-activity/m/SuperWaterGod/MaaGakumasu">
  <img alt="stars" src="https://img.shields.io/github/stars/SuperWaterGod/MaaGakumasu?style=social">
  <img alt="downloads" src="https://img.shields.io/github/downloads/SuperWaterGod/MaaGakumasu/total?style=social">
  <a href="https://mirrorchyan.com/zh/projects?rid=MaaGakumasu" target="_blank"><img alt="mirrorc" src="https://img.shields.io/badge/Mirror%E9%85%B1-%239af3f6?logo=countingworkspro&logoColor=4f46e5"></a>

</p>


## 功能列表

- 目前已实现

- [x] 启动游戏
- [x] 领取活动费

- [x] 社团互动
  - [x] `自动/指定` 请求
- [x] 安排工作
  - [x] 领取奖励
  - [x] `自动/指定` 选择偶像
  - [x] 指定时长
- [x] 商店购买
  - [x] 扭蛋购买
  - [x] 金币购买
  - [x] 自动免费刷新
  - [X] AP购买
- [x] 领取邮箱礼物
- [x] 领取任务奖励
- [x] 自动培育(测试阶段)
  - [x] 初 `REGULAR/PRO/MASTER` 难度
  - [x] 指定SSR偶像
  - [x] 自动选择
  - [x] **中断继续**
- [x] 支持从Mirror酱更新

详细内容见 [功能说明](docs/zh_cn/功能说明.md)

- 
  等待实现
- [ ] [新]竞赛挑战
  - [ ] 指定挑战
  - [ ] 自动选择
- [ ] 支持Kuyo版汉化
- [ ] 强化支援卡
- [ ] …………

## 注意事项

> [!NOTE]  
> 大部分测试都是在 Windows 系统上测试的，因此其他操作系统若有运行问题，请提 Issues 或加群讨论。  
> 开发是基于 MuMu12 模拟器测试的，因此推荐使用 MuMu12 运行游戏。
> 其他模拟器若出现问题，请第一时间把脚本根目录下`debug\maa.log` 文件保存并截图进行反馈。

1. 默认用户的操作系统为 Windows 系统，其他平台未经测试
2. 推荐使用`MuMu模拟器12`运行游戏，[模拟器支持情况](https://maa.plus/docs/zh-cn/manual/device/windows.html)请查看官方文档。
3. 模拟器分辨率建议设置为`1280*720(240DPI)`，其他`16:9`分辨率未经过详细测试。
4. **DMM版**本暂不支持，**Kuyo汉化**版未来会支持
5. 本项目部分功能使用了 `深度学习Yolov11` 模型进行识别，请确保电脑有显卡且开启GPU加速。
6. 本项目仅用于学习交流，请勿用于商业用途，否则后果自负。
7. 本项目仅提供脚本，不提供任何游戏资源，如需要游戏资源，请自行购买。


## 使用说明

### Windows

- 对于绝大部分用户，请下载 `MaaGakumasu-win-x86_64-vXXX.zip`
- 若确定自己的电脑是 arm 架构，请下载 `MaaGakumasu-win-aarch64-vXXX.zip`

请注意！Windows 的电脑几乎全都是 x86_64 的，可能占 **99.999%**，除非你非常确定自己是 arm，否则别下这个！

解压后运行 `MFAAvalonia.exe` 即可。

压缩包已自带`Python 3.12.9`环境，无需额外安装。

首次启动，将会自动安装相关依赖

如果无法运行则按照如下的方案尝试解决:

> MAA 本家提供的解决方案 ⬇️
>
> 请安装 [`Visual C++ 可再发行程序包`](https://aka.ms/vs/17/release/vc_redist.x64.exe) 和 [`.NET 桌面SDK 10`](https://dotnet.microsoft.com/zh-cn/download/dotnet/10.0) 并 **重新启动计算机** 。
>
> 推荐使用 Windows 10 或 11 的用户使用 winget 工具进行安装，只需在终端中运行以下命令。
>
> ```
> winget install Microsoft.VCRedist.2015+.x64 Microsoft.DotNet.DesktopRuntime.8
> ```

### MacOS

- 若使用 Intel 处理器，请下载 `MaaGakumasu-macos-x86_64-vXXX.zip`
- 若使用 M1, M2 等 arm 处理器，请下载 `MaaGakumasu-macos-aarch64-vXXX.zip`
- 解压缩之后，右击文件夹，点击“新建位于文件夹位置的终端窗口”
- 在终端窗口内逐行输入以下指令

  ```
  chmod a+x MaaGakumasu
  ./MaaGakumasu
  ```
  > 注：需要安装.NET 运行库（使用上面的命令启动失败时会直接返回下载地址）

- Mac 可能会提示：因为 Apple 无法检查其是否包含恶意软件

此时进入选择   设置  -  隐私与安全性，下方出现“已阻止…”点击   仍要打开。
多重复以上步骤，因为有很多文件会被检查。


### Linux

Linux大佬自有办法


## 开发相关

- 等待完善

### 深度学习YOLOv11训练相关

主要用于 `自动培育` 相关功能

模型训练参考[MFW](https://github.com/MaaXYZ/MaaNeuralNetworkCookbook/tree/main/NeuralNetworkDetect)官方文档

数据集标注使用 [roboflow](https://app.roboflow.com/gakumasu) 网站

- `cards` 集用于识别*出牌*
- `button` 集用于识别*上课/冲刺选项*

> [!IMPORTANT]
> `cards` 集 目前405张样本，均已标注完成。基本满足当前需求
> 
> `button` 集 目前仅28张样本，均已标注完成。**目前未能完全满足当前需求**，部分场景下识别率较低。
> 
> 若您有兴趣参与标注或者提供样本，欢迎联系开发组。

详细内容见 [开发相关](docs/zh_cn/开发相关.md)

### Mirror酱相关

>  自2025/11/08起，MaaGakumasu已全面支持Mirror酱，在其他地方购买的CDK同样可以在此使用。

Mirror酱是一个第三方应用分发平台，让开源应用的**更新**更简单。

用户付费使用，收益与开发者共享。此外，Mirror酱本身也是开源的。

CDK 购买连接: [官网](https://mirrorchyan.com/zh/projects?rid=MaaGakumasu)

## 免责声明

本软件开源、免费，仅供学习交流使用。若您遇到商家使用本软件进行代练并收费，可能是分发、设备或时间等费用，产生的费用、问题及后果与本软件无关。

**在使用过程中，MaaGakumasu 可能存在任何意想不到的问题，因 MaaGakumasu 自身漏洞、文本理解有歧义、异常操作导致的账号问题等开发组不承担任何责任，请在确保在阅读完用户手册、自行尝试运行效果后谨慎使用！**


## Star History

<a href="https://www.star-history.com/#SuperWaterGod/MaaGakumasu&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=SuperWaterGod/MaaGakumasu&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=SuperWaterGod/MaaGakumasu&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=SuperWaterGod/MaaGakumasu&type=Date" />
 </picture>
</a>

## 鸣谢

本项目由 **[MaaFramework](https://github.com/MaaXYZ/MaaFramework)** 强力驱动！
UI 由 [MFAAvalonia](https://github.com/SweetSmellFox/MFAAvalonia) 大力支持！

感谢以下开发者对本项目作出的贡献:

[![Contributors](https://contrib.rocks/image?repo=SuperWaterGod/MaaGakumasu&max=1000)](https://github.com/SuperWaterGod/MaaGakumasu/graphs/contributors)

## Join us
- MaaGakumasu 交流群 QQ 群：还没建立
- MaaFramework 开发交流 QQ 群: 595990173