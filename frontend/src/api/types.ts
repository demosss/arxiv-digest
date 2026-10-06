// Типы REST API. Описаны вручную по схемам backend (app/schemas); даты — ISO-строки в UTC.

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface ApiErrorBody {
  detail: string | { loc: (string | number)[]; msg: string; type?: string }[];
  code: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface User {
  id: number;
  username: string;
  is_active: boolean;
}

export interface PublicSettings {
  timezone: string;
  app_env: string;
  llm_base_url: string;
  llm_model: string;
  llm_context_chars: number;
  arxiv_max_results_per_topic: number;
  send_empty_digest: boolean;
  pipeline_stale_hours: number;
}

// --- Темы и получатели ---

export interface Topic {
  id: number;
  name: string;
  keywords: string[];
  arxiv_categories: string[];
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface TopicInput {
  name: string;
  keywords: string[];
  arxiv_categories: string[];
  is_active: boolean;
}

export interface Recipient {
  id: number;
  email: string;
  name: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface RecipientInput {
  email: string;
  name: string | null;
  is_active: boolean;
}

// --- Расписания ---

export type TaskType = 'full_pipeline' | 'collect' | 'summarize' | 'build_digest' | 'send_digest';

export interface Schedule {
  id: number;
  name: string;
  task_type: TaskType;
  cron_expression: string;
  timezone: string;
  is_active: boolean;
  last_fired_at: string | null;
  next_fire_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ScheduleInput {
  name: string;
  task_type: TaskType;
  cron_expression: string;
  timezone: string;
  is_active: boolean;
}

export interface CronValidation {
  valid: boolean;
  error: string | null;
  cron_expression: string | null;
  next_runs: string[];
}

// --- SMTP ---

export type SmtpSecurity = 'none' | 'starttls' | 'ssl';

export interface SmtpSettingsInput {
  host: string;
  port: number;
  username: string;
  from_email: string;
  from_name: string;
  security: SmtpSecurity;
  timeout_seconds: number;
}

export interface SmtpSettings extends SmtpSettingsInput {
  password_set: boolean;
  is_configured: boolean;
  updated_at: string;
}

// --- Запуски и логи ---

export type RunStatus = 'queued' | 'running' | 'success' | 'failed' | 'skipped' | 'cancelled';
export type RunTrigger = 'schedule' | 'manual';

export interface Run {
  id: number;
  task_type: TaskType;
  trigger: RunTrigger;
  schedule_id: number | null;
  parent_run_id: number | null;
  status: RunStatus;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  params: Record<string, unknown>;
  stats: Record<string, unknown>;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunDetail extends Run {
  celery_task_id: string | null;
  traceback: string | null;
  children: Run[];
}

export interface RunCreate {
  task_type: TaskType;
  period_start?: string;
  period_end?: string;
  digest_id?: number;
  paper_ids?: number[];
  force?: boolean;
  retry_failed?: boolean;
}

export type LogLevel = 'debug' | 'info' | 'warning' | 'error' | 'critical';

export interface LogEntry {
  id: number;
  run_id: number;
  ts: string;
  level: LogLevel;
  logger: string;
  message: string;
  extra: Record<string, unknown>;
}

export interface LogPage {
  items: LogEntry[];
  total: number;
  last_id: number | null;
}

// --- Статьи ---

export type PaperStatus = 'new' | 'downloaded' | 'text_extracted' | 'summarized' | 'failed';

export interface PaperTopic {
  id: number;
  name: string;
  matched_keywords: string[];
}

export interface Paper {
  id: number;
  arxiv_id: string;
  version: number;
  title: string;
  authors: string[];
  categories: string[];
  published_at: string;
  abs_url: string;
  pdf_url: string;
  status: PaperStatus;
  error: string | null;
  text_chars: number | null;
  created_at: string;
  topics: PaperTopic[];
  has_summary: boolean;
}

export interface Summary {
  text_ru: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  duration_ms: number;
  chunks_count: number;
  created_at: string;
  updated_at: string;
}

export interface PaperDetail extends Paper {
  abstract: string;
  updated_at_arxiv: string | null;
  updated_at: string;
  summary: Summary | null;
}

// --- Дайджесты ---

export type DigestStatus = 'building' | 'ready' | 'sent' | 'partially_sent' | 'failed' | 'empty';
export type DeliveryStatus = 'pending' | 'sent' | 'failed';

export interface Digest {
  id: number;
  period_start: string;
  period_end: string;
  papers_count: number;
  status: DigestStatus;
  error: string | null;
  has_pdf: boolean;
  filename: string | null;
  created_at: string;
  updated_at: string;
}

export interface DigestPaper {
  position: number;
  paper_id: number;
  arxiv_id: string;
  title: string;
  abs_url: string;
  topic_id: number | null;
  topic_name: string | null;
}

export interface Delivery {
  id: number;
  email: string;
  status: DeliveryStatus;
  attempts: number;
  error: string | null;
  sent_at: string | null;
  updated_at: string;
}

export interface DigestDetail extends Digest {
  papers: DigestPaper[];
  deliveries: Delivery[];
}

// --- Дашборд ---

export interface LlmHealth {
  ok: boolean;
  base_url: string;
  model: string;
  latency_ms: number | null;
  model_available: boolean | null;
  models: string[];
  error: string | null;
}

export interface SmtpStatus {
  ok: boolean;
  is_configured: boolean;
  password_set: boolean;
  host: string;
  error: string | null;
}

export interface Dashboard {
  counters: {
    papers_collected: number;
    papers_summarized: number;
    digests_sent: number;
    runs_success: number;
    runs_failed: number;
  };
  llm: LlmHealth;
  smtp: SmtpStatus;
  pipeline_running: boolean;
  upcoming_schedules: Schedule[];
  recent_runs: Run[];
  timezone: string;
}
