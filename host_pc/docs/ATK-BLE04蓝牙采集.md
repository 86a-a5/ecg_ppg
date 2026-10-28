# ATK-BLE04 蓝牙采集与同步绘图

正式下位机为相邻目录 `stm32f4/ECG_PPG`，上位机为本 `host_pc` 工程。
旧的独立 PING 演示和吞吐压测工程保留，日常采集使用正式工程。

## 使用

1. 保持当前传感器与蓝牙接线，给模块和 F411 上电；关闭占用该模块的其他 BLE 软件。
2. 双击 `start_gui.bat`。默认“BLE 蓝牙”，启动后搜索约 6 秒。也可点击“搜索蓝牙设备”刷新。
3. 在设备下拉列表选择目标（名称、地址、RSSI），点击“连接”。地址以扫描结果为准（示例：`AA:BB:CC:DD:EE:FF`）。
4. 状态依次为连接、同步最新数据、ECG / PPG 两路持续接收。ECG、IR、RED 使用同一时间轴滚动显示；左侧显示采样率、序号丢帧、坏帧、界面丢点。
5. “开始录制”保存收到的原始采样，停止后“导出 CSV”。显示滤波不修改原始数据。已有 UniCardio 页面继续使用相同的同步数据。
6. 点击“断开”会结束连接并停止当前录制。断连后可重新搜索连接；不会自动跳到附近其他设备。

无需在 Windows 设置里先配对，也不使用蓝牙虚拟 COM 口。搜索范围为广播 FFF0 服务的设备，另兼容未广播 UUID 的 ATK-BLE04 名称；连接时再次验证 FFF0 / FFF1 通知服务。相同服务 UUID 只代表传输接口兼容，对端仍须运行本项目的 12 字节采集协议。

## 当前接线与参数

下面的 USB / ST-Link 接线适用于不接人体的工程调试。连接人体电极时，
整套采集电路应独立电池供电，断开通往电脑及插电电源的 USB、ST-Link 和充电线，
仅通过蓝牙通信。缺少合适供电条件时，保持电极离开身体，可先进行 ADC 电平自检。

| 设备 | 引脚 | 接到 |
|---|---|---|
| ATK-BLE04 排针底板 | VCC / GND | 3.3V 电源板 / 共同地 |
| ATK-BLE04 | RXD | F411 PA2（USART2 TX）|
| ATK-BLE04 | TXD | F411 PA3（当前固件未启用 UART 接收）|
| ATK-BLE04 | STA / WUP | 悬空 |
| AD8232 | 3.3V / GND / OUTPUT | 3.3V / 共同地 / PA0 |
| MAX30102 | VIN / GND / SCL / SDA | 3.3V / 共同地 / PB6 / PB7 |

模块 UART 与下位机均为 **115200、8N1、无流控**。BLE 模式的波特率控件禁用；BLE 通知没有 PC 串口波特率设置。CH340 数据线保持断开。

“信号状态”显示 ECG 原始 ADC 范围、估计 OUT 电压及接近量程上下限的点数比例。
电压按 3.3V ADC 参考估算，是 AD8232 放大后的模拟输出，并非人体 ECG 的输入幅度。
峰峰值、PPG 交流/DC 只用于观察输入；幅度大不能直接认定波形质量好，因此不再换算成“质量百分比”。

下位机使用实测正确的 **25MHz HSE**，启动即采集和上传。HSE 失败进入错误处理，避免静默回退到本板偏差较大的 HSI。USART2 仅 TX + DMA，模块发来的 `STA:connect` 等文本不进入二进制采集流。

## 数据与同步

### PPG 显示极性（2026-10-28）

MAX30102 原始值代表接收光强，常见的收缩期主波对应光强下降。
原先显示 `raw − DC` 且未取反，因而可能出现尖谷朝下、看起来“缓升快降”的形态。
现在“信号状态”内的 **PPG显示极性** 同时作用于 IR 和 RED：

- **自动（MAX30102预设）**：默认使用 ×−1；IR 信号有至少约4秒连续数据和至少4个可靠周期后，依据主波偏斜和周期间隔一致性判定方向。判定后锁定，重连或重新选择模式才重新判断。
- **反相（MAX30102）**：固定 ×−1，显示 `DC − raw` 对应的向上主波，适合当前模块。
- **原极性（光强）**：固定 ×+1，用于核对传感器原始光强方向。

转换在绘图末端执行，保留原来的 0.5–8Hz 实时滤波和轻度 SG(5,2) 平滑。
切换极性不重置滤波，不反转时间轴，不修改原始 CSV、DC、采样率、帧序号或模型原始缓存。
关闭 PPG 滤波时，极性选项仍作用于未低通的交流分量。

自动方向是显示用启发式判断，平线、近正弦、不规则噪声、接触突变或数据不足时不强行判定，界面会显示使用预设的状态。波形不清晰时可明确选择“反相（MAX30102）”进行对照。
ECG R峰后到PPG峰的时间差可辅助检查，但不把固定0.15–0.35秒当作适用于所有人的方向判据。
极性修正不会生成原始数据中缺少的收缩峰或重搏切迹，也不自动认定任意小波动就是切迹。

本次只修正采集页的显示约定；UniCardio和离线特征分析继续从原始数据走各自处理流程，
模型的极性约定须与训练数据核对，不能由一个显示开关隐式改变。

可用已有CSV进行离线对照，不连接人体或蓝牙：

```powershell
.\.venv\Scripts\python.exe run.py replay <原始CSV> --ppg-polarity original --output outputs/ppg_original
.\.venv\Scripts\python.exe run.py replay <原始CSV> --ppg-polarity inverted --output outputs/ppg_inverted --keep-open
```

### 链路与数据帧

- 服务 `0000fff0-0000-1000-8000-00805f9b34fb`，通知 `0000fff1-0000-1000-8000-00805f9b34fb`。
- BLE 通知可能拆开一帧或包含多帧；接收线程直接按字节喂给同一个增量 `FrameParser`，不做 UTF-8 解码。
- 帧保持 `AA | 类型 | 8位序号 | 两个16位值 | TIM5微秒时间戳 | 55`，12 字节，小端。ECG 200Hz，PPG RED+IR 100Hz，总计约 **3600B/s**。
- 绘图使用下位机 TIM5 时间戳和共同的六秒窗口，不用蓝牙到达时间给每个点打时间戳。PPG FIFO 时间戳仍是轮询估计，尚未完成精确 PAT 时序标定。
- 透明模块可能缓存断连期间的串口数据。每次连接先同步约一秒（从首个通知计时），解析并舍弃这段数据，保持跨通知的半帧拼接；随后才交给绘图、录制和模型。测量期统计重新开始；验证报告单独保存同步期字节数及统计，避免旧数据污染新会话。
- 丢帧是根据 8 位序号估计，不能识别所有整 256 帧丢失，帧格式也没有 CRC；显示 0 丢帧不等于任意条件下绝对无丢包。此前独立压测有固件计数与完整流对照，日常 GUI 提供运行诊断。

## 环境与复现

启动器优先使用 `ECG_PPG_GUI_PY` 指定环境或项目 `.venv`，再查系统解释器。当前 `.venv` 已安装并验证 GUI 与 Bleak 依赖。重新配置时使用带 tkinter 的 Python：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launch_gui.py --doctor
.\.venv\Scripts\python.exe launch_gui.py
```

命令行自动搜索连接指定模块、实机验证：

```powershell
.\.venv\Scripts\python.exe launch_gui.py --ble-address AA:BB:CC:DD:EE:FF
.\.venv\Scripts\python.exe run.py verify --ble-address AA:BB:CC:DD:EE:FF --seconds 60 --output outputs/ble_validation
.\.venv\Scripts\python.exe run.py test
```

验证报告保存真实采样 CSV、三路波形 PNG、速率和协议统计 JSON。实机验证时只保留一个客户端占用蓝牙模块。API 用法参考 [Bleak 扫描文档](https://bleak.readthedocs.io/en/latest/api/scanner.html) 与 [连接/通知文档](https://bleak.readthedocs.io/en/latest/api/client.html)。
