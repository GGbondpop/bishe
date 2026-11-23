"""HTTP smoke checks using an isolated temporary SQLite database."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import shutil
import urllib.error
import urllib.request
from pathlib import Path


SYSTEM_DIR = Path(__file__).absolute().parents[1]
ROOT = SYSTEM_DIR.parent
BACKEND_DIR = SYSTEM_DIR / "backend"
DB_ROOT = SYSTEM_DIR / "database"
DB_ROOT.mkdir(parents=True, exist_ok=True)
temp_dir = Path(tempfile.mkdtemp(prefix="api-smoke-", dir=str(DB_ROOT)))
results = []
process = None


def request(method, url, body=None, headers=None, timeout=25):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def check(name, passed, detail):
    results.append({"test_item": name, "status": "通过" if passed else "失败", "detail": detail})


def multipart(filename, contents):
    boundary = "----BatteryHealthSmokeBoundary7MA4YWxkTrZu0gW"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: text/csv\r\n\r\n").encode("utf-8") + contents + f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


try:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    db_path = (temp_dir / "smoke.db").as_posix()
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                                "--port", str(port), "--log-level", "error"], cwd=str(BACKEND_DIR),
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    healthz = None
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("API server exited: " + (process.stderr.read() or ""))
        try:
            healthz = request("GET", base + "/api/healthz", timeout=1)
            break
        except (OSError, urllib.error.URLError):
            time.sleep(.25)
    check("API健康检查", bool(healthz and healthz[0] == 200 and json.loads(healthz[1]).get("ok")), str(healthz[0] if healthz else "timeout"))

    csv_ok = (b"source_row,battery_id,cycle_index,capacity_Ah,voltage_mean,voltage_min,current_mean,temperature_mean,time_max\n"
              b"1,T9001,1,1.02,3.70,2.80,-1.0,25.0,1800\n2,T9001,2,0.99,3.68,2.78,-1.1,25.2,1780\n3,T9001,3,0.96,3.65,2.75,-1.2,25.5,1750\n"
              b"4,T9002,1,0.98,3.60,2.70,-1.0,24.0,1750\n5,T9002,2,0.95,3.58,2.68,-1.1,24.3,1720\n6,T9002,3,0.91,3.54,2.64,-1.3,24.8,1680\n")
    body, headers = multipart("smoke.csv", csv_ok)
    status, payload, _ = request("POST", base + "/api/data/import", body, headers)
    parsed = json.loads(payload)
    check("CSV正常导入", status == 200 and parsed.get("imported_records") == 6 and parsed.get("batteries") == 2 and parsed.get("soh_records") == 6, f"HTTP {status}; {parsed}")

    body, headers = multipart("empty.csv", b"")
    status, payload, _ = request("POST", base + "/api/data/import", body, headers)
    check("空CSV拒绝", status == 400, f"HTTP {status}")
    body, headers = multipart("missing.csv", b"battery_id,cycle_index\nT9001,1\n")
    status, payload, _ = request("POST", base + "/api/data/import", body, headers)
    check("缺字段CSV拒绝", status == 422, f"HTTP {status}")

    status, payload, _ = request("GET", base + "/api/data/batteries")
    check("电池列表查询", status == 200 and len(json.loads(payload)) == 2, f"HTTP {status}")
    status, payload, _ = request("GET", base + "/api/data/records?page=1&page_size=10")
    records = json.loads(payload)
    check("周期记录分页查询", status == 200 and len(records.get("items", [])) == 6, f"HTTP {status}; rows={len(records.get('items', []))}")
    status, payload, _ = request("GET", base + "/api/data/records?battery_id=T9001&page=1&page_size=10")
    filtered = json.loads(payload)
    check("按电池筛选周期记录", status == 200 and filtered.get("total") == 3, f"HTTP {status}; total={filtered.get('total')}")
    status, payload, _ = request("GET", base + "/api/data/export?battery_id=T9001")
    check("筛选记录CSV导出", status == 200 and payload.count(b"T9001") == 3, f"HTTP {status}; bytes={len(payload)}")
    status, payload, _ = request("GET", base + "/api/health/summary")
    check("健康汇总查询", status == 200 and "mean_soh_pct" in json.loads(payload), f"HTTP {status}")
    status, payload, _ = request("GET", base + "/api/health/T9001")
    check("单电池SOH趋势查询", status == 200 and len(json.loads(payload).get("records", [])) == 3 and "capacity_Ah" in json.loads(payload)["records"][0], f"HTTP {status}")

    status, payload, _ = request("POST", base + "/api/models/train", body=b"{}", headers={"Content-Type": "application/json"}, timeout=90)
    train_result = json.loads(payload) if status == 200 else {}
    check("模型训练触发与数据库留痕", status == 200 and train_result.get("status") == "completed" and len(train_result.get("metrics", [])) == 3, f"HTTP {status}")

    model = json.loads((ROOT / "模型结果" / "linear_regression_model.json").read_text(encoding="utf-8"))
    feature_payload = {name: (90.0 if name == "soh_pct" else 0.0 if name == "capacity_degradation_rate" else 2.0) for name in model["features"]}
    prediction_body = json.dumps({"battery_id": "T9001", "cycle_index": 1, "features": feature_payload}).encode("utf-8")
    status, payload, _ = request("POST", base + "/api/models/predict", prediction_body, {"Content-Type": "application/json"})
    prediction = json.loads(payload) if status == 200 else {}
    check("模型特征预测", status == 200 and "linear_regression_prediction" in prediction and "decision_tree_prediction" in prediction, f"HTTP {status}")

    status, payload, _ = request("POST", base + "/api/anomaly/detect", body=b"{}", headers={"Content-Type": "application/json"})
    anomaly = json.loads(payload) if status == 200 else {}
    check("异常结果查询", status == 200 and "threshold" in anomaly and len(anomaly.get("items", [])) == 6 and "mahalanobis_distance" in anomaly["items"][0], f"HTTP {status}")
    status, payload, _ = request("GET", base + "/api/inspection/priority?limit=5")
    priority = json.loads(payload) if status == 200 else []
    check("巡检优先级查询", status == 200 and len(priority) == 5 and "soh_pct" in priority[0], f"HTTP {status}")
    status, payload, _ = request("GET", base + "/api/reports/export?report=inspection")
    check("导入数据巡检CSV导出", status == 200 and b"priority_score" in payload and b"T9001" in payload, f"HTTP {status}")
    status, payload, _ = request("GET", base + "/api/reports/export?report=inspection&battery_id=T9001")
    check("按电池筛选巡检导出", status == 200 and payload.count(b"T9001") == 3 and b"T9002" not in payload, f"HTTP {status}; bytes={len(payload)}")
    status, payload, _ = request("GET", base + "/api/reports/export?report=metrics")
    check("评价结果CSV导出", status == 200 and b"MAE_percentage_points" in payload, f"HTTP {status}")
finally:
    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)
    shutil.rmtree(str(temp_dir), ignore_errors=True)

out = ROOT / "模型结果" / "api_smoke_test_results.csv"
import pandas as pd
pd.DataFrame(results).to_csv(out, index=False, encoding="utf-8-sig")
print(json.dumps({"passed": sum(r["status"] == "通过" for r in results), "total": len(results),
                  "results": results, "report": str(out)}, ensure_ascii=False, indent=2))
if any(r["status"] != "通过" for r in results):
    raise SystemExit(1)
