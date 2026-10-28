"""Compile the production FIFO/restart C functions with mocked register I/O.

Run with Python and a native GCC on PATH; no board or STM32 HAL is needed.
This verifies policy/error paths, not electrical I2C behavior.
"""
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "BSP/Src/MAX.c").read_text(encoding="utf-8")


def function(name):
    start = source.index("uint8_t " + name + "(")
    brace = source.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


defines = "\n".join(re.findall(r"^#define (?:REG_\w+|MAX30102_FIFO_DEPTH)\s+[^\n]+", source, re.M))
harness = r'''
#include <stdint.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>
typedef struct { uint32_t red, ir; } MAX30102_RawSample;
enum {MAX30102_ERROR_NONE, MAX30102_ERROR_I2C, MAX30102_ERROR_PART_ID,
      MAX30102_ERROR_RESET, MAX30102_ERROR_CONFIG, MAX30102_ERROR_FIFO};
static uint8_t max30102_last_error, max30102_restart_pending;
static uint8_t registers[256];
static int fifo_reads, overflow_events, writes, fail_at, fail_read;
static uint8_t MAX30102_WriteRegister(uint8_t reg, uint8_t value) {
    if (++writes == fail_at) return 0;
    registers[reg] = value;
    return 1;
}
static uint8_t MAX30102_ReadRegister(uint8_t reg, uint8_t *out) {
    if (fail_read) return 0;
    *out = registers[reg];
    return 1;
}
static uint8_t MAX30102_ReadBytes(uint8_t reg, uint8_t *out, uint8_t n) {
    if (fail_read) return 0;
    if (reg == REG_FIFO_DATA) {
        const uint8_t pair[] = {1, 0x23, 0x45, 2, 0x34, 0x56};
        ++fifo_reads;
        for (unsigned i = 0; i < n; ++i) out[i] = pair[i % 6];
    } else memcpy(out, registers + reg, n);
    return 1;
}
static void STREAM_CountPpgFifoOverflow(void) { ++overflow_events; }
'''
cases = r'''
int main(void) {
    MAX30102_RawSample data[32];
    uint8_t count = 99;
    registers[REG_FIFO_WR_PTR] = 2;
    assert(MAX30102_ReadRawFIFO(data, 32, &count));
    assert(count == 2 && fifo_reads == 1);
    assert(data[0].red == 0x12345 && data[0].ir == 0x23456);
    assert(data[1].red == 0x12345 && data[1].ir == 0x23456);

    /* Full and saturated overflow: no old sample may be timestamped as now. */
    registers[REG_FIFO_RD_PTR] = registers[REG_FIFO_WR_PTR] = 7;
    registers[REG_OVF_COUNTER] = 31;
    assert(MAX30102_ReadRawFIFO(data, 32, &count));
    assert(count == 0 && fifo_reads == 1 && overflow_events == 1);
    assert(registers[REG_MODE_CONFIG] == 3);
    assert(registers[REG_FIFO_WR_PTR] == 0 && registers[REG_FIFO_RD_PTR] == 0);
    assert(registers[REG_OVF_COUNTER] == 0 && !max30102_restart_pending);

    /* Restart failure after shutdown must be retried by the next poll. */
    registers[REG_OVF_COUNTER] = 1;
    fail_at = writes + 2;
    assert(!MAX30102_ReadRawFIFO(data, 32, &count));
    assert(count == 0 && max30102_restart_pending);
    assert(registers[REG_MODE_CONFIG] == 0x83);
    fail_at = 0;
    assert(MAX30102_ReadRawFIFO(data, 32, &count));
    assert(count == 0 && !max30102_restart_pending);
    assert(registers[REG_MODE_CONFIG] == 3);
    assert(max30102_last_error == MAX30102_ERROR_NONE);

    registers[REG_FIFO_WR_PTR] = 1;
    assert(MAX30102_ReadRawFIFO(data, 32, &count) && count == 1);
    fail_read = 1;
    assert(!MAX30102_ReadRawFIFO(data, 32, &count) && count == 0);
    assert(max30102_last_error == MAX30102_ERROR_FIFO);
    puts("PASS: raw channel decode, overflow discard, restart retry, I2C read error");
    return 0;
}
'''
build = ROOT / "build-native-tests"
build.mkdir(exist_ok=True)
c_file = build / "fifo_recovery.c"
c_file.write_text(defines + "\n" + harness + "\n" + function("MAX30102_StartSampling")
                  + "\n" + function("MAX30102_ReadRawFIFO") + cases, encoding="utf-8")
compiler = shutil.which("gcc")
if not compiler:
    raise SystemExit("Native GCC is required on PATH")
exe = build / "fifo_recovery.exe"
subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", str(c_file), "-o", str(exe)], check=True)
subprocess.run([str(exe)], check=True)
