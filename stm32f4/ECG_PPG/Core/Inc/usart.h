#ifndef ECG_PPG_USART_H
#define ECG_PPG_USART_H

#include "main.h"

extern UART_HandleTypeDef huart2;
extern DMA_HandleTypeDef  hdma_usart2_tx;
void MX_USART2_UART_Init(void);

#endif
