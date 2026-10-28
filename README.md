# ECG + PPG 蓝牙同步采集系统

STM32F411CEU6 + AD8232 + MAX30102 + ATK-BLE04，实现 ECG、红光 PPG、红外 PPG 的连续采集、BLE 上传、同步绘图和原始 CSV 记录。

本仓库包含正式上位机和下位机源码，不包含实验参与者数据、采集记录、虚拟环境、编译产物、厂商资料压缩包或模型权重。

## 工程结构

```text
ecg_ppg/
├── host_pc/                  Python 上位机、算法接口与测试
│   ├── src/ecg_ppg/
│   │   ├── acquisition/      BLE/串口接收、二进制解析与记录
│   │   ├── gui/              Tk/Matplotlib 波形、显示滤波与极性
│   │   ├── algorithms/       信号处理、特征与传统血压估计
│   │   ├── models/           可选 UniCardio 进程桥接与预处理
│   │   └── tools/            合成数据、离线回放、链路验证
│   └── tests/
├── stm32f4/ECG_PPG/          CubeMX + HAL + CMake 下位机
│   ├── Core/                 时钟、ADC、DMA、TIM、USART
│   ├── BSP/                  AD8232、MAX30102、帧队列
│   ├── Drivers/              STM32 HAL 与 CMSIS（含许可证）
│   ├── cmake/                构建及 ARM 工具链配置
│   └── tests/                FIFO 恢复逻辑的本地测试
└── third_party/              第三方许可补充
```

## 硬件与数据链路

| 功能 | 配置 |
|---|---|
| 主控 | STM32F411CEU6，25MHz HSE |
| ECG | AD8232 OUTPUT → PA0，TIM2 触发 ADC/DMA，200Hz |
| PPG | MAX30102 SCL → PB6，SDA → PB7，RED+IR 100Hz |
| 蓝牙串口 | PA2 TX → ATK-BLE04 RXD，115200 / 8N1 / 无流控 |
| 预留接收 | PA3 ← ATK-BLE04 TXD，当前固件仅启用 UART TX |
| 电源 | 模块使用适配的3.3V电源，和 F411 共地 |
| 时间基准 | TIM5 1MHz；PPG 时间戳由 FIFO 轮询估计 |
| 下载 | ST-Link SWD；配置支持无 NRST 的软件复位 |

连接人体 ECG 电极时，整套采集电路使用独立电池供电，断开通往电脑和插电电源的 USB、ST-Link、充电线，仅保留无线通信。开发板与算法属于研究原型，血压估计结果尚未作为临床测量验证。

BLE 服务为 `0000fff0-0000-1000-8000-00805f9b34fb`，通知特征为 `0000fff1-0000-1000-8000-00805f9b34fb`。上位机直接使用 Bleak GATT 接收，不需要蓝牙虚拟 COM 口；同时保留 USB-TTL 串口入口。

每帧12字节，小端：`AA | 类型 | 序号 | 值1:uint16 | 值2:uint16 | 时间戳:uint32 | 55`。
ECG 类型 `01`，值2为 `FFFF`；PPG 类型 `02`，值1为RED、值2为IR，18位光强右移2位上传。
共享8位序号用于估计连续性，32位微秒时间戳在上位机展开回绕。协议没有CRC，8位序号也不能识别所有整256帧丢失；显示“0丢帧”只是运行诊断。

## 上位机启动

建议使用带 Tcl/Tk 的 Python 3.10 或更新版本；Windows 为当前主要验证平台。

```powershell
cd host_pc
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launch_gui.py --doctor
.\.venv\Scripts\python.exe launch_gui.py
```

Windows 也可双击 `host_pc/start_gui.bat`。选择“BLE 蓝牙”→搜索设备→连接，可查看 ECG / IR / RED 三路共同时间轴波形，并录制、导出原始 CSV。

PPG 原始数据是接收光强，收缩主波可能向下。“信号状态→PPG显示极性”提供自动、MAX30102反相和原光强三种模式；自动模式默认使用反相预设，可靠周期形态满足条件后才锁定。显示转换不改变原始记录和模型原始缓存。

可选模型需要自行准备 UniCardio 代码和权重，设置 `ECG_PPG_MODEL_DIR` 和（必要时）`ECG_PPG_MODEL_PY`，依赖见 `host_pc/requirements-unicardio.txt`。基础采集界面无需模型权重。

## 下位机构建与下载

准备 CMake ≥3.22、Ninja 和 ARM GNU Toolchain，确保 `arm-none-eabi-gcc`、`arm-none-eabi-objcopy` 在 PATH 中。

```powershell
cd stm32f4/ECG_PPG
cmake --preset Debug
cmake --build --preset Debug
```

输出在 `build/Debug/`：`ECG_PPG.elf`、`.hex`、`.bin`，均由本地构建生成。
可在 CLion 选择共享配置 `ECG_PPG_SWD_100kHz`，或使用已安装的 OpenOCD：

```powershell
openocd -s <OpenOCD脚本目录> -f openocd_stm32f411.cfg -c "program build/Debug/ECG_PPG.elf verify reset exit"
```

## 验证

上位机测试包含协议拆包、时间戳回绕、BLE断连/取消、原始记录、绘图、PPG极性和模型接口：

```powershell
cd host_pc
.\.venv\Scripts\python.exe run.py test
```

下位机 FIFO 单元验证需要主机原生 GCC（不是 ARM GCC）：

```powershell
cd stm32f4/ECG_PPG
python tests/test_fifo_recovery.py
```

没有附带真实采集数据；可用 `host_pc/run.py synth` 生成测试数据。历史文档中的本地记录、图像、模型和输出报告引用仅供理解，不保证这些被忽略的文件存在于克隆目录。

## 文档与许可

- [上位机说明](host_pc/README.md)
- [蓝牙采集与接线](host_pc/docs/ATK-BLE04蓝牙采集.md)
- [PPG极性修正](host_pc/docs/PPG极性显示修正_20261028.md)
- [下位机说明](stm32f4/ECG_PPG/README.md)
- [第三方许可说明](THIRD_PARTY_NOTICES.md)

第三方代码保留原始版权与许可证。项目自有代码暂未指定统一的开源许可证，不将第三方组件重新声明为项目自有代码。
