# UniCardio 血压预测接入说明

## 已实现的功能

上位机右侧增加两个页面：原有 ECG / IR PPG / RED PPG 三路采集波形，以及 UniCardio 血压预测。
预测页显示 SBP、DBP、MAP、4 秒预测 BP 波形和真正送入模型的两路归一化输入。
原始录制数据、串口协议、固件和原有显示滤波没有修改。

### 使用实时片段预测

1. 用原来的 `start_gui.bat`、`launch_gui.py` 或 `python run.py gui` 启动上位机。
2. 选择实际串口，保持 115200，连接后确认 ECG、IR PPG 和 RED PPG 正常采集。
3. 切换到“UniCardio 血压预测”，点击“开始实时片段预测”。
4. 模型首次在后台加载。两路共同连续数据至少达到 6 秒后，开始预测最新 4 秒。
5. 模型繁忙时继续采集，完成后取最新片段，不积压旧片段。每次新提交的片段结束时间至少前进 2 秒。
6. 点击“停止预测”或断开串口会取消后台计算；重新连接后需要再次启动预测。

本机当前检测到的是 CPU 推理，单次 4 秒片段约需 21–23 秒。界面显示片段时间、计算耗时，
以及片段结束距最新采样的时间。结果对应过去的一段数据，当前速度无法做到逐拍更新。

### 使用已保存的 CSV

点击“预测 CSV 末段…”，选择上位机导出的同步 CSV。当前只预测该文件最后 4 秒，
并需要前面的滤波上下文，因此共同有效数据至少 6 秒。
“导出上次预测…”保存 500 个 BP 采样点、对应两路模型输入、片段时间、来源和血压数值。
这些是单个片段的结果，没有拼接成整段连续血压记录。

也可以在 PowerShell 中启动并自动预测指定文件：

```powershell
# 在仓库的 host_pc 目录执行
& ".\.venv\Scripts\python.exe" .\run.py gui `
  --predict-csv .\outputs\link_validation\live_capture.csv
```

## 输入、预处理和输出

输入使用 AD8232 ECG（200 Hz）和 MAX30102 IR PPG（100 Hz），RED 不参与当前模型预测。
CSV 读取 `ecg_sync_time_s/ecg_processed` 和 `ir_sync_time_s/ir_processed`；
存在 `ecg_raw` 时优先从原始 ADC 码值换算 ECG 电压，兼容旧模拟数据中单位标注不同的情况。

模型使用独立的数据副本：检查时间戳、采样间断、ECG 近饱和、PPG 光强和接触突变，
然后进行 ECG 50 Hz 陷波及 0.5–40 Hz 带通、PPG 0.5–8 Hz 带通。
滤波使用最多 8 秒上下文，保留最后 4 秒；它与界面显示滤波分别运行，不会重写原始 CSV。
按同一时间轴重采样到 125 Hz，分别按第 1/99 百分位归一化，拼接为：

```text
[PPG 500点 | BP占位零 500点 | ECG 500点 | 辅助占位零 500点]
```

模型以 `[1, 1, 2000]` 的 float32 输入执行 `model_flag="021"`，输出 500 点 BP，
按 `BP_mmhg = BP_norm * 50 + 100` 反归一化。
当前任务实际执行配置中的 50 步扩散采样；旧脚本的 `sample_steps=1` 不会把双条件任务变成一步推理。
固定随机种子 42，使用一次采样，便于复现同一输入的结果。

ECG 近 ADC 上下限的点超过 5%、明显丢样、时间倒退或 PPG 接触突变等情况，会显示原因并跳过预测。
输出非有限值、明显越界或近似平线时也会报错，不会把异常输出强行截成正常血压数字。
这些检查用于排除明显采集问题，通过检查仍不代表血压准确性已经验证。

## 后台运行和文件

下面的 GUI 文件位于 `src/ecg_ppg/gui/`；模型相关文件位于 `src/ecg_ppg/models/`。
启动命令均在项目根目录运行，目录导航见 [根目录 README](../README.md)。

- `serial_ecg_ppg_gui.py`：预测页面、实时缓冲、任务调度、结果导出。
- `unicardio_client.py`：后台线程启动独立模型 Python 进程，单任务调度和结果队列。
- `unicardio_worker.py`：持久模型进程，加载一次权重，通过 JSON 行协议接收任务。
- `unicardio_inference.py`：模型专用预处理、质量检查和预测。
- `run_unicardio_bp.py`：沿用的离线批量入口；权重改为严格匹配，并修复模型不可用时片段导出越界。

GUI 不导入 torch，不安装模型依赖也能继续使用采集、显示和录制。
默认模型目录是同级 `UniCardio/UniCardio/UniCardio-main/base_model`，默认模型 Python 是其上层
`venv/Scripts/python.exe`。可用环境变量 `ECG_PPG_MODEL_DIR`、`ECG_PPG_MODEL_PY` 覆盖。
模型进程使用两个 CPU 计算线程；CUDA 可用时由原模型加载器选择 GPU。
模型日志写到 `outputs/bp_validation/worker.log`，子进程无控制台窗口，关闭上位机时会终止该子进程。

如需重建模型环境，使用 `requirements-unicardio.txt`；不要照搬原仓库含 Linux 绝对路径的完整环境导出。
本次使用已有环境，没有更改已安装依赖，也没有改动 UniCardio 权重和模型源码。

本次检查的权重 `no_compress799.pth` 的 SHA-256：

```text
585938cb6ca01030e5c6150355abaa4e3bf0249352ea67cf7e994356f70535ae
```

## 验证结果

修改前 15 项采集、协议和显示测试通过。新增测试覆盖输入时间对齐、独立滤波、原始数据不变、
占位布局、饱和/中断/接触突变拦截、过期任务结果丢弃、单任务调度和预测导出。
真实权重测试从 GUI 调用后台进程，同时继续注入串口测试帧，检查采集和录制仍工作、关闭时模型退出。

合成输入与历史实采输入均完成了真实权重预测。历史实采末段为 26.028806–30.028806 秒，
模型估计 SBP 125.48、DBP 80.36、MAP 97.72 mmHg，CPU 计算约 22.3 秒。
历史数据没有同步参考血压，这些数值不能作为误差或准确性验证。
本轮端口检查未发现 COM13，尚未完成本轮在线人体实采预测。
验证图、预测 CSV、报告和日志保存在 `outputs/bp_validation/`，不加入 Git。

运行常规测试：

```powershell
& ".\.venv\Scripts\python.exe" -B -m unittest `
  tests.test_acquisition_link tests.test_waveform_display tests.test_unicardio_integration -v
```

运行真实权重集成测试：

```powershell
$env:ECG_PPG_TEST_MODEL = '1'
& ".\.venv\Scripts\python.exe" -B -m unittest `
  tests.test_unicardio_integration.GuiTests.test_real_model_in_gui_while_acquisition_continues -v
```

设置 `ECG_PPG_TEST_CSV` 为已有同步 CSV 路径，可改为验证该文件末段；不设置时使用标注清楚的合成输入。

## Git 与回退

组合仓库的 Git 根目录位于 `ecg_ppg`，上位机和下位机一起由 `main` 分支管理。
原开发目录中的历史分支、标签和本地记录没有迁入本次源码快照。
回退前先保存当前修改，再根据本仓库的 `git log` 选择实际存在的提交。
虚拟环境、采集记录、模型与验证产物由 `.gitignore` 排除。
