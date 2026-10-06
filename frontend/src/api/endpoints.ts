import { api } from './client';
import type {
  CronValidation,
  Dashboard,
  DeletedCount,
  Digest,
  DigestDetail,
  DigestStatus,
  LlmHealth,
  LogLevel,
  LogPage,
  Page,
  Paper,
  PaperDetail,
  PaperStatus,
  PublicSettings,
  Recipient,
  RecipientInput,
  Run,
  RunCreate,
  RunDetail,
  RunStatus,
  RunTrigger,
  Schedule,
  ScheduleInput,
  SmtpSettings,
  SmtpSettingsInput,
  TaskType,
  TokenResponse,
  Topic,
  TopicInput,
  User,
} from './types';

export interface PageParams {
  limit: number;
  offset: number;
}

/** Убирает пустые значения, чтобы они не уходили в query string. */
function clean<T extends object>(params: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(params).filter(
      ([, value]) => value !== undefined && value !== null && value !== '',
    ),
  ) as Partial<T>;
}

// --- Авторизация и настройки ---

export const login = (username: string, password: string) =>
  api.post<TokenResponse>('/auth/login', { username, password }).then((r) => r.data);

export const getMe = () => api.get<User>('/auth/me').then((r) => r.data);

export const changePassword = (current_password: string, new_password: string) =>
  api.post<void>('/auth/change-password', { current_password, new_password }).then(() => undefined);

export const getSettings = () => api.get<PublicSettings>('/settings').then((r) => r.data);

export const getDashboard = () => api.get<Dashboard>('/dashboard').then((r) => r.data);

export const getLlmHealth = () => api.get<LlmHealth>('/llm/health').then((r) => r.data);

// --- Темы ---

export const listTopics = (params: PageParams & { is_active?: boolean; q?: string }) =>
  api.get<Page<Topic>>('/topics', { params: clean(params) }).then((r) => r.data);

export const createTopic = (data: TopicInput) =>
  api.post<Topic>('/topics', data).then((r) => r.data);

export const updateTopic = (id: number, data: Partial<TopicInput>) =>
  api.patch<Topic>(`/topics/${id}`, data).then((r) => r.data);

export const deleteTopic = (id: number) => api.delete<void>(`/topics/${id}`).then(() => undefined);

// --- Получатели ---

export const listRecipients = (params: PageParams & { is_active?: boolean; q?: string }) =>
  api.get<Page<Recipient>>('/recipients', { params: clean(params) }).then((r) => r.data);

export const createRecipient = (data: RecipientInput) =>
  api.post<Recipient>('/recipients', data).then((r) => r.data);

export const updateRecipient = (id: number, data: Partial<RecipientInput>) =>
  api.patch<Recipient>(`/recipients/${id}`, data).then((r) => r.data);

export const deleteRecipient = (id: number) =>
  api.delete<void>(`/recipients/${id}`).then(() => undefined);

// --- Расписания ---

export const listSchedules = (params: PageParams) =>
  api.get<Page<Schedule>>('/schedules', { params }).then((r) => r.data);

export const createSchedule = (data: ScheduleInput) =>
  api.post<Schedule>('/schedules', data).then((r) => r.data);

export const updateSchedule = (id: number, data: Partial<ScheduleInput>) =>
  api.patch<Schedule>(`/schedules/${id}`, data).then((r) => r.data);

export const deleteSchedule = (id: number) =>
  api.delete<void>(`/schedules/${id}`).then(() => undefined);

export const validateCron = (cron_expression: string, timezone: string) =>
  api
    .post<CronValidation>('/schedules/validate-cron', { cron_expression, timezone })
    .then((r) => r.data);

// --- SMTP ---

export const getSmtp = () => api.get<SmtpSettings>('/smtp').then((r) => r.data);

export const updateSmtp = (data: SmtpSettingsInput) =>
  api.put<SmtpSettings>('/smtp', data).then((r) => r.data);

export const sendSmtpTest = (to: string) =>
  api.post<{ ok: boolean; to: string }>('/smtp/test', { to }).then((r) => r.data);

// --- Запуски и логи ---

export interface RunFilters {
  task_type?: TaskType;
  status?: RunStatus;
  trigger?: RunTrigger;
  date_from?: string;
  date_to?: string;
}

export const listRuns = (params: PageParams & RunFilters) =>
  api.get<Page<Run>>('/runs', { params: clean(params) }).then((r) => r.data);

export const getRun = (id: number) => api.get<RunDetail>(`/runs/${id}`).then((r) => r.data);

export const createRun = (data: RunCreate) =>
  api.post<RunDetail>('/runs', data).then((r) => r.data);

export const cancelRun = (id: number) =>
  api
    .post<{ run: RunDetail; cancelled_run_ids: number[] }>(`/runs/${id}/cancel`)
    .then((r) => r.data);

export const deleteRun = (id: number) => api.delete<void>(`/runs/${id}`).then(() => undefined);

/** Удаляет завершённые запуски под фильтрами; активные остаются. */
export const clearRuns = (filters: RunFilters) =>
  api.delete<DeletedCount>('/runs', { params: clean(filters) }).then((r) => r.data);

export const getRunLogs = (id: number, params: { level?: LogLevel; limit?: number }) =>
  api.get<LogPage>(`/runs/${id}/logs`, { params: clean(params) }).then((r) => r.data);

// --- Статьи ---

export interface PaperFilters {
  topic_id?: number;
  status?: PaperStatus;
  date_from?: string;
  date_to?: string;
  q?: string;
}

export const listPapers = (params: PageParams & PaperFilters) =>
  api.get<Page<Paper>>('/papers', { params: clean(params) }).then((r) => r.data);

export const getPaper = (id: number) => api.get<PaperDetail>(`/papers/${id}`).then((r) => r.data);

export const resummarizePaper = (id: number) =>
  api.post<RunDetail>(`/papers/${id}/resummarize`).then((r) => r.data);

export const deletePaper = (id: number) => api.delete<void>(`/papers/${id}`).then(() => undefined);

export const clearPapers = (filters: PaperFilters) =>
  api.delete<DeletedCount>('/papers', { params: clean(filters) }).then((r) => r.data);

// --- Дайджесты ---

export const listDigests = (params: PageParams & { status?: DigestStatus }) =>
  api.get<Page<Digest>>('/digests', { params: clean(params) }).then((r) => r.data);

export const getDigest = (id: number) =>
  api.get<DigestDetail>(`/digests/${id}`).then((r) => r.data);

export const resendDigest = (id: number, force: boolean) =>
  api.post<RunDetail>(`/digests/${id}/resend`, { force }).then((r) => r.data);

export const deleteDigest = (id: number) =>
  api.delete<void>(`/digests/${id}`).then(() => undefined);

export const clearDigests = (filters: { status?: DigestStatus }) =>
  api.delete<DeletedCount>('/digests', { params: clean(filters) }).then((r) => r.data);

/** PDF скачивается через axios (нужен заголовок Authorization) и отдаётся браузеру как файл. */
export async function downloadDigestPdf(digest: Digest): Promise<void> {
  const response = await api.get<Blob>(`/digests/${digest.id}/pdf`, { responseType: 'blob' });
  const url = URL.createObjectURL(response.data);
  const link = document.createElement('a');
  link.href = url;
  link.download = digest.filename ?? `digest_${digest.id}.pdf`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
