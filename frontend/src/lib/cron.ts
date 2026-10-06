// «Простой режим» расписания: время + дни недели ↔ cron-выражение из пяти полей.

export const WEEKDAYS = [
  { value: '1', label: 'Пн' },
  { value: '2', label: 'Вт' },
  { value: '3', label: 'Ср' },
  { value: '4', label: 'Чт' },
  { value: '5', label: 'Пт' },
  { value: '6', label: 'Сб' },
  { value: '0', label: 'Вс' },
];

export const ALL_WEEKDAYS = WEEKDAYS.map((day) => day.value);

export interface SimpleSchedule {
  /** Время в формате HH:MM */
  time: string;
  /** Дни недели в нотации cron: 0 — воскресенье … 6 — суббота */
  days: string[];
}

const SIMPLE_CRON_RE = /^(\d{1,2}) (\d{1,2}) \* \* (\*|[0-6](?:,[0-6])*)$/;

function pad(value: number): string {
  return value.toString().padStart(2, '0');
}

/** Собирает cron из времени и дней недели. Без выбранных дней возвращает null. */
export function buildCron({ time, days }: SimpleSchedule): string | null {
  const match = /^(\d{1,2}):(\d{2})$/.exec(time);
  if (!match || days.length === 0) {
    return null;
  }
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (hour > 23 || minute > 59) {
    return null;
  }
  const sorted = [...new Set(days)].sort((a, b) => Number(a) - Number(b));
  const dayField = sorted.length === ALL_WEEKDAYS.length ? '*' : sorted.join(',');
  return `${minute} ${hour} * * ${dayField}`;
}

/** Разбирает cron, если он укладывается в простой режим; иначе null (нужен продвинутый режим). */
export function parseSimpleCron(expression: string): SimpleSchedule | null {
  const match = SIMPLE_CRON_RE.exec(expression.trim().replace(/\s+/g, ' '));
  if (!match) {
    return null;
  }
  const minute = Number(match[1]);
  const hour = Number(match[2]);
  if (hour > 23 || minute > 59) {
    return null;
  }
  const dayField = match[3] ?? '*';
  return {
    time: `${pad(hour)}:${pad(minute)}`,
    days: dayField === '*' ? [...ALL_WEEKDAYS] : dayField.split(','),
  };
}
