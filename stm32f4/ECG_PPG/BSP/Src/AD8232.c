/**
 * @file AD8232.c
 * @brief AD8232 心电前端：TIM2 触发 ADC1 + DMA 循环采集，200Hz 逐点上传。
 *
 * 数据流：
 *   TIM2 TRGO (200Hz)
 *     -> ADC1 转换 PA0 (IN0)
 *       -> DMA2_Stream0 循环写入 ecg_dma_buffer[2]
 *          -> HAL_ADC_ConvHalfCpltCallback : ecg_dma_buffer[0] 有效
 *          -> HAL_ADC_ConvCpltCallback     : ecg_dma_buffer[1] 有效
 *             -> STREAM_PushECG()  组帧入队，主循环负责 DMA 搬出
 *
 * 为什么用长度为 2 的 DMA 缓冲：
 *   DMA 循环模式下，半满中断对应 buffer[0]、全满中断对应 buffer[1]，
 *   每个采样点都能触发一次回调，从而拿到 200Hz 的不间断波形。
 */

#include "AD8232.h"
#include "adc.h"
#include "ecg_ppg_stream.h"

static uint16_t          ecg_dma_buffer[2];
static volatile uint16_t latest_ecg_sample;
static volatile uint32_t ecg_sample_count;
static volatile uint32_t ecg_overrun_count;

HAL_StatusTypeDef AD8232_Start(void)
{
    if (HAL_ADC_Start_DMA(&hadc1, (uint32_t *)ecg_dma_buffer, 2U) != HAL_OK) {
        return HAL_ERROR;
    }
    return HAL_OK;
}

uint16_t AD8232_GetLatestSample(void)
{
    return latest_ecg_sample;
}

int32_t AD8232_ToMilliVolt(uint16_t adc_raw)
{
    /* 以 ADC 中点为零点，输出带符号毫伏值 */
    float mv = ((float)adc_raw - AD8232_ADC_CENTER) * AD8232_MV_PER_LSB;
    return (int32_t)(mv >= 0.0f ? (mv + 0.5f) : (mv - 0.5f));
}

uint8_t AD8232_IsLeadsOff(void)
{
    /* 需要 LO+ / LO- 接到 GPIO 时在此实现；默认不检测 */
    return 0U;
}

/* --- ADC DMA 回调：半满 -> buffer[0] --- */
void HAL_ADC_ConvHalfCpltCallback(ADC_HandleTypeDef *hadc)
{
    if (hadc != NULL && hadc->Instance == ADC1) {
        latest_ecg_sample = ecg_dma_buffer[0];
        ++ecg_sample_count;
        /* 组帧入队后立即返回，不做阻塞发送，避免打乱 200Hz 采样节拍 */
        if (STREAM_PushECG(latest_ecg_sample) != 0U) {
            ++ecg_overrun_count;
            STREAM_CountEcgQueueError();
        }
    }
}

/* --- ADC DMA 回调：全满 -> buffer[1] --- */
void HAL_ADC_ConvCpltCallback(ADC_HandleTypeDef *hadc)
{
    if (hadc != NULL && hadc->Instance == ADC1) {
        latest_ecg_sample = ecg_dma_buffer[1];
        ++ecg_sample_count;
        if (STREAM_PushECG(latest_ecg_sample) != 0U) {
            ++ecg_overrun_count;
            STREAM_CountEcgQueueError();
        }
    }
}
