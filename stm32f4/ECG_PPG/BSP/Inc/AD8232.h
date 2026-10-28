#ifndef ECG_PPG_AD8232_H
#define ECG_PPG_AD8232_H

#include "main.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---------------------------------------------------------------------------
 * AD8232 心电前端采集
 * ---------------------------------------------------------------------------
 * 硬件链路：
 *   AD8232 OUTPUT -> PA0 (ADC1_IN0) -> ADC1 -> DMA2_Stream0 (circular)
 *   采样触发：TIM2 TRGO（200Hz），由 adc.c 中的 ExternalTrigConv=T2_TRGO 决定
 *
 * AD8232 特点与工程注意事项：
 *   - 单导联差分输出，输出已内置 0.5Hz 高通 + 40Hz 低通 + 右腿驱动
 *   - 静态输出摆幅居中在 AVDD/2 = 1.65V，对应 12bit 码值约 2048
 *   - 因此「ADC 原始值」就是可以直接做数字高通/低通处理的波形，无需额外放大
 *   - 电极脱落检测：AD8232 的 LO+ / LO- 引脚为高时说明导联脱落。
 *     若已接到 GPIO，可在此模块配套读取；未接线时可忽略（默认不检测）。
 * ------------------------------------------------------------------------- */

/* ADC 满量程与参考电压（STM32F411 内部 VREF ≈ 3.3V） */
#define AD8232_ADC_MAX        4095.0f
#define AD8232_VREF_MV        3300.0f
#define AD8232_ADC_CENTER     2048.0f

/* AD8232 输出零点对应的电压（mV），用于把 ADC 码值折算成 mV */
#define AD8232_MV_PER_LSB     (AD8232_VREF_MV / (AD8232_ADC_MAX + 1.0f))

HAL_StatusTypeDef AD8232_Start(void);
uint16_t AD8232_GetLatestSample(void);

/* 把 ADC 原始码值转换成相对零点的毫伏值（带符号），便于上位机直接绘图 */
int32_t AD8232_ToMilliVolt(uint16_t adc_raw);

/* 导联脱落状态：0=正常，1=脱落/未连接（依赖 LO+/LO- 接线，未接则恒返回 0） */
uint8_t AD8232_IsLeadsOff(void);

#ifdef __cplusplus
}
#endif

#endif /* ECG_PPG_AD8232_H */
