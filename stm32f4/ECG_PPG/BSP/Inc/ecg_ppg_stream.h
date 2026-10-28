#ifndef ECG_PPG_STREAM_H
#define ECG_PPG_STREAM_H

#include "main.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---------------------------------------------------------------------------
 * ECG_PPG 串口上传协议（与上位机 ble04_pc_gui / serial_ecg_ppg_gui 一致）
 * ---------------------------------------------------------------------------
 * 每帧固定 12 字节，小端：
 *   [0]     0xAA          帧头
 *   [1]     帧类型        0x01=ECG / 0x02=PPG
 *   [2]     序号          ECG 与 PPG 共用同一个 8 位递增计数器
 *   [3..4]  ECG: uint16 ADC 12 位原始值(0~4095)
 *           PPG: uint16 (RED 18bit 右移 2 位)，还原时需再左移 2 位才到满量程
 *   [5..6]  ECG: 0xFF 0xFF 填充
 *           PPG: uint16 (IR  18bit 右移 2 位)
 *   [7..10] uint32 时间戳，TIM5 计数，1MHz 时基，单位 us，小端
 *           （约 71.6 分钟回绕一次，上位机负责展开为单调时间）
 *   [11]    0x55          帧尾
 *
 * 上位机据 [1] 分流、[2] 检测丢帧、[7..10] 做时间对齐与实测采样率。
 *
 * 注意：本注释块内不得出现 "星号斜杠" 序列（注释结束符），
 *       否则会提前终止注释、把后面的中文当成代码，引发大量报错。
 * ------------------------------------------------------------------------- */

#define STREAM_FRAME_HEAD     0xAAU
#define STREAM_FRAME_TAIL     0x55U
#define STREAM_FRAME_LENGTH   12U
#define STREAM_TYPE_ECG       0x01U
#define STREAM_TYPE_PPG       0x02U

/* ECG 采样率由 TIM2 TRGO 决定：1MHz/(4999+1) = 200Hz
 * PPG 采样率由 MAX30102 SPO2_CONFIG=0x27 决定：100Hz
 * 合计 200*12 + 100*12 = 3600 B/s，115200 波特率（8N1 有效 11520 B/s）余量充足。 */
#define STREAM_ECG_RATE_HZ    200U
#define STREAM_PPG_RATE_HZ    100U

/* 队列用于吸收 MCU 发送调度的短时抖动；无流控 UART 无法感知 PC 暂停读取。 */
#define STREAM_TX_QUEUE_FRAMES 256U

/* 环形缓冲的实际字节数。★ 必须是 2 的幂 ★
 * ---------------------------------------------------------------------------
 * 取模是用 `& (STREAM_TX_BYTES - 1)` 实现的，这只有在 SIZE 是 2 的幂时才等价于
 * 取模。此前这里写成 `256 * 12 = 3072`，而 3072 不是 2 的幂：
 *   3071 = 0b101111111111，bit10 为 0，
 *   导致 head 在 1023 -> 1024 处被掩码清成 0（而不是在 3071 -> 3072 处回绕），
 *   结果只有前 1024 字节可用，且 STREAM_Used() 会返回错误值。
 * 4096 = 2^12，可容纳 341 帧（比设计目标还多），代价仅多 1KB RAM。 */
#define STREAM_TX_BYTES        4096U

/* 返回 0 表示入队成功，1 表示队列满（该帧被丢弃，可通过 STREAM_GetStats 观察） */
typedef struct {
    uint32_t tx_frames;        /* 成功进入发送队列的帧数 */
    uint32_t ecg_frames;       /* 采集到的 ECG 帧数（含未入队） */
    uint32_t ppg_frames;       /* 采集到的 PPG 帧数（含未入队） */
    uint32_t tx_dropped;       /* 因队列满而丢弃的帧数 */
    uint32_t ecg_queue_errors; /* ECG 发送队列满导致的入队失败次数 */
    uint32_t ppg_fifo_overflows; /* MAX30102 FIFO 溢出次数 */
    uint32_t tx_bytes_sent;    /* 已通过 UART 完成发送的字节数 */
    uint32_t tx_uart_errors;   /* UART/DMA 发送启动或传输失败次数 */
} STREAM_Stats;

/* 时间戳：TIM5 计数器，按当前 APB1 定时器时钟分频到 1MHz，约 71.6 分钟回绕 */
uint32_t STREAM_TimestampUs(void);

/* 帧入队（中断安全），供 ADC 回调与主循环调用 */
uint8_t STREAM_PushECG(uint16_t adc_raw);
uint8_t STREAM_PushPPG(uint32_t red_raw, uint32_t ir_raw);

/* 带显式时间戳的入队版本（单位 us，TIM5 计数）。
 * 批量读取 FIFO 时必须用这个：一次事务取出 N 个样本时，它们并非"同一时刻"，
 * 需要按 10ms 间隔逆推各自时间戳，否则上位机看到的 PPG 时间轴会塌缩成一点，
 * 实测采样率也会算错。 */
uint8_t STREAM_PushECGAt(uint16_t adc_raw, uint32_t timestamp_us);
uint8_t STREAM_PushPPGAt(uint32_t red_raw, uint32_t ir_raw, uint32_t timestamp_us);

/* 启动/维持 DMA 发送，需在主循环中持续调用 */
void STREAM_Start(void);
void STREAM_ProcessTx(void);

/* 统计 */
void STREAM_GetStats(STREAM_Stats *stats);
void STREAM_CountEcgQueueError(void);
void STREAM_CountPpgFifoOverflow(void);

#ifdef __cplusplus
}
#endif

#endif /* ECG_PPG_STREAM_H */
