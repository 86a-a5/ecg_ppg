#ifndef ECG_PPG_MAX_H
#define ECG_PPG_MAX_H

#include "main.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---------------------------------------------------------------------------
 * MAX30102 光电容积脉搏波(PPG)采集
 * ---------------------------------------------------------------------------
 * 硬件：软件 I2C（PB6=SCL / PB7=SDA，均为开漏输出 + 上拉），INT 未接也可工作，
 *       本驱动采用「轮询 FIFO 指针」方式，不依赖 INT 引脚。
 *
 * 时基选择（重要）：
 *   主时钟 MCLK = 25MHz（模块自带晶振，无需外部提供）
 *   SPO2_CONFIG(0x0A) = 0x27  ->  ADC Range 4096nA / Sample Rate 100Hz / Pulse Width 411us(18bit)
 *   即每 10ms 产出一个 RED + IR 样本对，与 ECG 200Hz 形成 2:1 关系。
 *
 * 上传约定：上位机协议里 RED/IR 为 uint16，存放 18bit 原始值 >> 2，
 *          因此上位机还原满量程时需要 << 2（见 ecg_ppg_stream.c 的注释）。
 * ------------------------------------------------------------------------- */

#define MAX30102_I2C_GPIO_PORT GPIOB
#define MAX30102_I2C_GPIO_CLK_ENABLE() __HAL_RCC_GPIOB_CLK_ENABLE()
#define MAX30102_I2C_SCL_PIN GPIO_PIN_6
#define MAX30102_I2C_SDA_PIN GPIO_PIN_7

#define MAX30102_I2C_TIMEOUT_US 200U
#define MAX30102_FIFO_DEPTH 32U
#define MAX30102_SAMPLE_RATE_HZ 100U
#define MAX30102_EXPECTED_PART_ID 0x15U

/* 18bit 满量程 */
#define MAX30102_FULL_SCALE_18BIT 262143UL

typedef enum {
    MAX30102_ERROR_NONE = 0U,
    MAX30102_ERROR_I2C,
    MAX30102_ERROR_PART_ID,
    MAX30102_ERROR_RESET,
    MAX30102_ERROR_CONFIG,
    MAX30102_ERROR_FIFO
} MAX30102_Error;

typedef struct {
    uint32_t red;
    uint32_t ir;
    uint8_t sample_ready;
    uint8_t interrupt_status;
    uint8_t fifo_write_pointer;
    uint8_t fifo_read_pointer;
    uint8_t fifo_overflow;
} MAX30102_DataTypeDef;

/* 一个未经处理的 MAX30102 FIFO 样本，RED/IR 为 18bit 原始值 */
typedef struct {
    uint32_t red;
    uint32_t ir;
} MAX30102_RawSample;

uint8_t MAX30102_Init(void);
/* Clear stale FIFO data while stopped, then start RED/IR conversion. */
uint8_t MAX30102_StartSampling(void);
uint8_t MAX30102_Update(MAX30102_DataTypeDef *data);
uint8_t MAX30102_WriteRegister(uint8_t reg, uint8_t value);
uint8_t MAX30102_ReadRegister(uint8_t reg, uint8_t *value);
uint8_t MAX30102_ReadFIFO(uint32_t *red, uint32_t *ir);
uint8_t MAX30102_ReadRawFIFO(MAX30102_RawSample *samples,
                             uint8_t max_samples,
                             uint8_t *sample_count);
uint8_t MAX30102_GetLastError(void);
uint8_t MAX30102_GetPartID(void);

/* ADC 满量程与每 LSB 对应的光电流量级（数据手册范围 4096nA / 18bit），
 * 用于把原始码值折算成便于观察的百分比（直流分量占满量程比例）。 */
uint32_t MAX30102_ToPercentX100(uint32_t raw18);

#ifdef __cplusplus
}
#endif

#endif /* ECG_PPG_MAX_H */
