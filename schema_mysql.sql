CREATE DATABASE IF NOT EXISTS battery_health CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE battery_health;

CREATE TABLE battery_info (
  battery_id VARCHAR(32) PRIMARY KEY,
  source_file VARCHAR(512),
  record_count INT NOT NULL DEFAULT 0,
  chemistry VARCHAR(80),
  nominal_capacity_Ah DECIMAL(10,4),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE battery_cycle_record (
  source_row BIGINT PRIMARY KEY,
  battery_id VARCHAR(32) NOT NULL,
  cycle_index INT NOT NULL,
  capacity_Ah DECIMAL(12,6),
  voltage_mean DECIMAL(10,6), voltage_min DECIMAL(10,6), voltage_max DECIMAL(10,6),
  current_mean DECIMAL(10,6), temperature_mean DECIMAL(10,6), time_max DECIMAL(14,4),
  quality_flag VARCHAR(64), quality_reason VARCHAR(512), source_file VARCHAR(512),
  processed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_battery_cycle (battery_id, cycle_index),
  KEY idx_record_battery (battery_id),
  CONSTRAINT fk_cycle_battery FOREIGN KEY (battery_id) REFERENCES battery_info(battery_id)
);

CREATE TABLE battery_soh_result (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  source_row BIGINT NOT NULL,
  battery_id VARCHAR(32) NOT NULL,
  cycle_index INT NOT NULL,
  capacity_Ah DECIMAL(12,6) NOT NULL,
  reference_capacity_Ah DECIMAL(12,6) NOT NULL,
  soh_pct DECIMAL(12,6) NOT NULL,
  data_quality_flag VARCHAR(64) NOT NULL,
  reference_rule VARCHAR(255) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_soh_record (battery_id, cycle_index),
  CONSTRAINT fk_soh_record FOREIGN KEY (source_row) REFERENCES battery_cycle_record(source_row),
  CONSTRAINT fk_soh_battery FOREIGN KEY (battery_id) REFERENCES battery_info(battery_id)
);

CREATE TABLE model_training_run (
  run_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  model_name VARCHAR(80) NOT NULL,
  model_version VARCHAR(80) NOT NULL,
  feature_config JSON NOT NULL,
  split_config JSON NOT NULL,
  metrics JSON,
  started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TIMESTAMP NULL,
  run_status VARCHAR(32) NOT NULL
);

CREATE TABLE model_prediction_result (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id BIGINT NOT NULL,
  battery_id VARCHAR(32) NOT NULL,
  cycle_index INT NOT NULL,
  target_cycle_index INT NOT NULL,
  actual_soh_pct DECIMAL(12,6),
  predicted_soh_pct DECIMAL(12,6) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_prediction_battery (battery_id, cycle_index),
  CONSTRAINT fk_prediction_run FOREIGN KEY (run_id) REFERENCES model_training_run(run_id),
  CONSTRAINT fk_prediction_battery FOREIGN KEY (battery_id) REFERENCES battery_info(battery_id)
);

CREATE TABLE system_operation_log (
  operation_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  operation_type VARCHAR(40) NOT NULL,
  source_name VARCHAR(512),
  record_count INT,
  battery_count INT,
  source_sha256 CHAR(64),
  status VARCHAR(24) NOT NULL,
  details JSON,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_operation_time (operation_id)
);

CREATE TABLE anomaly_detection_result (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  source_row BIGINT NOT NULL,
  battery_id VARCHAR(32) NOT NULL,
  cycle_index INT NOT NULL,
  method_name VARCHAR(80) NOT NULL,
  method_version VARCHAR(80) NOT NULL,
  distance_value DECIMAL(14,8) NOT NULL,
  threshold_value DECIMAL(14,8) NOT NULL,
  is_anomaly BOOLEAN NOT NULL,
  feature_config JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_anomaly_record (source_row, method_name, method_version),
  CONSTRAINT fk_anomaly_record FOREIGN KEY (source_row) REFERENCES battery_cycle_record(source_row),
  CONSTRAINT fk_anomaly_battery FOREIGN KEY (battery_id) REFERENCES battery_info(battery_id)
);

