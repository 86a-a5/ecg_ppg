# ECG / PPG 采集与血压预测上位机

日常使用：双击 **start_gui.bat**，默认进入 BLE 蓝牙并搜索兼容设备。
选择 ATK-BLE04 后点“连接”，即可显示 ECG、IR、RED 三路同步波形。
PPG 显示默认“自动（MAX30102预设）”，以反相预设显示向上主波；
可在“信号状态→PPG显示极性”中切换反相/原光强方向，原始CSV不受影响。
USB-TTL 可切换“USB 串口”；自动连接 COM13 仍可双击 **start_com13.bat**。
窗口内包含三路采集波形和 UniCardio 血压预测页面。

## 文件分类

```text
host_pc/
├─ start_gui.bat          常用启动入口
├─ start_com13.bat        自动连接 COM13
├─ launch_gui.py          启动器与环境检查
├─ run.py                 命令行统一入口
├─ requirements*.txt     界面和模型的依赖清单
├─ src/ecg_ppg/           正式源码
│  ├─ acquisition/       BLE / 串口传输、二进制协议、采样记录、CSV 导出
│  ├─ gui/               界面、波形显示、显示滤波
│  ├─ algorithms/        信号处理、特征提取、传统血压估计
│  ├─ models/            UniCardio 输入处理、推理、后台进程
│  ├─ tools/             模拟数据、历史回放、实机验证工具
│  └─ paths.py           项目路径统一管理
├─ tests/                自动化测试
├─ docs/                 使用手册、采集联调、滤波与模型说明
├─ records/              原有采集 CSV 和分析记录
├─ outputs/              验证图片、报告、模型日志
└─ archive/              旧代码备份与整理前缓存
```

隐藏的 `.git`、`.venv`、`.idea` 分别用于版本管理、运行环境、编辑器配置，保持原位。
外部 UniCardio 模型目录和权重保持原位。

## 常用命令

在本目录的 PowerShell 中运行：

```powershell
python run.py gui
python run.py gui --predict-csv outputs/link_validation/live_capture.csv
python run.py extract records/synth_rest.csv --plot
python run.py unicardio records/synth_rest.csv --max-segments 1
python run.py test
python launch_gui.py --doctor
```

工具也通过 `run.py` 选择：`synth` 生成模拟数据，`replay` 回放记录，`verify` 验证实机采集。
Python 源码使用包导入，请通过以上入口启动。

## 文档与结果

- [使用手册](docs/使用手册.md)
- [ATK-BLE04 蓝牙采集与同步绘图](docs/ATK-BLE04蓝牙采集.md)
- [正式工程 BLE 实机验证（2026-10-25）](docs/BLE接入验证_20261025.md)
- [PPG 极性显示修正与验证（2026-10-28）](docs/PPG极性显示修正_20261028.md)
- [采集联调说明](docs/采集联调说明.md)
- [显示滤波优化说明](docs/显示滤波优化说明.md)
- [波形链路修复与离线验证（2026-10-23）](docs/波形链路修复_20261023.md)
- [UniCardio 接入说明](docs/UniCardio接入说明.md)
- [本次目录整理与回退](docs/目录整理说明.md)

历史实采预测图位于 `outputs/bp_validation/historical_prediction.png`。
模型结果需要与参考血压对照验证；当前 CPU 推理约需 21–23 秒处理一个 4 秒片段。

## Git

Git 根目录在组合仓库 `ecg_ppg`，包含本上位机和 `stm32f4/ECG_PPG` 下位机。
原开发目录中的历史分支与标签未迁入本次源码快照。
原始采集数据、验证输出、模型权重、虚拟环境与旧备份不加入 Git。
