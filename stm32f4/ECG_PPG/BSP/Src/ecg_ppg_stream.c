/**
 * @file ecg_ppg_stream.c
 * @brief ECG/PPG 采集帧的串口发送层。
 *
 * 设计要点：
 * 1. 采集回调（ADC DMA / MAX30102 轮询）只做「组帧 + 入环形队列」，不做任何阻塞发送；
 * 2. 主循环调用 STREAM_ProcessTx()，把队列里的字节通过 USART2 TX + DMA 搬出去；
 * 3. 环形队列按「字节」存储，DMA 发送期间新入队的帧自动被下一次传输接上，
 *    因此 200Hz ECG + 100Hz PPG 的中断频率下不会挤压采集时序；
 * 4. USART2 仍保留 HAL_UART_Transmit 阻塞回退路径：若 DMA 未使能（例如
 *    CubeMX 重新生成后 DMA 配置丢失），自动退回阻塞发送，保证功能可用。
 */

#include "ecg_ppg_stream.h"
#include "usart.h"
#include "tim.h"
#include <string.h>

/* ---------------------------------------------------------------------------
 * 字节环形缓冲
 * ------------------------------------------------------------------------- */
/* 缓冲大小由头文件给出。这里做一次编译期断言：必须是 2 的幂，
 * 否则下面用 & (SIZE-1) 实现的取模不成立（曾经踩过这个坑）。 */
#define STREAM_TX_BUFFER_SIZE  STREAM_TX_BYTES

#if STREAM_TX_BYTES < 16 || STREAM_TX_BYTES > 32768 || (STREAM_TX_BYTES & (STREAM_TX_BYTES - 1)) != 0
#error "STREAM_TX_BYTES must be a power of two (mask-based modulo relies on it)"
#endif

static uint8_t          tx_buffer[STREAM_TX_BUFFER_SIZE];
static volatile uint16_t tx_head;   /* 写指针（生产者：采集中断/主循环） */
static volatile uint16_t tx_tail;   /* 读指针（消费者：DMA 完成中断） */
static volatile uint8_t  tx_busy;   /* DMA 正在搬运中 */
static volatile uint8_t  tx_dma_ready;
static volatile uint16_t tx_active_length;
static volatile uint8_t  tx_error_pending;

static volatile uint8_t  sequence;  /* ECG / PPG 共用序号 */
static STREAM_Stats      stats;

/* ---------------------------------------------------------------------------
 * 内部工具
 * ------------------------------------------------------------------------- */

static uint16_t STREAM_Used(void)
{
    return (uint16_t)((tx_head - tx_tail) & (STREAM_TX_BUFFER_SIZE - 1U));
}

static uint16_t STREAM_Free(void)
{
    return (uint16_t)(STREAM_TX_BUFFER_SIZE - 1U - STREAM_Used());
}

/* 在关中断临界区里把一帧 12 字节压入环形缓冲。
 *
 * 序号也在这里赋值，而不是由调用方先取号：这样「序号递增顺序」与
 * 「入队顺序」严格一致。否则中断可能插在「取号」与「入队」之间，
 * 使缓冲里出现 101,100 这样的乱序，上位机会误判为丢帧。
 *
 * 先取号再判满：若因队列满而丢弃，序号会留出空档，
 * 上位机正是靠这个空档发现丢帧的。 */
static uint8_t STREAM_PushFrame(uint8_t *frame)
{
    uint16_t i;
    uint32_t primask;

    primask = __get_PRIMASK();
    __disable_irq();

    frame[2] = sequence++;

    if (STREAM_Free() < STREAM_FRAME_LENGTH) {
        ++stats.tx_dropped;
        if (primask == 0U) {
            __enable_irq();
        }
        return 1U;
    }

    for (i = 0U; i < STREAM_FRAME_LENGTH; ++i) {
        tx_buffer[tx_head] = frame[i];
        tx_head = (uint16_t)((tx_head + 1U) & (STREAM_TX_BUFFER_SIZE - 1U));
    }
    ++stats.tx_frames;

    if (primask == 0U) {
        __enable_irq();
    }
    return 0U;
}

/* 计算本次 DMA 需要搬运的连续字节数：受限于环形缓冲尾部 */
static uint16_t STREAM_ContiguousChunk(void)
{
    uint16_t used = STREAM_Used();

    if (used == 0U) {
        return 0U;
    }
    if (tx_tail + used > (STREAM_TX_BUFFER_SIZE - 1U)) {
        return (uint16_t)(STREAM_TX_BUFFER_SIZE - tx_tail);
    }
    return used;
}

/* ---------------------------------------------------------------------------
 * 对外接口
 * ------------------------------------------------------------------------- */

uint32_t STREAM_TimestampUs(void)
{
    return __HAL_TIM_GET_COUNTER(&htim5);
}

uint8_t STREAM_PushECGAt(uint16_t adc_raw, uint32_t timestamp_us)
{
    /* ★ 用局部数组，不要用共享的 static 缓冲 ★
     * 本函数在 ADC DMA 中断（200Hz）里被调用，而 STREAM_PushPPGAt 在主循环里
     * 被调用。若两者共用一块 static 缓冲，中断可能恰好插在主循环组帧的中途，
     * 把 ECG 内容覆盖进去 —— 结果是多发一帧 ECG、丢掉一帧 PPG。 */
    uint8_t frame[STREAM_FRAME_LENGTH];

    frame[0]  = STREAM_FRAME_HEAD;
    frame[1]  = STREAM_TYPE_ECG;
    frame[2]  = 0U;   /* 序号在 STREAM_PushFrame 的临界区内填充 */
    frame[3]  = (uint8_t)(adc_raw & 0xFFU);
    frame[4]  = (uint8_t)((adc_raw >> 8) & 0xFFU);
    frame[5]  = 0xFFU;
    frame[6]  = 0xFFU;
    frame[7]  = (uint8_t)(timestamp_us & 0xFFU);
    frame[8]  = (uint8_t)((timestamp_us >> 8) & 0xFFU);
    frame[9]  = (uint8_t)((timestamp_us >> 16) & 0xFFU);
    frame[10] = (uint8_t)((timestamp_us >> 24) & 0xFFU);
    frame[11] = STREAM_FRAME_TAIL;

    ++stats.ecg_frames;
    return STREAM_PushFrame(frame);
}

uint8_t STREAM_PushECG(uint16_t adc_raw)
{
    return STREAM_PushECGAt(adc_raw, STREAM_TimestampUs());
}

uint8_t STREAM_PushPPGAt(uint32_t red_raw, uint32_t ir_raw, uint32_t timestamp_us)
{
    /* 同上：用局部数组，避免与 ECG 中断共享缓冲 */
    uint8_t frame[STREAM_FRAME_LENGTH];

    /* 上位机协议里 RED/IR 是 uint16，约定存放 18bit 原始值 >> 2 */
    uint16_t red = (uint16_t)((red_raw >> 2) & 0xFFFFU);
    uint16_t ir  = (uint16_t)((ir_raw  >> 2) & 0xFFFFU);

    frame[0]  = STREAM_FRAME_HEAD;
    frame[1]  = STREAM_TYPE_PPG;
    frame[2]  = 0U;   /* 序号在 STREAM_PushFrame 的临界区内填充 */
    frame[3]  = (uint8_t)(red & 0xFFU);
    frame[4]  = (uint8_t)((red >> 8) & 0xFFU);
    frame[5]  = (uint8_t)(ir & 0xFFU);
    frame[6]  = (uint8_t)((ir >> 8) & 0xFFU);
    frame[7]  = (uint8_t)(timestamp_us & 0xFFU);
    frame[8]  = (uint8_t)((timestamp_us >> 8) & 0xFFU);
    frame[9]  = (uint8_t)((timestamp_us >> 16) & 0xFFU);
    frame[10] = (uint8_t)((timestamp_us >> 24) & 0xFFU);
    frame[11] = STREAM_FRAME_TAIL;

    ++stats.ppg_frames;
    return STREAM_PushFrame(frame);
}

uint8_t STREAM_PushPPG(uint32_t red_raw, uint32_t ir_raw)
{
    return STREAM_PushPPGAt(red_raw, ir_raw, STREAM_TimestampUs());
}

void STREAM_Start(void)
{
    memset(tx_buffer, 0, sizeof(tx_buffer));
    tx_head = 0U;
    tx_tail = 0U;
    tx_busy = 0U;
    tx_dma_ready = 0U;
    tx_active_length = 0U;
    tx_error_pending = 0U;
    sequence = 0U;
    memset(&stats, 0, sizeof(stats));

    /* USART2_TX = PA2 -> DMA1_Stream6_Channel_4（见 usart.c 的 MspInit） */
    tx_dma_ready = (huart2.hdmatx != NULL) ? 1U : 0U;
}

/* Called after the UART has shifted out the last byte, not just DMA completion. */
static void STREAM_TxCompleteFromISR(void)
{
    if (tx_busy != 0U) {
        tx_tail = (uint16_t)((tx_tail + tx_active_length) & (STREAM_TX_BUFFER_SIZE - 1U));
        stats.tx_bytes_sent += tx_active_length;
        tx_active_length = 0U;
        tx_busy = 0U;
    }
}

void STREAM_ProcessTx(void)
{
    uint16_t chunk;
    HAL_StatusTypeDef result;

    if (tx_error_pending != 0U) {
        /* Recover in the main loop. Retain queued bytes for a retry. */
        if (HAL_UART_AbortTransmit(&huart2) != HAL_OK) {
            return;
        }
        tx_active_length = 0U;
        tx_busy = 0U;
        tx_error_pending = 0U;
    }

    if (tx_busy != 0U) {
        return;
    }

    chunk = STREAM_ContiguousChunk();
    if (chunk == 0U) {
        return;
    }

    if (tx_dma_ready != 0U) {
        /* Publish state before starting DMA: completion may interrupt this call. */
        tx_active_length = chunk;
        tx_busy = 1U;
        result = HAL_UART_Transmit_DMA(&huart2, &tx_buffer[tx_tail], chunk);
        if (result != HAL_OK) {
            tx_busy = 0U;
            tx_active_length = 0U;
            if (result != HAL_BUSY) {
                ++stats.tx_uart_errors;
            }
        }
        /* HAL_BUSY is transient. Do not discard bytes or disable DMA permanently. */
        return;
    }

    {
        uint16_t len = (chunk > STREAM_FRAME_LENGTH * 4U) ? (STREAM_FRAME_LENGTH * 4U)
                                                          : chunk;
        result = HAL_UART_Transmit(&huart2, &tx_buffer[tx_tail], len, 20U);
        if (result == HAL_OK) {
            tx_tail = (uint16_t)((tx_tail + len) & (STREAM_TX_BUFFER_SIZE - 1U));
            stats.tx_bytes_sent += len;
        } else if (result != HAL_BUSY) {
            ++stats.tx_uart_errors;
        }
    }
}

/* HAL 回调：DMA 发送完成 */
void HAL_UART_TxCpltCallback(UART_HandleTypeDef *huart)
{
    if (huart != NULL && huart->Instance == USART2 && tx_error_pending == 0U) {
        STREAM_TxCompleteFromISR();
    }
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
    if (huart != NULL && huart->Instance == USART2 && tx_busy != 0U) {
        ++stats.tx_uart_errors;
        tx_error_pending = 1U;
    }
}

void STREAM_GetStats(STREAM_Stats *out)
{
    uint32_t primask;

    if (out == NULL) {
        return;
    }
    primask = __get_PRIMASK();
    __disable_irq();
    *out = stats;
    if (primask == 0U) {
        __enable_irq();
    }
}

void STREAM_CountEcgQueueError(void)
{
    ++stats.ecg_queue_errors;
}

void STREAM_CountPpgFifoOverflow(void)
{
    ++stats.ppg_fifo_overflows;
}
