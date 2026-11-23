from __future__ import annotations

import io
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, JSON, MetaData, String, Table, UniqueConstraint, create_engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError


ROOT = Path(__file__).absolute().parents[2]
DB_DIR = ROOT / "battery-health-system" / "database"
DB_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DB_DIR / 'battery_health.db').as_posix()}")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
metadata = MetaData()
battery_info = Table("battery_info", metadata,
    Column("battery_id", String(32), primary_key=True), Column("source_file", String(512)), Column("record_count", Integer, default=0))
battery_cycle_record = Table("battery_cycle_record", metadata,
    Column("source_row", Integer, primary_key=True), Column("battery_id", String(32), ForeignKey("battery_info.battery_id"), index=True),
    Column("cycle_index", Integer), Column("capacity_Ah", Float), Column("voltage_mean", Float),
    Column("voltage_min", Float), Column("current_mean", Float), Column("temperature_mean", Float),
    Column("time_max", Float), Column("quality_flag", String(64)), Column("quality_reason", String(512)),
    Column("source_file", String(512)), UniqueConstraint("battery_id", "cycle_index", name="uq_battery_cycle"))
battery_soh_result = Table("battery_soh_result", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True), Column("source_row", Integer, ForeignKey("battery_cycle_record.source_row"), nullable=False),
    Column("battery_id", String(32), ForeignKey("battery_info.battery_id"), nullable=False), Column("cycle_index", Integer, nullable=False),
    Column("capacity_Ah", Float, nullable=False), Column("reference_capacity_Ah", Float, nullable=False), Column("soh_pct", Float, nullable=False),
    Column("data_quality_flag", String(64), nullable=False), Column("reference_rule", String(255), nullable=False),
    UniqueConstraint("battery_id", "cycle_index", name="uq_soh_record"))
model_training_run = Table("model_training_run", metadata,
    Column("run_id", Integer, primary_key=True, autoincrement=True), Column("model_name", String(80), nullable=False),
    Column("model_version", String(80), nullable=False), Column("feature_config", JSON, nullable=False),
    Column("split_config", JSON, nullable=False), Column("metrics", JSON),
    Column("started_at", DateTime, server_default=func.now()), Column("finished_at", DateTime), Column("run_status", String(32), nullable=False))
model_prediction_result = Table("model_prediction_result", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True), Column("run_id", Integer, ForeignKey("model_training_run.run_id"), nullable=False),
    Column("battery_id", String(32), nullable=False), Column("cycle_index", Integer, nullable=False),
    Column("target_cycle_index", Integer, nullable=False), Column("actual_soh_pct", Float),
    Column("predicted_soh_pct", Float, nullable=False), Column("created_at", DateTime, server_default=func.now()))
system_operation_log = Table("system_operation_log", metadata,
    Column("operation_id", Integer, primary_key=True, autoincrement=True),
    Column("operation_type", String(40), nullable=False), Column("source_name", String(512)),
    Column("record_count", Integer), Column("battery_count", Integer), Column("source_sha256", String(64)),
    Column("status", String(24), nullable=False), Column("details", JSON),
    Column("created_at", DateTime, server_default=func.now()))
anomaly_detection_result = Table("anomaly_detection_result", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True), Column("source_row", Integer, ForeignKey("battery_cycle_record.source_row"), nullable=False),
    Column("battery_id", String(32), ForeignKey("battery_info.battery_id"), nullable=False), Column("cycle_index", Integer, nullable=False),
    Column("method_name", String(80), nullable=False), Column("method_version", String(80), nullable=False),
    Column("distance_value", Float, nullable=False), Column("threshold_value", Float, nullable=False),
    Column("is_anomaly", Integer, nullable=False), Column("feature_config", JSON, nullable=False),
    UniqueConstraint("source_row", "method_name", "method_version", name="uq_anomaly_record"))
anomaly_review_state = Table("anomaly_review_state", metadata,
    Column("source_row", Integer, primary_key=True), Column("review_status", String(24), nullable=False, default="待复核"),
    Column("note", String(1000), nullable=False, default=""), Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()))
metadata.create_all(engine)

app = FastAPI(title="锂离子电池健康评估与异常检测系统", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


class PredictionRequest(BaseModel):
    battery_id: str = Field(min_length=1, max_length=32)
    cycle_index: Optional[int] = None
    features: Dict[str, float] = Field(default_factory=dict)


class AnomalyReviewUpdate(BaseModel):
    status: str = Field(pattern="^(待复核|已确认|已排除)$")
    note: str = Field(default="", max_length=1000)


def output_file(*parts: str) -> Path:
    return ROOT.joinpath(*parts)


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig")


IMPORT_FIELD_ALIASES = {
    "battery_id": ("battery_id", "battery", "cell_id", "cell", "电池编号", "电芯编号", "电池id", "电芯id"),
    "cycle_index": ("cycle_index", "cycle", "cycle_number", "循环次数", "循环编号", "循环数", "周期", "周期编号"),
    "capacity_Ah": ("capacity_ah", "capacity", "capacitymah", "discharge_capacity", "discharge_capacity_ah", "discharge_capacity_mah", "放电容量", "放电容量ah", "放电容量mah", "容量", "容量ah", "容量mah"),
    "voltage_mean": ("voltage_mean", "mean_voltage", "voltage", "平均电压", "电压", "电压均值"),
    "voltage_min": ("voltage_min", "minimum_voltage", "最低电压", "电压最小值"),
    "current_mean": ("current_mean", "mean_current", "current", "平均电流", "电流", "电流均值"),
    "temperature_mean": ("temperature_mean", "mean_temperature", "temperature", "平均温度", "温度", "温度均值"),
    "time_max": ("time_max", "duration", "discharge_time", "放电时长", "放电时间", "持续时间"),
    "quality_flag": ("quality_flag", "quality", "data_quality", "数据质量", "质量标记"),
    "quality_reason": ("quality_reason", "quality_note", "数据质量说明", "质量原因", "复核说明"),
}
IMPORT_REQUIRED_FIELDS = ("battery_id", "cycle_index", "capacity_Ah")


def _normalize_column_name(value: str) -> str:
    value = str(value).strip().lower()
    return "".join(char for char in value if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def read_uploaded_table(payload: bytes, filename: str) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".csv":
            last_error = None
            for encoding in ("utf-8-sig", "utf-8", "gb18030"):
                try:
                    return pd.read_csv(io.BytesIO(payload), encoding=encoding)
                except UnicodeDecodeError as exc:
                    last_error = exc
            raise last_error or ValueError("无法识别 CSV 文件编码")
        if suffix == ".xlsx":
            return pd.read_excel(io.BytesIO(payload), engine="openpyxl")
        raise HTTPException(400, "当前支持 CSV 和 XLSX 文件；旧版 XLS 请先另存为 XLSX")
    except HTTPException:
        raise
    except (ValueError, pd.errors.ParserError, ImportError) as exc:
        raise HTTPException(400, f"无法读取文件：{exc}") from exc


def infer_import_mapping(columns):
    normalized = {_normalize_column_name(column): str(column) for column in columns}
    result = {}
    for field, aliases in IMPORT_FIELD_ALIASES.items():
        candidates = {_normalize_column_name(alias) for alias in aliases}
        result[field] = next((column for name, column in normalized.items() if name in candidates), "")
    capacity_column = result.get("capacity_Ah", "")
    capacity_unit = "mAh" if "mah" in _normalize_column_name(capacity_column) else "Ah"
    return result, capacity_unit


def json_records(frame: pd.DataFrame):
    if frame.empty:
        return []
    return json.loads(frame.where(pd.notna(frame), None).to_json(orient="records", force_ascii=False))


def _truthy(value) -> bool:
    if pd.isna(value):
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "y", "是"}


def derive_import_results(frame: pd.DataFrame):
    """Create database-backed SOH and anomaly results for an imported dataset."""
    data = frame.copy()
    data["capacity_Ah"] = pd.to_numeric(data["capacity_Ah"], errors="coerce")
    data["cycle_index"] = pd.to_numeric(data["cycle_index"], errors="coerce").astype(int)
    if "quality_flag" not in data:
        data["quality_flag"] = "导入数据待评估"
    if "quality_reason" not in data:
        data["quality_reason"] = ""
    usable = data["capacity_Ah"].notna() & data["capacity_Ah"].gt(0)
    if "capacity_label_usable" in data:
        usable &= data["capacity_label_usable"].map(_truthy)
    if "recommended_for_initial_training" in data:
        usable &= data["recommended_for_initial_training"].map(_truthy)
    flags = data["quality_flag"].fillna("").astype(str)
    usable &= ~flags.str.contains("待核查|待复核|需复核|不通过|排除|无效", regex=True)

    soh_rows = []
    series_rows = []
    reference_rule = "该电池前三条通过质量检查的正容量中位数；校准记录仅用于确定参考值"
    for battery_id, group in data.loc[usable].sort_values(["battery_id", "cycle_index", "source_row"]).groupby("battery_id"):
        if len(group) < 3:
            continue
        reference = float(group.head(3)["capacity_Ah"].median())
        if not reference > 0:
            continue
        prior_capacity = None
        prior_cycle = None
        for row in group.to_dict("records"):
            capacity = float(row["capacity_Ah"])
            cycle = int(row["cycle_index"])
            soh = 100.0 * capacity / reference
            flag = "待复核：容量型SOH超过120%" if soh > 120 else (str(row.get("quality_flag") or "通过质量检查"))
            soh_rows.append({"source_row": int(row["source_row"]), "battery_id": str(battery_id),
                "cycle_index": cycle, "capacity_Ah": capacity, "reference_capacity_Ah": reference, "soh_pct": soh,
                "data_quality_flag": flag, "reference_rule": reference_rule})
            gap = cycle - prior_cycle if prior_cycle is not None else 0
            degradation_rate = (-(capacity - prior_capacity) / prior_capacity / gap
                                if prior_capacity and prior_cycle is not None and gap > 0 else 0.0)
            series_rows.append({"source_row": int(row["source_row"]), "battery_id": str(battery_id),
                "cycle_index": cycle, "soh_pct": soh, "capacity_degradation_rate": float(degradation_rate),
                "voltage_mean": pd.to_numeric(row.get("voltage_mean"), errors="coerce"),
                "current_mean": pd.to_numeric(row.get("current_mean"), errors="coerce"),
                "temperature_mean": pd.to_numeric(row.get("temperature_mean"), errors="coerce"),
                "discharge_duration": pd.to_numeric(row.get("time_max"), errors="coerce")})
            prior_capacity, prior_cycle = capacity, cycle

    soh_df = pd.DataFrame(soh_rows)
    series = pd.DataFrame(series_rows)
    if series.empty:
        return soh_rows, [], []
    features = ["soh_pct", "capacity_degradation_rate", "voltage_mean", "current_mean", "temperature_mean", "discharge_duration"]
    matrix = series[features].replace([np.inf, -np.inf], np.nan)
    matrix = matrix.fillna(matrix.median()).fillna(0.0).to_numpy(dtype=float)
    mean = matrix.mean(axis=0)
    scale = matrix.std(axis=0)
    scale[scale < 1e-12] = 1.0
    z = (matrix - mean) / scale
    covariance = np.cov(z, rowvar=False) if len(z) > 1 else np.eye(z.shape[1])
    covariance = np.atleast_2d(covariance) + np.eye(z.shape[1]) * 1e-6
    inverse = np.linalg.pinv(covariance)
    distances = np.sqrt(np.maximum(0.0, np.einsum("ij,jk,ik->i", z, inverse, z)))
    threshold = float(np.quantile(distances, 0.975)) if len(distances) >= 10 else float(np.max(distances) + 1.0)
    anomaly_rows = []
    feature_config = {"features": features, "standardization": "global z-score", "threshold_quantile": 0.975}
    for row, distance in zip(series.to_dict("records"), distances):
        flagged = bool(distance > threshold)
        anomaly_rows.append({"source_row": row["source_row"], "battery_id": row["battery_id"],
            "cycle_index": row["cycle_index"], "method_name": "mahalanobis", "method_version": "1.0",
            "distance_value": float(distance), "threshold_value": threshold,
            "is_anomaly": int(flagged), "feature_config": feature_config})

    return soh_rows, anomaly_rows


@app.get("/api/healthz")
def healthz():
    return {"ok": True, "service": "battery-health-system"}


@app.get("/api/data/provenance")
def data_provenance():
    with engine.connect() as conn:
        imported = conn.execute(select(battery_info.c.source_file,
            func.sum(battery_info.c.record_count).label("record_count"),
            func.count().label("battery_count")).group_by(battery_info.c.source_file)).mappings().all()
    training = read_csv(output_file("建模数据", "soh_dataset.csv"))
    external_path = output_file("模型结果", "CX2_external_test_summary.json")
    external = json.loads(external_path.read_text(encoding="utf-8")) if external_path.exists() else {}
    imported_sources = [{"source_name": row["source_file"] or "未命名 CSV",
        "record_count": int(row["record_count"] or 0), "battery_count": int(row["battery_count"] or 0),
        "role": "当前应用数据库；导入时替换上一批数据库记录"} for row in imported]
    return {"database_sources": imported_sources,
        "internal_training": {"source_name": "NASA PCoE 电池循环数据", "record_count": int(len(training)),
            "battery_count": int(training.battery_id.nunique()) if not training.empty else 0,
            "role": "模型开发与按电芯划分的内部评价"},
        "external_test": {"source_name": "CALCE CX2 独立外部测试集", "ready": bool(external),
            "record_count": int(external.get("scored_rows", 0)), "battery_count": int(external.get("battery_count", 0)),
            "role": "只用于外部评价，不进入 NASA 训练流程"}}


@app.get("/api/experiments/history")
def experiment_history(limit: int = Query(50, ge=1, le=200)):
    with engine.connect() as conn:
        operations = conn.execute(select(system_operation_log).order_by(system_operation_log.c.operation_id.desc()).limit(limit)).mappings().all()
        model_runs = conn.execute(select(model_training_run).order_by(model_training_run.c.run_id.desc()).limit(limit)).mappings().all()
    items = [{"time": row["created_at"].isoformat() if row["created_at"] else None,
        "operation": row["operation_type"], "source_name": row["source_name"],
        "record_count": row["record_count"], "battery_count": row["battery_count"],
        "status": row["status"], "details": row["details"] or {}, "sha256": row["source_sha256"]} for row in operations]
    logged_models = {item["details"].get("model") for item in items
                     if item["operation"] == "model_training" and item["details"].get("model")}
    items.extend({"time": row["finished_at"].isoformat() if row["finished_at"] else None,
        "operation": "model_training", "source_name": "NASA PCoE 训练/内部测试集",
        "record_count": None, "battery_count": None, "status": row["run_status"],
        "details": {"model": row["model_name"], "metrics": row["metrics"], "split": row["split_config"]}, "sha256": None}
        for row in model_runs if row["model_name"] not in logged_models)
    items.sort(key=lambda item: item["time"] or "", reverse=True)
    return {"items": items[:limit]}


@app.post("/api/data/import/preview")
async def preview_import(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "请选择 CSV 或 XLSX 文件")
    payload = await file.read()
    if not payload:
        raise HTTPException(400, "文件为空")
    frame = read_uploaded_table(payload, file.filename)
    if frame.empty:
        raise HTTPException(400, "文件中没有数据行")
    frame.columns = [str(column).strip() for column in frame.columns]
    if len(set(frame.columns)) != len(frame.columns):
        raise HTTPException(422, "存在重名列。请先在表格中修改重复列名，再重新上传")
    mapping, capacity_unit = infer_import_mapping(frame.columns)
    missing = [field for field in IMPORT_REQUIRED_FIELDS if not mapping.get(field)]
    invalid_keys = None
    duplicate_cycles = None
    missing_capacity = None
    if not missing:
        battery_values = frame[mapping["battery_id"]].astype("string").str.strip()
        cycle_values = pd.to_numeric(frame[mapping["cycle_index"]], errors="coerce")
        capacity_values = pd.to_numeric(frame[mapping["capacity_Ah"]], errors="coerce")
        invalid_keys = int((battery_values.isna() | battery_values.eq("") | cycle_values.isna() | cycle_values.le(0) |
                            ~np.isclose(cycle_values.fillna(0), np.round(cycle_values.fillna(0)))).sum())
        duplicate_cycles = int(pd.DataFrame({"battery_id": battery_values, "cycle_index": cycle_values})
                               .duplicated(["battery_id", "cycle_index"]).sum())
        missing_capacity = int(capacity_values.isna().sum())
    sample = frame.head(8).astype(object).where(pd.notna(frame.head(8)), None)
    return {"filename": file.filename, "columns": list(frame.columns), "row_count": int(len(frame)),
            "mapping": mapping, "capacity_unit": capacity_unit,
            "missing_required_fields": missing,
            "quality_preview": {"invalid_battery_cycle_rows": invalid_keys,
                                "duplicate_battery_cycle_rows": duplicate_cycles,
                                "missing_capacity_rows": missing_capacity},
            "preview": json_records(sample),
            "notes": ["必需字段为电池编号、循环次数、放电容量。",
                      "电压、电流、温度、时长为可选字段；缺少时仍可计算容量型 SOH。",
                      "若容量列单位为 mAh，请确认单位选项，系统会换算为 Ah。"]}


@app.get("/api/data/import/template")
def import_template():
    template = pd.DataFrame([
        {"battery_id": "CELL_01", "cycle_index": 1, "capacity_Ah": 2.1, "voltage_mean": 3.7,
         "voltage_min": 2.8, "current_mean": -1.0, "temperature_mean": 25.0, "time_max": 3600},
        {"battery_id": "CELL_01", "cycle_index": 2, "capacity_Ah": 2.08, "voltage_mean": 3.68,
         "voltage_min": 2.8, "current_mean": -1.0, "temperature_mean": 25.2, "time_max": 3590},
        {"battery_id": "CELL_01", "cycle_index": 3, "capacity_Ah": 2.05, "voltage_mean": 3.65,
         "voltage_min": 2.8, "current_mean": -1.0, "temperature_mean": 25.4, "time_max": 3570},
    ])
    content = template.to_csv(index=False).encode("utf-8-sig")
    return StreamingResponse(io.BytesIO(content), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="battery_import_template.csv"'})


@app.post("/api/data/import")
async def import_data(file: UploadFile = File(...), mapping: str = Form(...),
                      source_name: str = Form("用户导入数据"), capacity_unit: str = Form("Ah")):
    if not file.filename:
        raise HTTPException(400, "请选择 CSV 或 XLSX 文件")
    try:
        payload = await file.read()
        if not payload:
            raise HTTPException(400, "文件为空")
        raw = read_uploaded_table(payload, file.filename)
        raw.columns = [str(column).strip() for column in raw.columns]
        if raw.empty:
            raise HTTPException(400, "文件中没有数据行")
        try:
            field_mapping = json.loads(mapping)
        except json.JSONDecodeError as exc:
            raise HTTPException(422, "字段对应关系格式无效，请重新预览文件") from exc
        if capacity_unit not in {"Ah", "mAh"}:
            raise HTTPException(422, "容量单位请选择 Ah 或 mAh")
        missing = [field for field in IMPORT_REQUIRED_FIELDS if not field_mapping.get(field)]
        if missing:
            labels = {"battery_id": "电池编号", "cycle_index": "循环次数", "capacity_Ah": "放电容量"}
            raise HTTPException(422, f"请为以下必需字段选择对应列：{', '.join(labels[field] for field in missing)}")
        selected_columns = [column for column in field_mapping.values() if column]
        invalid_columns = sorted(set(selected_columns) - set(raw.columns))
        if invalid_columns:
            raise HTTPException(422, "字段对应关系与当前文件不匹配，请重新预览并选择")
        frame = pd.DataFrame(index=raw.index)
        for field in IMPORT_FIELD_ALIASES:
            column = field_mapping.get(field)
            frame[field] = raw[column] if column else None
        frame["battery_id"] = frame["battery_id"].astype("string").str.strip()
        frame["cycle_index"] = pd.to_numeric(frame["cycle_index"], errors="coerce")
        numeric_fields = ("capacity_Ah", "voltage_mean", "voltage_min", "current_mean", "temperature_mean", "time_max")
        for col in numeric_fields:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        if capacity_unit == "mAh":
            frame["capacity_Ah"] /= 1000.0
        frame["quality_flag"] = frame["quality_flag"].fillna("").replace("", "导入数据待评估")
        frame["quality_reason"] = frame["quality_reason"].fillna("")
        invalid_key = frame["battery_id"].isna() | frame["battery_id"].eq("") | frame["cycle_index"].isna() | frame["cycle_index"].le(0)
        if invalid_key.any():
            raise HTTPException(422, f"有 {int(invalid_key.sum())} 行缺少有效电池编号或正整数循环次数。请修正数据后重新导入")
        if not np.all(np.isclose(frame["cycle_index"], np.round(frame["cycle_index"]))):
            raise HTTPException(422, "循环次数必须为正整数")
        frame["cycle_index"] = frame["cycle_index"].astype(int)
        if frame.duplicated(["battery_id", "cycle_index"]).any():
            raise HTTPException(422, "同一电池存在重复循环记录，请先确认并处理重复项")
        source_name = source_name.strip()[:200] or "用户导入数据"
        display_source = f"{source_name} / {file.filename}"[:512]
        frame["source_row"] = range(1, len(frame) + 1)
        frame["source_file"] = display_source
        soh_rows, anomaly_rows = derive_import_results(frame)
        source_sha256 = hashlib.sha256(payload).hexdigest()
        fields = ["source_row", "battery_id", "cycle_index", "capacity_Ah", "voltage_mean", "voltage_min", "current_mean", "temperature_mean", "time_max", "quality_flag", "quality_reason", "source_file"]
        with engine.begin() as conn:
            if engine.dialect.name == "mysql":
                for table_name in ("anomaly_review_state", "anomaly_detection_result", "battery_soh_result",
                                   "model_prediction_result", "model_training_run", "battery_cycle_record", "battery_info"):
                    conn.execute(text(f"DELETE FROM {table_name}"))
            else:
                conn.execute(anomaly_review_state.delete())
                conn.execute(anomaly_detection_result.delete())
                conn.execute(battery_soh_result.delete())
                conn.execute(model_prediction_result.delete())
                conn.execute(model_training_run.delete())
                conn.execute(battery_cycle_record.delete())
                conn.execute(battery_info.delete())
            insert_rows = frame[fields].astype(object).where(pd.notna(frame[fields]), None).to_dict("records")
            conn.execute(battery_cycle_record.insert(), insert_rows)
            counts = frame.groupby("battery_id").size()
            conn.execute(battery_info.insert(), [{"battery_id": str(b), "source_file": display_source, "record_count": int(n)} for b, n in counts.items()])
            if soh_rows: conn.execute(battery_soh_result.insert(), soh_rows)
            if anomaly_rows: conn.execute(anomaly_detection_result.insert(), anomaly_rows)
            conn.execute(system_operation_log.insert().values(
                operation_type="dataset_import", source_name=display_source, record_count=int(len(frame)),
                battery_count=int(frame["battery_id"].nunique()), source_sha256=source_sha256,
                status="completed", details={"replaced_current_database": True, "capacity_unit_in_file": capacity_unit,
                    "field_mapping": field_mapping, "soh_records": len(soh_rows), "anomaly_records": len(anomaly_rows)}))
        capacity_usable = frame["capacity_Ah"].notna() & frame["capacity_Ah"].gt(0)
        if "capacity_label_usable" in frame:
            capacity_usable &= frame["capacity_label_usable"].map(_truthy)
        if "recommended_for_initial_training" in frame:
            capacity_usable &= frame["recommended_for_initial_training"].map(_truthy)
        capacity_usable &= ~frame["quality_flag"].fillna("").astype(str).str.contains("待核查|待复核|需复核|不通过|排除|无效", regex=True)
        quality_report = {"total_records": int(len(frame)),
            "valid_capacity_records": int(capacity_usable.sum()),
            "excluded_capacity_records": int((~capacity_usable).sum()),
            "missing_capacity_records": int(frame["capacity_Ah"].isna().sum()),
            "non_positive_capacity_records": int((frame["capacity_Ah"].fillna(0) <= 0).sum()),
            "batteries": int(frame["battery_id"].nunique()),
            "optional_features_missing": [label for field, label in (("voltage_mean", "电压"), ("current_mean", "电流"), ("temperature_mean", "温度"), ("time_max", "时长")) if not field_mapping.get(field)],
            "anomaly_records": int(len(anomaly_rows)), "soh_records": int(len(soh_rows))}
        return {"imported_records": int(len(frame)), "batteries": int(frame["battery_id"].nunique()),
                "soh_records": len(soh_rows), "anomaly_records": len(anomaly_rows),
                "quality_report": quality_report,
                "filename": file.filename,
                "source_name": source_name, "source_sha256": source_sha256,
                "capacity_unit_converted_to": "Ah", "optional_features_available": {
                    name: bool(field_mapping.get(name)) for name in ("voltage_mean", "current_mean", "temperature_mean", "time_max")}}
    except HTTPException:
        raise
    except (ValueError, pd.errors.ParserError) as exc:
        raise HTTPException(400, f"数据文件解析失败：{exc}") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(500, "数据写入失败，请检查数据库连接和字段类型") from exc


@app.get("/api/data/batteries")
def batteries():
    with engine.connect() as conn:
        rows = conn.execute(select(battery_info).order_by(battery_info.c.battery_id)).mappings().all()
    if rows:
        return [dict(r) for r in rows]
    df = read_csv(output_file("建模数据", "battery_data_quality_summary.csv"))
    return json_records(df[[c for c in ("battery_id", "source_file", "model_ready_records") if c in df]])


@app.get("/api/data/records")
def records(battery_id: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500)):
    with engine.connect() as conn:
        count_query = select(func.count()).select_from(battery_cycle_record)
        if battery_id: count_query = count_query.where(battery_cycle_record.c.battery_id == battery_id)
        total = conn.execute(count_query).scalar_one()
        q = select(battery_cycle_record)
        if battery_id: q = q.where(battery_cycle_record.c.battery_id == battery_id)
        q = q.order_by(battery_cycle_record.c.battery_id, battery_cycle_record.c.cycle_index).offset((page-1)*page_size).limit(page_size)
        rows = conn.execute(q).mappings().all()
    if total: return {"page": page, "page_size": page_size, "total": total, "items": [dict(r) for r in rows]}
    df = read_csv(output_file("建模数据", "discharge_model_ready.csv"))
    if battery_id and not df.empty: df = df[df.battery_id == battery_id]
    total = len(df)
    return {"page": page, "page_size": page_size, "total": total,
            "items": json_records(df.iloc[(page-1)*page_size:page*page_size])}


@app.get("/api/data/export")
def export_data(battery_id: Optional[str] = None):
    with engine.connect() as conn:
        q = select(battery_cycle_record).order_by(battery_cycle_record.c.battery_id,
                                                   battery_cycle_record.c.cycle_index)
        if battery_id:
            q = q.where(battery_cycle_record.c.battery_id == battery_id)
        rows = conn.execute(q).mappings().all()
    if rows:
        frame = pd.DataFrame([dict(row) for row in rows])
    else:
        frame = read_csv(output_file("建模数据", "discharge_model_ready.csv"))
        if battery_id and not frame.empty:
            frame = frame[frame.battery_id == battery_id]
        if frame.empty:
            raise HTTPException(404, "没有可导出的记录")
    content = frame.to_csv(index=False)
    filename = "battery_records.csv"
    return StreamingResponse(io.BytesIO(content.encode("utf-8-sig")), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/health/summary")
def health_summary():
    with engine.connect() as conn:
        imported_batteries = conn.execute(select(func.count()).select_from(battery_info)).scalar_one()
        if imported_batteries:
            soh_rows = conn.execute(select(battery_soh_result)).mappings().all()
            record_count = conn.execute(select(func.count()).select_from(battery_cycle_record)).scalar_one()
            anomaly_count = conn.execute(select(func.count()).select_from(anomaly_detection_result).where(anomaly_detection_result.c.is_anomaly == 1)).scalar_one()
        else:
            soh_rows = None
    if soh_rows is not None:
        df = pd.DataFrame([dict(row) for row in soh_rows])
        if df.empty:
            return {"battery_count": int(imported_batteries), "valid_record_count": 0, "mean_soh_pct": None,
                    "review_record_count": 0, "excluded_record_count": int(record_count), "trend": []}
        trend = (df.groupby("cycle_index", as_index=False).soh_pct.mean().sort_values("cycle_index")
                 .iloc[::max(1, len(df)//150)])
        return {"battery_count": int(imported_batteries), "valid_record_count": int(len(df)),
                "mean_soh_pct": float(df.soh_pct.mean()), "review_record_count": int(anomaly_count),
                "excluded_record_count": int(max(0, record_count - len(df))), "trend": json_records(trend)}
    df = read_csv(output_file("建模数据", "soh_dataset.csv"))
    quality = read_csv(output_file("建模数据", "battery_data_quality_summary.csv"))
    anomaly = read_csv(output_file("模型结果", "mahalanobis_anomaly_results.csv"))
    return {"battery_count": int(df.battery_id.nunique()) if not df.empty else 0,
            "valid_record_count": int(len(df)), "mean_soh_pct": float(df.soh_pct.mean()) if not df.empty else None,
            "review_record_count": int(anomaly.statistical_anomaly.sum()) if not anomaly.empty else 0,
            "excluded_record_count": int(quality.excluded_records.sum()) if not quality.empty else 0,
            "trend": json_records(df.groupby("cycle_index", as_index=False).soh_pct.mean().sort_values("cycle_index").iloc[::max(1, len(df)//150)]) if not df.empty else []}


@app.get("/api/health/{battery_id}")
def battery_health(battery_id: str):
    with engine.connect() as conn:
        imported = conn.execute(select(func.count()).select_from(battery_info).where(battery_info.c.battery_id == battery_id)).scalar_one()
        if imported:
            rows = conn.execute(select(battery_soh_result).where(battery_soh_result.c.battery_id == battery_id)
                                .order_by(battery_soh_result.c.cycle_index)).mappings().all()
        else:
            rows = None
    if rows is not None:
        if not rows: raise HTTPException(422, "该电池有效容量不足3条，暂不能计算SOH趋势")
        df = pd.DataFrame([dict(row) for row in rows])
        return {"battery_id": battery_id, "reference_capacity_Ah": float(df.reference_capacity_Ah.iloc[0]),
                "reference_rule": str(df.reference_rule.iloc[0]),
                "records": json_records(df[["cycle_index", "capacity_Ah", "soh_pct", "data_quality_flag", "reference_rule"]]
                    if "capacity_Ah" in df else df)}
    df = read_csv(output_file("建模数据", "soh_dataset.csv"))
    if df.empty: raise HTTPException(503, "SOH 数据尚未生成")
    df = df[df.battery_id == battery_id].sort_values("cycle_index")
    if df.empty: raise HTTPException(404, "未找到该电池的可用 SOH 记录")
    return {"battery_id": battery_id, "reference_capacity_Ah": float(df.reference_capacity_Ah.iloc[0]),
            "reference_rule": str(df.reference_rule.iloc[0]),
            "records": json_records(df[["cycle_index", "capacity_Ah", "soh_pct", "data_quality_flag", "reference_rule"]])}


@app.post("/api/models/train")
def train_models():
    runner = ROOT / "实验流程" / "run_experiments.py"
    started = pd.Timestamp.utcnow().to_pydatetime().replace(tzinfo=None)
    proc = subprocess.run([sys.executable, str(runner)], cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    if proc.returncode:
        raise HTTPException(500, "模型训练失败；请查看后端日志")
    result = read_csv(output_file("模型结果", "regression_model_comparison.csv"))
    prediction_rows = read_csv(output_file("模型结果", "regression_predictions.csv"))
    linear = json.loads(output_file("模型结果", "linear_regression_model.json").read_text(encoding="utf-8"))
    tree = json.loads(output_file("模型结果", "decision_tree_model.json").read_text(encoding="utf-8"))
    now = pd.Timestamp.utcnow().to_pydatetime().replace(tzinfo=None)
    with engine.begin() as conn:
        known_batteries = set(conn.execute(select(battery_info.c.battery_id)).scalars().all())
        for model_name, feature_config, pred_col in [
            ("linear_regression", linear, "linear_regression_prediction"),
            ("decision_tree", tree, "decision_tree_prediction")]:
            metric_row = json.loads(result[result.model == model_name].iloc[0].to_json())
            run_id = conn.execute(model_training_run.insert().values(model_name=model_name, model_version="1.0",
                feature_config=feature_config, split_config={"train": linear["train_batteries"], "validation": linear["validation_batteries"], "test": linear["test_batteries"]},
                metrics=metric_row, started_at=started, finished_at=now, run_status="completed")).inserted_primary_key[0]
            conn.execute(system_operation_log.insert().values(operation_type="model_training",
                source_name="NASA PCoE 训练/内部测试集", record_count=int(metric_row.get("test_rows", 0)),
                battery_count=len(linear["test_batteries"]), status="completed",
                details={"model": model_name, "metrics": metric_row,
                         "split": {"train": linear["train_batteries"], "validation": linear["validation_batteries"],
                                   "test": linear["test_batteries"]}}))
            out_rows = []
            for row in prediction_rows.to_dict("records"):
                if str(row["battery_id"]) not in known_batteries:
                    continue
                out_rows.append({"run_id": run_id, "battery_id": row["battery_id"], "cycle_index": int(row["cycle_index"]),
                    "target_cycle_index": int(row["target_cycle_index"]), "actual_soh_pct": float(row["actual_next_soh_pct"]),
                    "predicted_soh_pct": float(row[pred_col])})
            if out_rows: conn.execute(model_prediction_result.insert(), out_rows)
    return {"status": "completed", "metrics": json_records(result)}


@app.get("/api/models/external-test")
def external_test_status():
    summary_path = output_file("模型结果", "CX2_external_test_summary.json")
    metrics_path = output_file("模型结果", "CX2_external_test_metrics.csv")
    if not summary_path.exists():
        source = output_file("测试数据")
        batteries = []
        if source.is_dir():
            for folder in sorted(path for path in source.iterdir() if path.is_dir()):
                files = [path for path in folder.iterdir() if path.is_file()]
                batteries.append({"battery_id": folder.name,
                                  "discharge_profile": {"CX2_33": "0.5C恒流放电", "CX2_34": "1C恒流放电",
                                                        "CX2_35": "0.5C恒流放电", "CX2_36": "1C恒流放电"}.get(folder.name, ""),
                                  "xlsx_files": sum(path.suffix.lower() == ".xlsx" for path in files),
                                  "txt_files_not_parsed": sum(path.suffix.lower() == ".txt" for path in files)})
        return {"ready": False, "dataset_label": "独立外部测试集（用户提供）",
                "training_and_external_test_mixed": False, "batteries": batteries}
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    metrics = read_csv(metrics_path)
    return {"ready": metrics_path.exists(), **summary, "metrics": json_records(metrics)}


@app.post("/api/models/evaluate-external")
def evaluate_external_test():
    runner = ROOT / "实验流程" / "evaluate_external_test.py"
    try:
        proc = subprocess.run([sys.executable, str(runner)], cwd=str(ROOT), capture_output=True,
                              text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "独立测试集处理超时，请稍后重试") from exc
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or "未知错误").strip().splitlines()[-1]
        raise HTTPException(422, f"独立测试集评估失败：{detail[:400]}")
    summary = json.loads(output_file("模型结果", "CX2_external_test_summary.json").read_text(encoding="utf-8"))
    metrics = read_csv(output_file("模型结果", "CX2_external_test_metrics.csv"))
    metrics_path = output_file("模型结果", "CX2_external_test_metrics.csv")
    with engine.begin() as conn:
        conn.execute(system_operation_log.insert().values(operation_type="external_evaluation",
            source_name="CALCE CX2 独立外部测试集", record_count=int(summary.get("scored_rows", 0)),
            battery_count=int(summary.get("battery_count", 0)),
            source_sha256=hashlib.sha256(metrics_path.read_bytes()).hexdigest(), status="completed",
            details={"training_test_mixed": False, "models": int(len(metrics))}))
    return {"ready": True, **summary, "metrics": json_records(metrics)}


@app.get("/api/models/external-test/export/{report}")
def export_external_test(report: str):
    files = {"metrics": "CX2_external_test_metrics.csv",
             "predictions": "CX2_external_test_predictions.csv",
             "records": "CX2_external_test_records.csv",
             "cleaned": "CX2_external_test_cleaned.csv"}
    if report not in files:
        raise HTTPException(404, "未知的独立测试集导出类型")
    folder = "建模数据" if report in {"records", "cleaned"} else "模型结果"
    path = output_file(folder, files[report])
    if not path.exists():
        raise HTTPException(404, "请先运行独立测试集评估")
    return FileResponse(path, media_type="text/csv; charset=utf-8", filename=files[report])


@app.post("/api/models/predict")
def predict(request: PredictionRequest):
    if request.features:
        linear = json.loads(output_file("模型结果", "linear_regression_model.json").read_text(encoding="utf-8"))
        tree = json.loads(output_file("模型结果", "decision_tree_model.json").read_text(encoding="utf-8"))
        def predict_linear(model):
            names = model["features"]
            missing = [n for n in names if n not in request.features]
            if missing: raise HTTPException(422, f"预测缺少特征：{', '.join(missing)}")
            z = [(float(request.features[n]) - m) / s for n, m, s in zip(names, model["feature_means"], model["feature_scales"])]
            return float(sum(a*b for a,b in zip(z, model["standardized_coefficients"])) + model["intercept"])
        def predict_tree(node, model):
            while "value" not in node:
                name = node["feature"]
                if name not in request.features: raise HTTPException(422, f"预测缺少特征：{name}")
                node = node["left"] if float(request.features[name]) <= node["threshold"] else node["right"]
            return float(node["value"])
        try:
            return {"battery_id": request.battery_id, "cycle_index": request.cycle_index,
                    "target": "next observed SOH (%)", "linear_regression_prediction": predict_linear(linear),
                    "decision_tree_prediction": predict_tree(tree["tree"], tree)}
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, f"预测特征无效：{exc}") from exc
    df = read_csv(output_file("模型结果", "regression_predictions.csv"))
    if df.empty: raise HTTPException(503, "还没有保存的测试集预测结果")
    df = df[df.battery_id == request.battery_id]
    if request.cycle_index is not None: df = df[df.cycle_index == request.cycle_index]
    if df.empty: raise HTTPException(404, "没有匹配的已保存预测；此接口不自动重训")
    return json_records(df)


@app.post("/api/anomaly/detect")
def anomaly_detect(battery_id: Optional[str] = None):
    with engine.connect() as conn:
        has_imports = conn.execute(select(func.count()).select_from(battery_info)).scalar_one()
    if has_imports:
        filters = []
        if battery_id:
            filters.append(anomaly_detection_result.c.battery_id == battery_id)
        with engine.connect() as conn:
            checked_query = select(func.count()).select_from(anomaly_detection_result)
            flagged_query = select(
                anomaly_detection_result.c.source_row,
                anomaly_detection_result.c.battery_id,
                anomaly_detection_result.c.cycle_index,
                anomaly_detection_result.c.distance_value,
                anomaly_detection_result.c.threshold_value,
                anomaly_detection_result.c.is_anomaly,
                battery_cycle_record.c.capacity_Ah,
                battery_cycle_record.c.voltage_mean,
                battery_cycle_record.c.current_mean,
                battery_cycle_record.c.temperature_mean,
                battery_cycle_record.c.time_max,
                battery_cycle_record.c.quality_reason,
            ).select_from(anomaly_detection_result.join(
                battery_cycle_record,
                anomaly_detection_result.c.source_row == battery_cycle_record.c.source_row,
            )).where(anomaly_detection_result.c.is_anomaly == 1)
            threshold_query = select(anomaly_detection_result.c.threshold_value).limit(1)
            if filters:
                checked_query = checked_query.where(*filters)
                flagged_query = flagged_query.where(*filters)
                threshold_query = threshold_query.where(*filters)
            checked_count = int(conn.execute(checked_query).scalar_one())
            rows = conn.execute(flagged_query.order_by(anomaly_detection_result.c.source_row)).mappings().all()
            threshold = conn.execute(threshold_query).scalar_one_or_none()
        df = pd.DataFrame([dict(row) for row in rows])
        if not df.empty:
            with engine.connect() as conn:
                reviews = conn.execute(select(anomaly_review_state).where(
                    anomaly_review_state.c.source_row.in_(df["source_row"].astype(int).tolist()))).mappings().all()
            review_map = {int(r["source_row"]): r for r in reviews}
            df["review_status"] = df["source_row"].map(lambda key: review_map.get(int(key), {}).get("review_status", "待复核"))
            df["review_note"] = df["source_row"].map(lambda key: review_map.get(int(key), {}).get("note", ""))
            df = df.rename(columns={"distance_value": "mahalanobis_distance",
                                   "threshold_value": "anomaly_threshold",
                                   "is_anomaly": "statistical_anomaly"})
            df["statistical_anomaly"] = df["statistical_anomaly"].astype(bool)
            df["review_reason"] = df.apply(lambda row: (
                f"多项测量特征与参考分布差异较大（距离 {row['mahalanobis_distance']:.2f}，参考界限 {row['anomaly_threshold']:.2f}）；"
                "建议对照容量、电压、电流、温度和放电时长原始记录。"), axis=1)
        return {"threshold": float(threshold) if threshold is not None else None,
                "checked_count": checked_count, "anomaly_count": int(len(df)), "items": json_records(df)}
    df = read_csv(output_file("模型结果", "mahalanobis_anomaly_results.csv"))
    if df.empty: raise HTTPException(503, "异常检测结果尚未生成")
    if battery_id: df = df[df.battery_id == battery_id]
    checked_count = int(len(df))
    if not df.empty and "source_row" in df:
        with engine.connect() as conn:
            reviews = conn.execute(select(anomaly_review_state).where(
                anomaly_review_state.c.source_row.in_(df["source_row"].astype(int).tolist()))).mappings().all()
        review_map = {int(r["source_row"]): r for r in reviews}
        df["review_status"] = df["source_row"].map(lambda key: review_map.get(int(key), {}).get("review_status", "待复核"))
        df["review_note"] = df["source_row"].map(lambda key: review_map.get(int(key), {}).get("note", ""))
    df["review_reason"] = df.apply(lambda row: (
        f"多项测量特征与训练参考分布差异较大（距离 {float(row['mahalanobis_distance']):.2f}，参考界限 {float(row['anomaly_threshold']):.2f}）；"
        "建议对照容量、电压、电流和放电时长原始记录。" if bool(row["statistical_anomaly"])
        else "未超过训练数据的统计复核界限。"), axis=1)
    df = df[df.statistical_anomaly.astype(bool)]
    return {"threshold": float(df.anomaly_threshold.iloc[0]) if not df.empty else None,
            "checked_count": checked_count, "anomaly_count": int(len(df)), "items": json_records(df)}


@app.put("/api/anomaly/{source_row}/review")
def update_anomaly_review(source_row: int, request: AnomalyReviewUpdate):
    if request.status not in {"待复核", "已确认", "已排除"}:
        raise HTTPException(422, "状态只能选择待复核、已确认或已排除")
    with engine.begin() as conn:
        found = conn.execute(select(anomaly_detection_result.c.source_row).where(
            anomaly_detection_result.c.source_row == source_row,
            anomaly_detection_result.c.is_anomaly == 1).limit(1)).scalar_one_or_none()
        has_imported_dataset = conn.execute(select(func.count()).select_from(battery_info)).scalar_one() > 0
        if found is None and has_imported_dataset:
            raise HTTPException(404, "找不到这条异常记录，请刷新列表")
        if found is None:
            fallback = read_csv(output_file("模型结果", "mahalanobis_anomaly_results.csv"))
            if fallback.empty or "source_row" not in fallback or not (pd.to_numeric(fallback.source_row, errors="coerce") == source_row).any():
                raise HTTPException(404, "找不到这条异常记录，请刷新列表")
        exists = conn.execute(select(anomaly_review_state.c.source_row).where(
            anomaly_review_state.c.source_row == source_row)).scalar_one_or_none()
        values = {"review_status": request.status, "note": request.note.strip()}
        if exists is None:
            conn.execute(anomaly_review_state.insert().values(source_row=source_row, **values))
        else:
            conn.execute(anomaly_review_state.update().where(anomaly_review_state.c.source_row == source_row).values(**values))
    return {"source_row": source_row, **values}


@app.get("/api/reports/export")
def export_report(report: str = Query("anomaly", pattern="^(anomaly|soh|metrics)$"),
                  battery_id: Optional[str] = None):
    names = {"anomaly": "mahalanobis_anomaly_results.csv", "soh": "soh_dataset.csv",
             "metrics": "regression_model_comparison.csv"}
    if report in {"anomaly", "soh"}:
        with engine.connect() as conn:
            imported = conn.execute(select(func.count()).select_from(battery_info)).scalar_one()
            if imported:
                if report == "anomaly":
                    q = select(anomaly_detection_result).order_by(anomaly_detection_result.c.source_row)
                else:
                    q = select(battery_soh_result).order_by(battery_soh_result.c.battery_id, battery_soh_result.c.cycle_index)
                if battery_id:
                    column = anomaly_detection_result.c.battery_id if report == "anomaly" else battery_soh_result.c.battery_id
                    q = q.where(column == battery_id)
                frame = pd.DataFrame([dict(r) for r in conn.execute(q).mappings().all()])
                if report == "anomaly" and not frame.empty:
                    frame = frame.rename(columns={"distance_value": "mahalanobis_distance", "threshold_value": "anomaly_threshold", "is_anomaly": "statistical_anomaly"})
                if report == "soh" and not frame.empty:
                    frame = frame.drop(columns=["id"], errors="ignore")
                if frame.empty:
                    frame = pd.DataFrame(columns=["battery_id", "cycle_index"])
                content = frame.to_csv(index=False, encoding="utf-8-sig")
                return StreamingResponse(io.BytesIO(content.encode("utf-8-sig")), media_type="text/csv; charset=utf-8",
                                         headers={"Content-Disposition": f'attachment; filename="{names[report]}"'})
    folder = "模型结果" if report != "soh" else "建模数据"
    path = output_file(folder, names[report])
    if not path.exists(): raise HTTPException(404, "报告尚未生成")
    return FileResponse(path, media_type="text/csv; charset=utf-8", filename=names[report])
