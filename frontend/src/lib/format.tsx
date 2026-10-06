import { useQuery } from '@tanstack/react-query';
import { createContext, useContext, useMemo, type ReactNode } from 'react';

import { getSettings } from '../api/endpoints';
import type { PublicSettings } from '../api/types';

const BROWSER_TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;

interface Formatters {
  /** Таймзона сервиса (TZ из .env): в ней показываются все даты. */
  timezone: string;
  settings: PublicSettings | undefined;
  dateTime: (value: string | null | undefined) => string;
  date: (value: string | null | undefined) => string;
  time: (value: string | null | undefined) => string;
}

function makeFormatters(timezone: string, settings: PublicSettings | undefined): Formatters {
  const build = (options: Intl.DateTimeFormatOptions) => {
    const formatter = new Intl.DateTimeFormat('ru-RU', { timeZone: timezone, ...options });
    return (value: string | null | undefined) => {
      if (!value) {
        return '—';
      }
      const parsed = new Date(value);
      return Number.isNaN(parsed.getTime()) ? '—' : formatter.format(parsed);
    };
  };
  return {
    timezone,
    settings,
    dateTime: build({
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }),
    date: build({ day: '2-digit', month: '2-digit', year: 'numeric' }),
    time: build({ hour: '2-digit', minute: '2-digit', second: '2-digit' }),
  };
}

const FormatContext = createContext<Formatters>(makeFormatters(BROWSER_TIMEZONE, undefined));

export function FormatProvider({ children }: { children: ReactNode }) {
  const { data } = useQuery({ queryKey: ['settings'], queryFn: getSettings, staleTime: Infinity });
  const value = useMemo(() => makeFormatters(data?.timezone ?? BROWSER_TIMEZONE, data), [data]);
  return <FormatContext.Provider value={value}>{children}</FormatContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useFormat(): Formatters {
  return useContext(FormatContext);
}
