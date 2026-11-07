-- All timestamps are UTC ISO-8601 strings ending in Z.
CREATE TABLE roles (
  role_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  department TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','closed')),
  requirements_json TEXT NOT NULL,
  stage_order_json TEXT NOT NULL,
  stage_targets_json TEXT NOT NULL
);

CREATE TABLE candidates (
  candidate_id TEXT PRIMARY KEY,
  role_id TEXT NOT NULL REFERENCES roles(role_id),
  name TEXT NOT NULL,
  contact_email TEXT NOT NULL,
  cv_text TEXT NOT NULL,
  current_stage TEXT NOT NULL,
  application_status TEXT NOT NULL CHECK (application_status IN ('active','rejected','withdrawn')),
  applied_at TEXT NOT NULL
);

CREATE TABLE stage_events (
  event_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  stage TEXT NOT NULL,
  entered_at TEXT NOT NULL,
  scheduled_at TEXT,      -- when the interview was booked (interview stages only)
  interview_at TEXT,      -- when it took place (past only)
  exited_at TEXT,
  exit_outcome TEXT CHECK (exit_outcome IN ('advanced','rejected','withdrawn')),
  reason_code TEXT CHECK (reason_code IN (
    'missing_required_skill','lower_interview_signal','compensation_mismatch',
    'accepted_other_offer','withdrew_scheduling_delay','withdrew_personal',
    'withdrew_no_reason','no_response')),
  reason_detail TEXT,
  exit_reason TEXT
);

CREATE TABLE interview_feedback (
  feedback_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  stage TEXT NOT NULL,
  interview_at TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  interviewer TEXT NOT NULL,
  criteria_json TEXT NOT NULL,
  summary_text TEXT NOT NULL,
  recommendation TEXT NOT NULL CHECK (recommendation IN ('strong_yes','yes','mixed','no','strong_no'))
);

CREATE TABLE email_messages (
  email_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  sent_at TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
  subject TEXT NOT NULL,
  body TEXT NOT NULL
);

CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  candidate_id TEXT REFERENCES candidates(candidate_id),
  role_id TEXT NOT NULL REFERENCES roles(role_id),
  task_type TEXT NOT NULL CHECK (task_type IN ('schedule_interview','collect_feedback','send_update','other')),
  description TEXT NOT NULL,
  owner TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','completed','cancelled')),
  created_at TEXT NOT NULL,
  due_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE INDEX idx_events_cand ON stage_events(candidate_id);
CREATE INDEX idx_fb_cand ON interview_feedback(candidate_id);
CREATE INDEX idx_email_cand ON email_messages(candidate_id);
CREATE INDEX idx_task_role ON tasks(role_id);
