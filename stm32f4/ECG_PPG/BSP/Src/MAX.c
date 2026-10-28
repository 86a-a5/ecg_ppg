#include "MAX.h"
#include "main.h"
#include "ecg_ppg_stream.h"

static void MAX30102_DelayUs(uint32_t us)
{
    uint32_t start = DWT->CYCCNT;
    uint32_t ticks = us * (SystemCoreClock / 1000000U);
    while ((DWT->CYCCNT - start) < ticks) { }
}

static void MAX30102_DelayMs(uint32_t ms)
{
    HAL_Delay(ms);
}

/**
 * @file MAX.c
 * @brief MAX30102 software-I2C driver and raw FIFO acquisition.
 * @note Signal processing is intentionally omitted; RED/IR samples are exported raw.
 */

/* MAX30102 7-bit I2C address: 0x57. */
#define MAX30102_ADDR_WRITE          0xAEU
#define MAX30102_ADDR_READ           0xAFU

/* MAX30102 register map. */
#define REG_INTR_STATUS_1            0x00U
#define REG_INTR_STATUS_2            0x01U
#define REG_INTR_ENABLE_1            0x02U
#define REG_INTR_ENABLE_2            0x03U
#define REG_FIFO_WR_PTR              0x04U
#define REG_OVF_COUNTER              0x05U
#define REG_FIFO_RD_PTR              0x06U
#define REG_FIFO_DATA                0x07U
#define REG_FIFO_CONFIG              0x08U
#define REG_MODE_CONFIG              0x09U
#define REG_SPO2_CONFIG              0x0AU
#define REG_LED1_PA                  0x0CU
#define REG_LED2_PA                  0x0DU
#define REG_PILOT_PA                 0x10U
#define REG_REV_ID                   0xFEU
#define REG_PART_ID                  0xFFU

#define MAX30102_FIFO_DEPTH          32U
#define MAX30102_SAMPLE_RATE_HZ       100U
#define MAX30102_EXPECTED_PART_ID     0x15U

#define MAX30102_SCL_HIGH() \
    HAL_GPIO_WritePin(MAX30102_I2C_GPIO_PORT, MAX30102_I2C_SCL_PIN, GPIO_PIN_SET)
#define MAX30102_SCL_LOW() \
    HAL_GPIO_WritePin(MAX30102_I2C_GPIO_PORT, MAX30102_I2C_SCL_PIN, GPIO_PIN_RESET)
#define MAX30102_SDA_HIGH() \
    HAL_GPIO_WritePin(MAX30102_I2C_GPIO_PORT, MAX30102_I2C_SDA_PIN, GPIO_PIN_SET)
#define MAX30102_SDA_LOW() \
    HAL_GPIO_WritePin(MAX30102_I2C_GPIO_PORT, MAX30102_I2C_SDA_PIN, GPIO_PIN_RESET)
#define MAX30102_SDA_READ() \
    HAL_GPIO_ReadPin(MAX30102_I2C_GPIO_PORT, MAX30102_I2C_SDA_PIN)

static uint8_t max30102_last_error;
static uint8_t max30102_part_id;
static uint8_t max30102_restart_pending;

static void MAX30102_I2C_Delay(void);
static void MAX30102_SDA_Output(void);
static void MAX30102_SDA_Input(void);
static void MAX30102_I2C_Start(void);
static void MAX30102_I2C_Stop(void);
static uint8_t MAX30102_I2C_WriteByte(uint8_t value);
static uint8_t MAX30102_I2C_ReadByte(uint8_t acknowledge);
static void MAX30102_I2C_BusRecovery(void);
static uint8_t MAX30102_ReadBytes(uint8_t reg, uint8_t *buffer, uint8_t length);

static void MAX30102_I2C_Delay(void)
{
    MAX30102_DelayUs(4U);
}

static void MAX30102_SDA_Output(void)
{
    /* PB7 is configured as open-drain by MX_GPIO_Init(). */
}

static void MAX30102_SDA_Input(void)
{
    /* Open-drain high releases PB7; the pin can then be sampled as input. */
    HAL_GPIO_WritePin(MAX30102_I2C_GPIO_PORT, MAX30102_I2C_SDA_PIN, GPIO_PIN_SET);
}

static void MAX30102_I2C_Start(void)
{
    MAX30102_SDA_Output();
    MAX30102_SDA_HIGH();
    MAX30102_SCL_HIGH();
    MAX30102_I2C_Delay();
    MAX30102_SDA_LOW();
    MAX30102_I2C_Delay();
    MAX30102_SCL_LOW();
}

static void MAX30102_I2C_Stop(void)
{
    MAX30102_SDA_Output();
    MAX30102_SCL_LOW();
    MAX30102_SDA_LOW();
    MAX30102_I2C_Delay();
    MAX30102_SCL_HIGH();
    MAX30102_I2C_Delay();
    MAX30102_SDA_HIGH();
    MAX30102_I2C_Delay();
}

static uint8_t MAX30102_I2C_WriteByte(uint8_t value)
{
    uint8_t i;
    uint16_t timeout;

    MAX30102_SDA_Output();
    MAX30102_SCL_LOW();

    for (i = 0U; i < 8U; ++i) {
        MAX30102_SCL_LOW();
        if ((value & 0x80U) != 0U) {
            MAX30102_SDA_HIGH();
        } else {
            MAX30102_SDA_LOW();
        }

        MAX30102_I2C_Delay();
        MAX30102_SCL_HIGH();
        MAX30102_I2C_Delay();
        value <<= 1;
    }

    MAX30102_SCL_LOW();
    MAX30102_SDA_Input();
    MAX30102_SDA_HIGH();
    MAX30102_I2C_Delay();
    MAX30102_SCL_HIGH();
    MAX30102_I2C_Delay();

    timeout = 200U;
    while ((MAX30102_SDA_READ() != GPIO_PIN_RESET) && (timeout > 0U)) {
        --timeout;
        MAX30102_DelayUs(1U);
    }

    MAX30102_SCL_LOW();
    MAX30102_SDA_Output();
    return (timeout > 0U) ? 1U : 0U;
}

static uint8_t MAX30102_I2C_ReadByte(uint8_t acknowledge)
{
    uint8_t i;
    uint8_t value = 0U;

    MAX30102_SDA_Input();
    MAX30102_SDA_HIGH();

    for (i = 0U; i < 8U; ++i) {
        MAX30102_SCL_LOW();
        MAX30102_I2C_Delay();
        MAX30102_SCL_HIGH();
        value <<= 1;

        if (MAX30102_SDA_READ() != GPIO_PIN_RESET) {
            value |= 0x01U;
        }
        MAX30102_I2C_Delay();
    }

    MAX30102_SCL_LOW();
    MAX30102_SDA_Output();
    if (acknowledge != 0U) {
        MAX30102_SDA_LOW();
    } else {
        MAX30102_SDA_HIGH();
    }

    MAX30102_I2C_Delay();
    MAX30102_SCL_HIGH();
    MAX30102_I2C_Delay();
    MAX30102_SCL_LOW();
    MAX30102_SDA_HIGH();
    return value;
}

static void MAX30102_I2C_BusRecovery(void)
{
    uint8_t i;

    MAX30102_SDA_Output();
    MAX30102_SDA_HIGH();

    for (i = 0U; i < 9U; ++i) {
        MAX30102_SCL_LOW();
        MAX30102_I2C_Delay();
        MAX30102_SCL_HIGH();
        MAX30102_I2C_Delay();
    }

    MAX30102_I2C_Stop();
}

uint8_t MAX30102_WriteRegister(uint8_t reg, uint8_t value)
{
    MAX30102_I2C_Start();

    if ((MAX30102_I2C_WriteByte(MAX30102_ADDR_WRITE) == 0U) ||
        (MAX30102_I2C_WriteByte(reg) == 0U) ||
        (MAX30102_I2C_WriteByte(value) == 0U)) {
        MAX30102_I2C_Stop();
        return 0U;
    }

    MAX30102_I2C_Stop();
    return 1U;
}

static uint8_t MAX30102_ReadBytes(uint8_t reg, uint8_t *buffer, uint8_t length)
{
    uint8_t i;

    if ((buffer == 0) || (length == 0U)) {
        return 0U;
    }

    MAX30102_I2C_Start();
    if ((MAX30102_I2C_WriteByte(MAX30102_ADDR_WRITE) == 0U) ||
        (MAX30102_I2C_WriteByte(reg) == 0U)) {
        MAX30102_I2C_Stop();
        return 0U;
    }

    MAX30102_I2C_Start();
    if (MAX30102_I2C_WriteByte(MAX30102_ADDR_READ) == 0U) {
        MAX30102_I2C_Stop();
        return 0U;
    }

    for (i = 0U; i < length; ++i) {
        buffer[i] = MAX30102_I2C_ReadByte((i + 1U < length) ? 1U : 0U);
    }

    MAX30102_I2C_Stop();
    return 1U;
}

uint8_t MAX30102_ReadRegister(uint8_t reg, uint8_t *value)
{
    return MAX30102_ReadBytes(reg, value, 1U);
}

uint8_t MAX30102_ReadFIFO(uint32_t *red, uint32_t *ir)
{
    uint8_t raw[6];

    if ((red == 0) || (ir == 0) ||
        (MAX30102_ReadBytes(REG_FIFO_DATA, raw, 6U) == 0U)) {
        return 0U;
    }

    *red = (((uint32_t)raw[0] << 16) |
            ((uint32_t)raw[1] << 8) |
            raw[2]) & 0x03FFFFUL;
    *ir = (((uint32_t)raw[3] << 16) |
           ((uint32_t)raw[4] << 8) |
           raw[5]) & 0x03FFFFUL;
    return 1U;
}

uint8_t MAX30102_Init(void)
{
    uint8_t value;
    uint8_t timeout;
    uint8_t attempt;

    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    max30102_last_error = MAX30102_ERROR_NONE;
    max30102_part_id = 0U;

    MAX30102_SCL_HIGH();
    MAX30102_SDA_HIGH();
    MAX30102_DelayMs(10U);
    MAX30102_I2C_BusRecovery();

    for (attempt = 0U; attempt < 3U; ++attempt) {
        if (MAX30102_ReadRegister(REG_PART_ID, &value) != 0U) {
            break;
        }
        MAX30102_I2C_BusRecovery();
        MAX30102_DelayMs(2U);
    }

    if (attempt >= 3U) {
        max30102_last_error = MAX30102_ERROR_I2C;
        return 0U;
    }

    max30102_part_id = value;
    if (value != MAX30102_EXPECTED_PART_ID) {
        max30102_last_error = MAX30102_ERROR_PART_ID;
        return 0U;
    }

    if (MAX30102_WriteRegister(REG_MODE_CONFIG, 0x40U) == 0U) {
        max30102_last_error = MAX30102_ERROR_RESET;
        return 0U;
    }

    timeout = 100U;
    do {
        MAX30102_DelayMs(1U);
        if (MAX30102_ReadRegister(REG_MODE_CONFIG, &value) == 0U) {
            max30102_last_error = MAX30102_ERROR_RESET;
            return 0U;
        }
        --timeout;
    } while (((value & 0x40U) != 0U) && (timeout > 0U));

    if ((value & 0x40U) != 0U) {
        max30102_last_error = MAX30102_ERROR_RESET;
        return 0U;
    }

    if ((MAX30102_WriteRegister(REG_INTR_ENABLE_1, 0xC0U) == 0U) ||
        (MAX30102_WriteRegister(REG_INTR_ENABLE_2, 0x00U) == 0U) ||
        (MAX30102_WriteRegister(REG_FIFO_WR_PTR, 0x00U) == 0U) ||
        (MAX30102_WriteRegister(REG_OVF_COUNTER, 0x00U) == 0U) ||
        (MAX30102_WriteRegister(REG_FIFO_RD_PTR, 0x00U) == 0U) ||
        (MAX30102_WriteRegister(REG_FIFO_CONFIG, 0x0FU) == 0U) ||
        (MAX30102_WriteRegister(REG_MODE_CONFIG, 0x83U) == 0U) ||
        (MAX30102_WriteRegister(REG_SPO2_CONFIG, 0x27U) == 0U) ||
        (MAX30102_WriteRegister(REG_LED1_PA, 0x24U) == 0U) ||
        (MAX30102_WriteRegister(REG_LED2_PA, 0x24U) == 0U) ||
        (MAX30102_WriteRegister(REG_PILOT_PA, 0x7FU) == 0U)) {
        max30102_last_error = MAX30102_ERROR_CONFIG;
        return 0U;
    }

    MAX30102_DelayMs(10U);
    if ((MAX30102_ReadRegister(REG_MODE_CONFIG, &value) == 0U) ||
        ((value & 0x07U) != 0x03U)) {
        max30102_last_error = MAX30102_ERROR_CONFIG;
        return 0U;
    }

    if ((MAX30102_ReadRegister(REG_INTR_STATUS_1, &value) == 0U) ||
        (MAX30102_ReadRegister(REG_INTR_STATUS_2, &value) == 0U)) {
        max30102_last_error = MAX30102_ERROR_CONFIG;
        return 0U;
    }

    max30102_last_error = MAX30102_ERROR_NONE;
    return 1U;
}

uint8_t MAX30102_StartSampling(void)
{
    uint8_t status;
    max30102_restart_pending = 1U;
    if ((MAX30102_WriteRegister(REG_MODE_CONFIG, 0x83U) == 0U) ||
        (MAX30102_WriteRegister(REG_FIFO_WR_PTR, 0U) == 0U) ||
        (MAX30102_WriteRegister(REG_OVF_COUNTER, 0U) == 0U) ||
        (MAX30102_WriteRegister(REG_FIFO_RD_PTR, 0U) == 0U) ||
        (MAX30102_ReadRegister(REG_INTR_STATUS_1, &status) == 0U) ||
        (MAX30102_ReadRegister(REG_INTR_STATUS_2, &status) == 0U) ||
        (MAX30102_WriteRegister(REG_MODE_CONFIG, 0x03U) == 0U)) {
        max30102_last_error = MAX30102_ERROR_CONFIG;
        return 0U;
    }
    max30102_restart_pending = 0U;
    max30102_last_error = MAX30102_ERROR_NONE;
    return 1U;
}

uint8_t MAX30102_GetLastError(void)
{
    return max30102_last_error;
}

uint8_t MAX30102_GetPartID(void)
{
    return max30102_part_id;
}

/* 把 18bit 原始码值换算成占满量程的比例（放大 100 倍，避免用浮点）。
 * 例：raw=131072 -> 5000，即 50.00%。 */
uint32_t MAX30102_ToPercentX100(uint32_t raw18)
{
    uint32_t clamped = (raw18 > MAX30102_FULL_SCALE_18BIT) ? MAX30102_FULL_SCALE_18BIT
                                                           : raw18;
    return (uint32_t)(((uint64_t)clamped * 10000ULL) / MAX30102_FULL_SCALE_18BIT);
}

/* Read the FIFO pointer snapshot, then burst-read its oldest complete pairs. */
uint8_t MAX30102_ReadRawFIFO(MAX30102_RawSample *samples,
                              uint8_t max_samples,
                              uint8_t *sample_count)
{
    uint8_t interrupt_status;
    uint8_t write_pointer;
    uint8_t read_pointer;
    uint8_t overflow;
    uint8_t available;
    uint8_t count;
    uint8_t i;
    uint8_t pointers[3];
    uint8_t raw[MAX30102_FIFO_DEPTH * 6U];

    if ((samples == 0) || (sample_count == 0) || (max_samples == 0U)) {
        return 0U;
    }
    *sample_count = 0U;

    /* A failed restart may have left SHDN set. Retry the complete sequence
     * before reading data, rather than remaining silently stopped forever. */
    if (max30102_restart_pending != 0U) {
        return MAX30102_StartSampling();
    }

    /* Reading status also clears the MAX30102 interrupt latch. */
    if ((MAX30102_ReadRegister(REG_INTR_STATUS_1, &interrupt_status) == 0U) ||
        (MAX30102_ReadBytes(REG_FIFO_WR_PTR, pointers, 3U) == 0U)) {
        max30102_last_error = MAX30102_ERROR_FIFO;
        return 0U;
    }

    write_pointer = pointers[0] & 0x1FU;
    overflow = pointers[1] & 0x1FU;
    read_pointer = pointers[2] & 0x1FU;
    available = (uint8_t)((write_pointer - read_pointer) &
                          (MAX30102_FIFO_DEPTH - 1U));
    if (available == 0U && (overflow != 0U || (interrupt_status & 0x80U) != 0U)) {
        available = MAX30102_FIFO_DEPTH;
    }

    if (overflow != 0U) {
        STREAM_CountPpgFifoOverflow();
        /* Rollover is disabled: a full FIFO retains OLD samples while new
         * conversions are lost. OVF_COUNTER saturates at 31, so their age
         * cannot be recovered reliably. Never label this backlog as "now".
         * Restart stopped/cleared, exposing the real gap to the host. */
        return MAX30102_StartSampling();
    }
    if (max_samples > MAX30102_FIFO_DEPTH) {
        max_samples = MAX30102_FIFO_DEPTH;
    }
    count = (available < max_samples) ? available : max_samples;
    if (count > 0U && MAX30102_ReadBytes(REG_FIFO_DATA, raw, (uint8_t)(count * 6U)) == 0U) {
        max30102_last_error = MAX30102_ERROR_FIFO;
        return 0U;
    }
    for (i = 0U; i < count; ++i) {
        const uint8_t *p = &raw[i * 6U];
        samples[i].red = (((uint32_t)p[0] << 16) | ((uint32_t)p[1] << 8) | p[2]) & 0x03FFFFUL;
        samples[i].ir = (((uint32_t)p[3] << 16) | ((uint32_t)p[4] << 8) | p[5]) & 0x03FFFFUL;
    }

    *sample_count = count;
    max30102_last_error = MAX30102_ERROR_NONE;
    return 1U;
}

/* Raw FIFO polling. No finger detection, filtering, heart-rate or SpO2
 * calculation is performed here. The host receives every RED/IR sample. */
uint8_t MAX30102_Update(MAX30102_DataTypeDef *data)
{
    uint8_t interrupt_status;
    uint8_t write_pointer;
    uint8_t read_pointer;
    uint8_t overflow;
    uint8_t available;
    uint32_t red;
    uint32_t ir;

    if (data == 0) return 0U;
    data->sample_ready = 0U;
    if ((MAX30102_ReadRegister(REG_INTR_STATUS_1, &interrupt_status) == 0U) ||
        (MAX30102_ReadRegister(REG_FIFO_WR_PTR, &write_pointer) == 0U) ||
        (MAX30102_ReadRegister(REG_OVF_COUNTER, &overflow) == 0U) ||
        (MAX30102_ReadRegister(REG_FIFO_RD_PTR, &read_pointer) == 0U)) {
        max30102_last_error = MAX30102_ERROR_FIFO;
        return 0U;
    }

    write_pointer &= 0x1FU;
    read_pointer &= 0x1FU;
    data->interrupt_status = interrupt_status;
    data->fifo_write_pointer = write_pointer;
    data->fifo_read_pointer = read_pointer;
    data->fifo_overflow = overflow & 0x1FU;
    available = (uint8_t)((write_pointer - read_pointer) & (MAX30102_FIFO_DEPTH - 1U));
    if ((data->fifo_overflow != 0U) && (available == 0U)) available = MAX30102_FIFO_DEPTH;
    if ((available == 0U) && ((interrupt_status & 0x40U) != 0U)) available = 1U;

    /* MAX30102_Update exposes one sample per call. If several FIFO samples
       accumulated, the remaining samples stay in the sensor FIFO. */
    if (available != 0U) {
        if (MAX30102_ReadFIFO(&red, &ir) == 0U) {
            max30102_last_error = MAX30102_ERROR_FIFO;
            return 0U;
        }
        data->red = red;
        data->ir = ir;
        data->sample_ready = 1U;
    }
    if (data->fifo_overflow != 0U) {
        if (MAX30102_WriteRegister(REG_OVF_COUNTER, 0U) == 0U) {
            max30102_last_error = MAX30102_ERROR_FIFO;
            return 0U;
        }
    }
    max30102_last_error = MAX30102_ERROR_NONE;
    return 1U;
}

/* Old processing implementation intentionally removed. */
#if 0
static void MAX30102_Calculate(MAX30102_DataTypeDef *data)
{
    uint16_t i;
    uint16_t peak_count = 0U;
    uint16_t peak_locations[MAX30102_MAX_PEAKS];
    uint32_t ir_sum = 0U;
    uint32_t red_sum = 0U;
    uint32_t ir_mean;
    uint32_t red_mean;
    uint32_t ir_ac;
    uint32_t red_ac;
    uint32_t interval_sum = 0U;
    uint32_t average_interval;
    uint32_t valid_interval_sum = 0U;
    uint16_t valid_intervals = 0U;
    int32_t baseline;
    int32_t difference;
    int32_t positive_peak = 0;
    int32_t negative_peak = 0;
    int32_t threshold;
    int32_t polarity;
    uint64_t ir_square_sum = 0U;
    uint64_t red_square_sum = 0U;
    uint64_t denominator;
    uint64_t ratio_x100;
    int32_t spo2_x100;

    MAX30102_ClearResult(data);

    for (i = 0U; i < MAX30102_WINDOW_SAMPLES; ++i) {
        ir_sum += max30102_ir_buffer[i];
        red_sum += max30102_red_buffer[i];
    }

    ir_mean = ir_sum / MAX30102_WINDOW_SAMPLES;
    red_mean = red_sum / MAX30102_WINDOW_SAMPLES;
    if ((ir_mean < MAX30102_FINGER_IR_MIN) || (red_mean == 0U)) {
        return;
    }

    for (i = 0U; i < MAX30102_WINDOW_SAMPLES; ++i) {
        difference = (int32_t)max30102_ir_buffer[i] - (int32_t)ir_mean;
        ir_square_sum += (uint64_t)((int64_t)difference * difference);
        difference = (int32_t)max30102_red_buffer[i] - (int32_t)red_mean;
        red_square_sum += (uint64_t)((int64_t)difference * difference);
    }

    ir_ac = MAX30102_IntegerSqrt(ir_square_sum / MAX30102_WINDOW_SAMPLES);
    red_ac = MAX30102_IntegerSqrt(red_square_sum / MAX30102_WINDOW_SAMPLES);

    if ((ir_ac >= MAX30102_MIN_AC_LEVEL) &&
        (red_ac >= MAX30102_MIN_AC_LEVEL)) {
        denominator = (uint64_t)ir_ac * red_mean;
        ratio_x100 = ((uint64_t)red_ac * ir_mean * 100U +
                      denominator / 2U) / denominator;

        if ((ratio_x100 >= 30U) && (ratio_x100 <= 170U)) {
            spo2_x100 = 9485L;
            spo2_x100 += (int32_t)((30354ULL * ratio_x100 + 500ULL) / 1000ULL);
            spo2_x100 -= (int32_t)((4506ULL * ratio_x100 * ratio_x100 +
                                5000ULL) / 10000ULL);
            if (spo2_x100 > 10000L) {
                spo2_x100 = 10000L;
            }
            if (spo2_x100 >= 7000L) {
                data->spo2 = (uint16_t)((spo2_x100 + 50L) / 100L);
                data->spo2_valid = 1U;
            }
        }
    }

    baseline = (int32_t)max30102_ir_buffer[0];
    for (i = 0U; i < MAX30102_WINDOW_SAMPLES; ++i) {
        baseline += ((int32_t)max30102_ir_buffer[i] - baseline) / 16;
        max30102_filtered[i] = (int32_t)max30102_ir_buffer[i] - baseline;
        if (max30102_filtered[i] > positive_peak) {
            positive_peak = max30102_filtered[i];
        }
        if (max30102_filtered[i] < negative_peak) {
            negative_peak = max30102_filtered[i];
        }
    }

    polarity = (positive_peak >= -negative_peak) ? 1 : -1;
    threshold = ((polarity > 0) ? positive_peak : -negative_peak) * 35L / 100L;
    if (threshold < (int32_t)MAX30102_MIN_AC_LEVEL) {
        return;
    }

    for (i = 2U; i < (MAX30102_WINDOW_SAMPLES - 2U); ++i) {
        int32_t current = max30102_filtered[i] * polarity;
        int32_t previous = max30102_filtered[i - 1U] * polarity;
        int32_t next = max30102_filtered[i + 1U] * polarity;

        if ((current >= threshold) &&
            (current >= previous) &&
            (current > next)) {
            if (peak_count == 0U) {
                peak_locations[peak_count++] = i;
            } else if ((i - peak_locations[peak_count - 1U]) <
                       MAX30102_MIN_PEAK_GAP) {
                int32_t previous_peak =
                    max30102_filtered[peak_locations[peak_count - 1U]] *
                    polarity;
                if (current > previous_peak) {
                    peak_locations[peak_count - 1U] = i;
                }
            } else if (peak_count < MAX30102_MAX_PEAKS) {
                peak_locations[peak_count++] = i;
            }
        }
    }

    if (peak_count < 3U) {
        return;
    }

    for (i = 1U; i < peak_count; ++i) {
        interval_sum += peak_locations[i] - peak_locations[i - 1U];
    }
    average_interval = interval_sum / (peak_count - 1U);

    for (i = 1U; i < peak_count; ++i) {
        uint16_t interval = peak_locations[i] - peak_locations[i - 1U];
        uint32_t lower = average_interval * 60U / 100U;
        uint32_t upper = average_interval * 140U / 100U;

        if ((interval >= lower) && (interval <= upper)) {
            valid_interval_sum += interval;
            ++valid_intervals;
        }
    }

    if (valid_intervals >= 2U) {
        average_interval = valid_interval_sum / valid_intervals;
        if (average_interval != 0U) {
            data->heart_rate = (uint16_t)((MAX30102_SAMPLE_RATE_HZ * 60U +
                                      average_interval / 2U) /
                                     average_interval);
            if ((data->heart_rate >= 30U) &&
                (data->heart_rate <= 200U)) {
                data->heart_rate_valid = 1U;
            }
        }
    }
}

static void MAX30102_AddSample(uint32_t red, uint32_t ir, MAX30102_DataTypeDef *data)
{
    uint16_t i;

    data->red = red;
    data->ir = ir;
    data->sample_ready = 1U;
    data->fifo_active = 1U;

    if (ir < MAX30102_FINGER_IR_MIN) {
        if (max30102_no_finger_count < MAX30102_FINGER_LOST) {
            ++max30102_no_finger_count;
        }
        if (max30102_no_finger_count >= MAX30102_FINGER_LOST) {
            MAX30102_ResetMeasurement(data);
        }
        return;
    }

    max30102_no_finger_count = 0U;
    data->finger_detected = 1U;

    if (max30102_sample_count < MAX30102_WINDOW_SAMPLES) {
        max30102_red_buffer[max30102_sample_count] = red;
        max30102_ir_buffer[max30102_sample_count] = ir;
        ++max30102_sample_count;
    }

    data->progress = (max30102_measurement_ready != 0U) ?
                     100U :
                     (uint8_t)(((uint32_t)max30102_sample_count * 100U) /
                          MAX30102_WINDOW_SAMPLES);

    if (max30102_sample_count >= MAX30102_WINDOW_SAMPLES) {
        MAX30102_Calculate(data);
        max30102_measurement_ready = 1U;

        for (i = 0U; i < (MAX30102_WINDOW_SAMPLES - MAX30102_WINDOW_STEP);
             ++i) {
            max30102_red_buffer[i] =
                max30102_red_buffer[i + MAX30102_WINDOW_STEP];
            max30102_ir_buffer[i] =
                max30102_ir_buffer[i + MAX30102_WINDOW_STEP];
        }

        max30102_sample_count = MAX30102_WINDOW_SAMPLES -
                                MAX30102_WINDOW_STEP;
        data->progress = 100U;
    }
}

uint8_t MAX30102_Update(MAX30102_DataTypeDef *data)
{
    uint8_t interrupt_status;
    uint8_t write_pointer;
    uint8_t read_pointer;
    uint8_t overflow;
    uint8_t available;
    uint8_t i;
    uint32_t red;
    uint32_t ir;

    if (data == 0) {
        return 0U;
    }

    data->sample_ready = 0U;
    if ((MAX30102_ReadRegister(REG_INTR_STATUS_1, &interrupt_status) == 0U) ||
        (MAX30102_ReadRegister(REG_FIFO_WR_PTR, &write_pointer) == 0U) ||
        (MAX30102_ReadRegister(REG_OVF_COUNTER, &overflow) == 0U) ||
        (MAX30102_ReadRegister(REG_FIFO_RD_PTR, &read_pointer) == 0U)) {
        max30102_last_error = MAX30102_ERROR_FIFO;
        return 0U;
    }

    write_pointer &= 0x1FU;
    read_pointer &= 0x1FU;
    data->interrupt_status = interrupt_status;
    data->fifo_write_pointer = write_pointer;
    data->fifo_read_pointer = read_pointer;

    available = (uint8_t)((write_pointer - read_pointer) &
                     (MAX30102_FIFO_DEPTH - 1U));
    if ((overflow & 0x1FU) != 0U) {
        if (available == 0U) {
            available = MAX30102_FIFO_DEPTH;
        }
    } else if ((available == 0U) && ((interrupt_status & 0x40U) != 0U)) {
        available = 1U;
    }

    for (i = 0U; i < available; ++i) {
        if (MAX30102_ReadFIFO(&red, &ir) == 0U) {
            max30102_last_error = MAX30102_ERROR_FIFO;
            return 0U;
        }
        MAX30102_AddSample(red, ir, data);
    }

    if ((overflow & 0x1FU) != 0U) {
        if (MAX30102_WriteRegister(REG_OVF_COUNTER, 0U) == 0U) {
            max30102_last_error = MAX30102_ERROR_FIFO;
            return 0U;
        }
    }

    max30102_last_error = MAX30102_ERROR_NONE;
    return 1U;
}

#endif /* 0: legacy on-device HR/SpO2 processing */
