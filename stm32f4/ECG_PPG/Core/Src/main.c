/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : ECG(AD8232) + PPG(MAX30102) 同步采集，串口上传上位机
  ******************************************************************************
  * @attention
  *
  * 项目：基于 ECG+PPG 的深度学习无袖带血压监测（大创）
  * MCU ：STM32F411CEU6，板载 25MHz HSE（UART 与采样时基必须准确）
  *
  * 采集链路：
  *   AD8232  -> PA0(ADC1_IN0) -> TIM2 TRGO 200Hz 触发 -> DMA2_Stream0 循环
  *   MAX30102-> PB6/PB7 软件 I2C -> 100Hz FIFO 轮询
  *   两条链路共用 TIM5(1MHz)；PPG 时间戳由 FIFO 读取时刻估计
  *
  * 上传链路：
  *   USART2 (PA2=TX, PA3=RX) @115200 8N1 -> ATK-BLE04 -> BLE 上位机
  *   也可由 PA2 接 USB-TTL RX；上传数据格式相同。
  *   12 字节二进制帧，见 ecg_ppg_stream.h
  *
  * 上位机：host_pc/launch_gui.py（配套），
  *         导出 CSV 供 UniCardio / ABPImputation / 特征提取使用。
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"
#include "adc.h"
#include "dma.h"
#include "tim.h"
#include "gpio.h"
#include "usart.h"
#include "MAX.h"
#include "AD8232.h"
#include "ecg_ppg_stream.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* 主循环轮询 MAX30102 FIFO 的间隔。
 * MAX30102 在 100Hz 下每 10ms 产出一个样本，FIFO 深度 32。
 * 每轮最多读取 FIFO 的完整快照，避免分批时把旧样本误当作最新样本。 */
#define MAX30102_POLL_INTERVAL_MS   2U
#define MAX30102_MAX_SAMPLES        MAX30102_FIFO_DEPTH

/* 每 3 秒把统计信息（帧数/丢帧/FIFO 溢出）通过 USER 调试帧输出一次。
 * 由于 12 字节协议里没有专用调试帧类型，这里改为：
 *   - 若出现丢帧或溢出，点亮 LED（若已接）；
 *   - 统计信息保留在 STREAM_Stats 中，可用调试器查看。
 * 置 1 会通过 UART 发送 ASCII 调试行，会占用带宽，默认关闭。 */
#define ENABLE_ASCII_DEBUG          0

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/

/* USER CODE BEGIN PV */

/* 说明：曾经在这里放了一个 static MAX30102_DataTypeDef max30102_data，
 * 但改成分帧上传后不再需要它（每帧直接由 STREAM_PushPPGAt 组帧），
 * 留着会触发 -Wunused-variable，故移除。 */

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
/* USER CODE BEGIN PFP */

static void MAX30102_DrainFifo(void);

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

/**
 * @brief 一次性把 MAX30102 FIFO 里的样本全部取出并组帧入队。
 *
 * 为什么按「批」而不是按「点」取：
 *   软件 I2C 读 6 字节（一个 RED+IR 对）约需数百微秒。
 *   若每个样本都单独发起一次 I2C 事务，100Hz 下尚可接受，
 *   但一旦主循环有抖动就容易让 FIFO 累积直至溢出。
 *   批量读取 + FIFO 指针差计算，是一次事务周期内把积压样本清空，更稳。
 */
static void MAX30102_DrainFifo(void)
{
    MAX30102_RawSample samples[MAX30102_MAX_SAMPLES];
    uint8_t sample_count = 0U;
    uint8_t i;
    uint32_t newest_us;

    /* Anchor before the burst: the transfer time is not acquisition time. */
    newest_us = STREAM_TimestampUs();
    if (!MAX30102_ReadRawFIFO(samples, MAX30102_MAX_SAMPLES, &sample_count)) {
        return;
    }
    if (sample_count == 0U) {
        return;
    }

    /* 最后一个样本以读取前时刻近似，其余按标称 100Hz 逆推。
     * 仅批内是等间隔；批间仍包含轮询延迟及传感器时钟误差。
     * 不能将这些估计时间戳当作已经标定的精确 PAT 时间基准。
     * 注意：必须用带显式时间戳的 STREAM_PushPPGAt，否则同一批 N 个样本
     *       会被打成同一个时刻，时间轴塌缩。 */
    for (i = 0U; i < sample_count; ++i) {
        uint32_t age_us = (uint32_t)(sample_count - 1U - i) * 10000U;
        (void)STREAM_PushPPGAt(samples[i].red, samples[i].ir,
                               newest_us - age_us);
    }
}

/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_DMA_Init();
  MX_ADC1_Init();
  MX_TIM2_Init();
  MX_TIM5_Init();
  MX_USART2_UART_Init();
  /* USER CODE BEGIN 2 */

  /* 启动 1MHz 时间戳基准（必须在任何采集开始之前） */
   if (HAL_TIM_Base_Start(&htim5) != HAL_OK) Error_Handler();

  /* 初始化串口发送层（探测 DMA 可用性） */
  STREAM_Start();

   /* Configure the sensor stopped; no sample predates the TIM5 time base. */
   if (MAX30102_Init() == 0U) Error_Handler();
   if (MAX30102_StartSampling() == 0U) Error_Handler();

  /* 启动 ADC1 + DMA，开始 ECG 采集 */
  if (AD8232_Start() != HAL_OK)
    Error_Handler();

   /* Arm ADC/DMA before enabling its 200 Hz trigger. */
   if (HAL_TIM_Base_Start(&htim2) != HAL_OK) Error_Handler();

  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* ECG 由 TIM2 TRGO + ADC DMA 中断自动完成组帧，主循环无需干预。 */

    /* PPG：每 2ms 轮询一次 FIFO，把积压样本批量取回并入队。 */
    MAX30102_DrainFifo();

    /* 串口发送：把队列中的帧通过 USART2+DMA 搬出去（非阻塞） */
    STREAM_ProcessTx();

    /* 2ms 节拍，给 SysTick / 中断留出执行时间 */
    HAL_Delay(MAX30102_POLL_INTERVAL_MS);

    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Configure the main internal regulator output voltage
  */
  __HAL_RCC_PWR_CLK_ENABLE();
  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI | RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_ON;
  RCC_OscInitStruct.HSIState = RCC_HSI_ON;
  RCC_OscInitStruct.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_NONE;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    /* Do not silently change the UART baud / acquisition time base to HSI.
     * This board's HSI was measured outside the UART timing tolerance. */
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_HSE;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_0) != HAL_OK)
  {
    Error_Handler();
  }
}

/* USER CODE BEGIN 4 */

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
