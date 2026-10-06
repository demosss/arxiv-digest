export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) {
    return '—';
  }
  if (ms < 1000) {
    return `${ms} мс`;
  }
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) {
    return `${seconds} с`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes} мин ${seconds % 60} с`;
  }
  return `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
}

/** Период дайджеста: целые сутки UTC показываются одной датой, как в самом PDF. */
export function formatPeriod(start: string, end: string): string {
  const formatter = new Intl.DateTimeFormat('ru-RU', {
    timeZone: 'UTC',
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
  });
  const first = formatter.format(new Date(start));
  const last = formatter.format(new Date(end));
  return first === last ? first : `${first} — ${last}`;
}

/** Границы суток UTC для выбранной в календаре даты (берутся её год, месяц и день). */
export function utcDayBounds(from: Date, to: Date): { start: string; end: string } {
  const start = new Date(Date.UTC(from.getFullYear(), from.getMonth(), from.getDate(), 0, 0, 0));
  const end = new Date(Date.UTC(to.getFullYear(), to.getMonth(), to.getDate(), 23, 59, 59));
  return { start: start.toISOString(), end: end.toISOString() };
}
