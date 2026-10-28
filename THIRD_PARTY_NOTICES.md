# 第三方组件

本仓库仅整理项目实际使用的源码，保留各文件原始版权声明。项目自有代码尚未指定统一的开源许可证。

| 组件 | 位置 | 许可说明 |
|---|---|---|
| STM32F4 HAL | `stm32f4/ECG_PPG/Drivers/STM32F4xx_HAL_Driver` | 保留 ST 原始声明及 LICENSE.txt；按该文件对缺少独立包协议的情况使用 BSD-3-Clause，完整条款补充于 `third_party/licenses/` |
| Arm CMSIS | `stm32f4/ECG_PPG/Drivers/CMSIS` | Apache-2.0，完整原文在对应 LICENSE.txt |
| STM32F4 CMSIS Device | `stm32f4/ECG_PPG/Drivers/CMSIS/Device/ST/STM32F4xx` | 保留 ST 原始许可说明，依其缺省条款采用 Apache-2.0 |
| OLED 驱动及字模 | `stm32f4/ECG_PPG/BSP` 中相关 OLED 文件 | 保留文件头中的江协科技版权及共享声明；不统一改为 MIT 或其他许可证 |
| Python 依赖 | `host_pc/requirements*.txt` | 通过包管理器安装，不随仓库复制其安装包；各依赖遵循自身许可证 |
| UniCardio | 外部可选依赖，未包含 | 用户需自行准备上游代码和权重，并遵循其许可与数据使用条件 |

硬件手册、商家调试软件、第三方资料包、实验参与者记录和训练模型权重不包含在本仓库中。

HAL 完整条款固定来源：[ST 官方 stm32f4xx-hal-driver/LICENSE.md](https://github.com/STMicroelectronics/stm32f4xx-hal-driver/blob/1f6451c3e07728b4c830744de380e56bf5bc0026/LICENSE.md)。本地补充文件保持原文字节，SHA256 为 `376c82bcef0be563bf59b93d05cfdd4b6295e4660b55b31dd8a883bf1d6637f7`。
