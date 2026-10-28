# ECG + PPG 正式下位机

STM32F411CEU6，CubeMX + HAL。AD8232 200Hz、MAX30102 RED/IR 100Hz；共同 TIM5 微秒时基；12 字节二进制帧经 USART2 DMA → ATK-BLE04 → PC BLE 实时绘图。PC 工程在仓库内 `host_pc`，见其 `docs/ATK-BLE04蓝牙采集.md`。

## 接线

| 信号 | F411 |
|---|---|
| AD8232 OUTPUT | PA0 |
| MAX30102 SCL / SDA | PB6 / PB7 |
| ATK-BLE04 RXD | PA2（USART2 TX）|
| ATK-BLE04 TXD | PA3（预留，当前 UART 仅 TX）|
| 三个模块供电 | 3.3V 电源板，和 F411 共地 |

ATK-BLE04 为带底板版本，UART 保持 115200 / 8N1 / 无流控；STA、WUP 悬空。当前不接 CH340 数据线。ST-Link 保持原有 SWDIO、SWCLK、GND 和 F411 供电接线，无 NRST。

固定使用板载 **25MHz HSE**，不静默回退 HSI。上电即初始化两个传感器并持续采集上传；MAX30102 初始化失败会进入 `Error_Handler`。模块的 ASCII 连接状态不会混入上传帧。BLE 在模块内完成，STM32 无需运行 BLE 协议栈或恢复历史 `HM10.c`。

## 构建与下载

```powershell
cmake --preset Debug
cmake --build --preset Debug
```

产物：`build/Debug/ECG_PPG.elf`、`.hex`、`.bin`。CLion 选择共享运行配置 **ECG_PPG_SWD_100kHz**，使用 `openocd_stm32f411.cfg`；它针对没有 NRST 的接线使用软件复位。

命令行（把工具路径替换为本机安装位置）：

```powershell
openocd -s <OpenOCD脚本目录> -f openocd_stm32f411.cfg -c "program build/Debug/ECG_PPG.elf verify reset exit"
```

采集帧与队列统计见 `BSP/Inc/ecg_ppg_stream.h`；`STREAM_GetStats` 可查看发送丢帧、UART 错误和 FIFO 溢出。PPG 时间戳是基于 FIFO 读取时刻的估计，不能当作已标定的精确 PAT 时序。

重新从 CubeMX 生成后，检查 USART2 TX 模式、DMA1 Stream6 Channel4、USART2/DMA 中断及 HSE25MHz，保留 BSP 和主循环采集逻辑。ADC DMA 与 USART DMA 不同，不要把两者配置混淆。
