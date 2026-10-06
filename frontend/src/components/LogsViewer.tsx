import {
  Badge,
  Box,
  Card,
  Group,
  Loader,
  ScrollArea,
  Select,
  Switch,
  Text,
  Title,
} from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import { getRunLogs } from '../api/endpoints';
import type { LogEntry, LogLevel } from '../api/types';
import { useFormat } from '../lib/format';
import { LOG_LEVEL_COLORS, LOG_LEVEL_OPTIONS } from '../lib/labels';

const LOG_LIMIT = 2000;
const REFRESH_MS = 2000;

function formatExtra(extra: LogEntry['extra']): string {
  const entries = Object.entries(extra).filter(([key]) => key !== 'exception');
  return entries
    .map(([key, value]) => `${key}=${typeof value === 'string' ? value : JSON.stringify(value)}`)
    .join('  ');
}

interface Props {
  runId: number;
  /** Пока запуск активен, логи автоматически обновляются. */
  live: boolean;
}

export function LogsViewer({ runId, live }: Props) {
  const fmt = useFormat();
  const [level, setLevel] = useState<LogLevel>('info');
  const [follow, setFollow] = useState(true);
  const viewport = useRef<HTMLDivElement>(null);

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ['run-logs', runId, level],
    queryFn: () => getRunLogs(runId, { level, limit: LOG_LIMIT }),
    refetchInterval: live ? REFRESH_MS : false,
  });

  // запуск только что завершился: дочитываем записи, появившиеся после последнего опроса
  const wasLive = useRef(live);
  useEffect(() => {
    if (wasLive.current && !live) {
      void refetch();
    }
    wasLive.current = live;
  }, [live, refetch]);

  // при появлении новых записей прокручиваем к последней, если включено «следить»
  const lastId = data?.last_id;
  useEffect(() => {
    if (follow && viewport.current) {
      viewport.current.scrollTo({ top: viewport.current.scrollHeight });
    }
  }, [lastId, follow]);

  return (
    <Card withBorder padding="md">
      <Group justify="space-between" mb="sm" wrap="wrap">
        <Group gap="sm">
          <Title order={5}>Логи</Title>
          {live ? (
            <Badge
              color="blue"
              variant="light"
              leftSection={isFetching ? <Loader size={10} /> : null}
            >
              автообновление
            </Badge>
          ) : null}
          <Text size="sm" c="dimmed">
            {data ? `записей: ${data.total}` : ''}
            {data && data.total > LOG_LIMIT ? ` (показаны первые ${LOG_LIMIT})` : ''}
          </Text>
        </Group>
        <Group gap="md">
          <Switch
            label="Следить за новыми"
            checked={follow}
            onChange={(event) => setFollow(event.currentTarget.checked)}
          />
          <Select
            data={LOG_LEVEL_OPTIONS}
            value={level}
            onChange={(value) => value && setLevel(value as LogLevel)}
            allowDeselect={false}
            w={190}
            aria-label="Уровень логов"
          />
        </Group>
      </Group>

      <ScrollArea
        h={460}
        viewportRef={viewport}
        type="auto"
        bg="gray.0"
        style={{ borderRadius: 8 }}
      >
        <Box p="xs" ff="monospace" fz={12.5} lh={1.5}>
          {isLoading ? <Loader size="sm" /> : null}
          {data && data.items.length === 0 ? (
            <Text c="dimmed" size="sm" ff="inherit">
              Записей нет
            </Text>
          ) : null}
          {data?.items.map((entry) => (
            <Box key={entry.id} mb={2} style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              <Text span c="dimmed" inherit>
                {fmt.time(entry.ts)}
              </Text>{' '}
              <Text span c={LOG_LEVEL_COLORS[entry.level]} fw={700} inherit>
                {entry.level.toUpperCase().padEnd(7)}
              </Text>{' '}
              <Text span fw={600} inherit>
                {entry.message}
              </Text>{' '}
              <Text span c="gray.7" inherit>
                {formatExtra(entry.extra)}
              </Text>
              {typeof entry.extra.exception === 'string' ? (
                <Box c="red.8" pl="md">
                  {entry.extra.exception}
                </Box>
              ) : null}
            </Box>
          ))}
        </Box>
      </ScrollArea>
    </Card>
  );
}
