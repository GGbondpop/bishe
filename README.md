# 基于多源循环数据的锂离子电池健康状态评估与异常检测系统

本系统围绕毕业设计题目完成电池循环数据导入、基础清洗、SOH 健康状态评估和异常记录筛查。界面主线是“导入数据 → 查看健康变化 → 复核异常记录”；模型与外部验证作为实验功能保留。

## 系统功能

- **数据管理**：导入 CSV/XLSX，预览字段并映射电芯编号、循环次数和放电容量；支持 Ah/mAh 换算，并检查无效编号、缺失容量和重复循环。
- **健康评估**：按电芯计算容量型 SOH，查看单电芯和数据集总体趋势。SOH 参考容量采用该电芯前三条合格容量的中位数，属于研究计算规则，不是厂家额定容量。
- **异常检测**：使用正则化马氏距离筛查与同批数据参考分布差异较大的记录，显示相关测量值，支持人工复核状态和备注。
- **实验评价**：查看 NASA PCoE 内部模型评价结果，并在独立 CALCE CX2 数据上运行外部评价。

系统不提供剩余寿命预测、巡检优先级打分或生产线质量判定。异常提示是统计复核线索，不代表电芯故障结论。当前系统分析循环级数据，不连接车辆 BMS，也不直接处理原始实时采样流。

## 数据用途与文件

- `C:\bishe\可导入数据`：适用于页面右上角“导入循环数据”的数据文件，包括 HUST 循环数据和转换后的 KIproBatt 批次容量数据。每次导入会替换当前通用分析批次，不会进入模型训练。
- `C:\bishe\建模数据`：NASA PCoE 清洗及模型开发数据。NASA 按电芯划分训练、验证和内部测试，供模型评价使用。
- `C:\bishe\测试数据`：CALCE CX2 外部测试文件，只在“模型与外部验证”页单独运行，不与 NASA 训练数据合并。
- `C:\bishe\原数据`：原始 NASA MAT 文件及其他原始来源文件，供清洗与实验流程使用。

不同数据来源承担不同用途。通用导入仅更新当前 SOH 和异常分析数据；它不会自动重训 NASA 模型，也不会改变 CALCE 外部测试结果。

## 启动

在 PowerShell 中分别启动后端和前端：

```powershell
cd C:\bishe\battery-health-system
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

另开一个 PowerShell 窗口：

```powershell
cd C:\bishe\battery-health-system\frontend
npm.cmd run dev -- --host 127.0.0.1
```

浏览器访问 `http://127.0.0.1:5173/`。如前端依赖尚未安装，在 frontend 目录运行 `npm.cmd install`。

## 技术组成

- 前端：Vue 3、ECharts、Vite
- 后端：Python、FastAPI、Pandas、NumPy
- 数据库：默认 SQLite；MySQL 表定义位于 `database/schema_mysql.sql`
- 离线实验：`C:\bishe\实验流程\run_experiments.py`；CALCE 评估：`C:\bishe\实验流程\evaluate_external_test.py`
