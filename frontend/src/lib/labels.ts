import type {
  DeliveryStatus,
  DigestContent,
  DigestStatus,
  LogLevel,
  PaperStatus,
  Relevance,
  RunStatus,
  RunTrigger,
  SmtpSecurity,
  TaskType,
} from '../api/types';

interface StatusView {
  label: string;
  color: string;
}

export const TASK_TYPE_LABELS: Record<TaskType, string> = {
  full_pipeline: 'Полный пайплайн',
  collect: 'Сбор статей',
  summarize: 'Суммаризация',
  build_digest: 'Сборка дайджеста',
  send_digest: 'Рассылка',
};

export const TASK_TYPE_OPTIONS = (Object.keys(TASK_TYPE_LABELS) as TaskType[]).map((value) => ({
  value,
  label: TASK_TYPE_LABELS[value],
}));

export const RUN_STATUS: Record<RunStatus, StatusView> = {
  queued: { label: 'В очереди', color: 'gray' },
  running: { label: 'Выполняется', color: 'blue' },
  success: { label: 'Успешно', color: 'green' },
  failed: { label: 'Ошибка', color: 'red' },
  skipped: { label: 'Пропущен', color: 'yellow' },
  cancelled: { label: 'Отменён', color: 'orange' },
};

export const RUN_TRIGGER_LABELS: Record<RunTrigger, string> = {
  schedule: 'По расписанию',
  manual: 'Вручную',
};

export const PAPER_STATUS: Record<PaperStatus, StatusView> = {
  new: { label: 'Новая', color: 'gray' },
  downloaded: { label: 'PDF скачан', color: 'cyan' },
  text_extracted: { label: 'Текст извлечён', color: 'blue' },
  summarized: { label: 'Есть резюме', color: 'green' },
  failed: { label: 'Ошибка', color: 'red' },
  irrelevant: { label: 'Нерелевантна', color: 'orange' },
};

export const RELEVANCE: Record<Relevance, StatusView> = {
  relevant: { label: 'По теме', color: 'green' },
  partial: { label: 'Частично по теме', color: 'yellow' },
  irrelevant: { label: 'Не по теме', color: 'orange' },
};

export const DIGEST_STATUS: Record<DigestStatus, StatusView> = {
  building: { label: 'Собирается', color: 'blue' },
  ready: { label: 'Готов', color: 'cyan' },
  sent: { label: 'Отправлен', color: 'green' },
  partially_sent: { label: 'Отправлен частично', color: 'yellow' },
  failed: { label: 'Ошибка', color: 'red' },
  empty: { label: 'Пустой', color: 'gray' },
};

export const DIGEST_CONTENT_LABELS: Record<DigestContent, string> = {
  abstract: 'Только аннотация',
  full: 'Аннотация и разделы резюме',
  summary: 'Только разделы резюме',
};

export const DIGEST_CONTENT_OPTIONS = (Object.keys(DIGEST_CONTENT_LABELS) as DigestContent[]).map(
  (value) => ({ value, label: DIGEST_CONTENT_LABELS[value] }),
);

export const DELIVERY_STATUS: Record<DeliveryStatus, StatusView> = {
  pending: { label: 'Ожидает', color: 'gray' },
  sent: { label: 'Доставлено', color: 'green' },
  failed: { label: 'Ошибка', color: 'red' },
};

export const SMTP_SECURITY_OPTIONS: { value: SmtpSecurity; label: string }[] = [
  { value: 'none', label: 'Без шифрования' },
  { value: 'starttls', label: 'STARTTLS' },
  { value: 'ssl', label: 'SSL / TLS' },
];

export const LOG_LEVEL_COLORS: Record<LogLevel, string> = {
  debug: 'gray',
  info: 'blue',
  warning: 'yellow',
  error: 'red',
  critical: 'red',
};

export const LOG_LEVEL_OPTIONS: { value: LogLevel; label: string }[] = [
  { value: 'info', label: 'INFO и выше' },
  { value: 'warning', label: 'WARNING и выше' },
  { value: 'error', label: 'Только ошибки' },
];

/** Популярные категории arXiv для подсказок в форме темы; можно ввести и любую другую. */
export const POPULAR_ARXIV_CATEGORIES = [
  'cs.AI',
  'cs.CL',
  'cs.CV',
  'cs.LG',
  'cs.IR',
  'cs.NE',
  'cs.RO',
  'cs.SE',
  'cs.CR',
  'cs.DC',
  'cs.HC',
  'cs.MA',
  'stat.ML',
  'eess.AS',
  'eess.IV',
  'math.OC',
  'math.ST',
  'physics.comp-ph',
  'q-bio.NC',
  'q-fin.ST',
];

const STAT_LABELS: Record<string, string> = {
  papers_found: 'Найдено статей',
  papers_new: 'Новых',
  duplicates: 'Дублей',
  download_errors: 'Ошибок скачивания',
  text_extracted: 'Текст извлечён',
  topics_processed: 'Тем обработано',
  papers_total: 'Статей к обработке',
  summarized: 'Суммаризировано',
  failed: 'С ошибкой',
  irrelevant: 'Нерелевантных',
  relevance_checks: 'Проверок релевантности',
  llm_calls: 'Вызовов LLM',
  chunks: 'Чанков',
  prompt_tokens: 'Токенов запроса',
  completion_tokens: 'Токенов ответа',
  llm_duration_ms: 'Время LLM, мс',
  digest_id: 'Дайджест №',
  papers_count: 'Статей в дайджесте',
  topics: 'Тем',
  pdf_size_bytes: 'Размер PDF, байт',
  build_duration_ms: 'Время сборки, мс',
  recipients_total: 'Получателей',
  sent: 'Отправлено',
  skipped_already_sent: 'Уже получили ранее',
  digest_status: 'Статус дайджеста',
  status: 'Статус',
  content: 'Состав карточек',
  digest_content: 'Состав карточек',
  fill_abstracts: 'Только дописать аннотации',
  retry_failed: 'Повторить упавшие',
  period_start: 'Начало периода',
  period_end: 'Конец периода',
  pdf_path: 'Файл',
  found: 'Найдено',
  new: 'Новых',
};

export function statLabel(key: string): string {
  return STAT_LABELS[key] ?? key;
}
