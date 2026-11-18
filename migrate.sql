BEGIN;
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.users (
 user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 employee_id TEXT UNIQUE REFERENCES org.employees(employee_id), active BOOLEAN NOT NULL DEFAULT TRUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.user_roles (
 user_id TEXT NOT NULL REFERENCES app.users(user_id), role TEXT NOT NULL
 CHECK(role IN ('admin','sales','finance','employee','manager','executive')),
 PRIMARY KEY(user_id,role)
);
CREATE TABLE IF NOT EXISTS app.sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES app.users(user_id),
 csrf_token TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.audit_events (
 event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 actor_user_id TEXT REFERENCES app.users(user_id), action TEXT NOT NULL,
 entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, before_json JSONB,
 after_json JSONB, request_id TEXT, occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.idempotency_keys (
 user_id TEXT NOT NULL REFERENCES app.users(user_id), operation TEXT NOT NULL,
 request_key TEXT NOT NULL, request_hash TEXT, result_json JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(user_id,operation,request_key)
);
ALTER TABLE app.idempotency_keys ADD COLUMN IF NOT EXISTS request_hash TEXT;
CREATE TABLE IF NOT EXISTS app.finance_evidence (
 entity_kind TEXT NOT NULL CHECK(entity_kind IN ('receipt','expense')),
 entity_id TEXT NOT NULL, voucher_ref TEXT NOT NULL,
 created_by TEXT NOT NULL REFERENCES app.users(user_id),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(entity_kind,entity_id), UNIQUE(entity_kind,voucher_ref)
);
CREATE TABLE IF NOT EXISTS app.calendar_days (
 day DATE PRIMARY KEY, is_workday BOOLEAN NOT NULL, note TEXT,
 configured_by TEXT REFERENCES app.users(user_id), configured_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.daily_report_versions (
 version_id TEXT PRIMARY KEY, employee_id TEXT NOT NULL, report_date DATE NOT NULL,
 version_no INTEGER NOT NULL, narrative TEXT, completion_status TEXT, risk_note TEXT,
 next_plan TEXT, need_collaboration TEXT, submit_state TEXT NOT NULL
 CHECK(submit_state IN ('草稿','已提交')),
 created_by TEXT NOT NULL REFERENCES app.users(user_id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 FOREIGN KEY(employee_id,report_date) REFERENCES work.daily_reports(employee_id,report_date),
 UNIQUE(employee_id,report_date,version_no)
);
CREATE TABLE IF NOT EXISTS app.daily_report_reviews (
 review_id TEXT PRIMARY KEY, version_id TEXT NOT NULL UNIQUE REFERENCES app.daily_report_versions(version_id),
 reviewer_user_id TEXT NOT NULL REFERENCES app.users(user_id), decision TEXT NOT NULL
 CHECK(decision IN ('通过','退回')), comment TEXT,
 reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.employee_weekly_summaries (
 employee_id TEXT NOT NULL REFERENCES org.employees(employee_id), week_start DATE NOT NULL,
 source_fingerprint TEXT NOT NULL, model_name TEXT NOT NULL, prompt_version TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','ready','failed')),
 summary_json JSONB, error_message TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
 generated_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(employee_id,week_start,source_fingerprint,model_name,prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_app_sessions_expiry ON app.sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_app_versions_pending ON app.daily_report_versions(employee_id,report_date,version_no DESC);
COMMIT;
