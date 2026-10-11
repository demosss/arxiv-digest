import {
  Alert,
  Anchor,
  Button,
  Card,
  Code,
  Group,
  SimpleGrid,
  Spoiler,
  Stack,
  Table,
  Text,
  Title,
} from '@mantine/core';
import { modals } from '@mantine/modals';
import { IconArrowLeft, IconPlayerStop } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';

import { cancelRun, getRun } from '../api/endpoints';
import type { DigestContent, Run } from '../api/types';
import { RunStatusBadge } from '../components/Badges';
import { Field, PageHeader, QueryState } from '../components/common';
import { LogsViewer } from '../components/LogsViewer';
import { formatDuration } from '../lib/dates';
import { useFormat } from '../lib/format';
import {
  DIGEST_CONTENT_LABELS,
  RUN_TRIGGER_LABELS,
  TASK_TYPE_LABELS,
  statLabel,
} from '../lib/labels';
import { notifySuccess } from '../lib/queryClient';

const PARAM_ORDER = ['period_start', 'period_end'];

function isActive(run: Pick<Run, 'status'>): boolean {
  return run.status === 'queued' || run.status === 'running';
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Статистика запуска: простые значения — таблицей, вложенные (по темам, по шагам) — отдельно. */
function StatsView({ stats }: { stats: Record<string, unknown> }) {
  const entries = Object.entries(stats);
  if (entries.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        Статистики пока нет
      </Text>
    );
  }
  const scalars = entries.filter(([, value]) => !isPlainObject(value));
  const nested = entries.filter((entry): entry is [string, Record<string, unknown>] =>
    isPlainObject(entry[1]),
  );
  return (
    <Stack gap="sm">
      {scalars.length > 0 ? (
        <SimpleGrid cols={{ base: 2, sm: 3, md: 4 }} spacing="sm">
          {scalars.map(([key, value]) => (
            <Field key={key} label={statLabel(key)}>
              <Text size="sm" fw={600} style={{ wordBreak: 'break-word' }}>
                {String(value)}
              </Text>
            </Field>
          ))}
        </SimpleGrid>
      ) : null}
      {nested.map(([key, value]) => (
        <Card key={key} withBorder padding="sm" bg="gray.0">
          <Text size="sm" fw={600} mb={6}>
            {TASK_TYPE_LABELS[key as keyof typeof TASK_TYPE_LABELS] ?? statLabel(key)}
          </Text>
          <StatsView stats={value} />
        </Card>
      ))}
    </Stack>
  );
}

export function RunDetailPage() {
  const fmt = useFormat();
  const queryClient = useQueryClient();
  const runId = Number(useParams().runId);

  const {
    data: run,
    isLoading,
    error,
  } = useQuery({
    queryKey: ['run', runId],
    queryFn: () => getRun(runId),
    enabled: Number.isInteger(runId),
    refetchInterval: (query) => (query.state.data && isActive(query.state.data) ? 2000 : false),
  });

  const cancel = useMutation({
    mutationFn: () => cancelRun(runId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['run', runId] });
      void queryClient.invalidateQueries({ queryKey: ['runs'] });
      notifySuccess('Запуск отменён');
    },
  });

  const confirmCancel = () =>
    modals.openConfirmModal({
      title: 'Отменить запуск?',
      centered: true,
      children: (
        <Text size="sm">Выполняющаяся задача будет остановлена, оставшиеся шаги — пропущены.</Text>
      ),
      labels: { confirm: 'Отменить запуск', cancel: 'Не отменять' },
      confirmProps: { color: 'red' },
      onConfirm: () => cancel.mutate(),
    });

  // период показываем в естественном порядке: начало, затем конец
  const params = run
    ? Object.entries(run.params).sort(([a], [b]) => PARAM_ORDER.indexOf(a) - PARAM_ORDER.indexOf(b))
    : [];

  return (
    <>
      <PageHeader title={`Запуск №${Number.isInteger(runId) ? runId : '?'}`}>
        {run && isActive(run) ? (
          <Button
            color="red"
            variant="light"
            leftSection={<IconPlayerStop size={16} />}
            loading={cancel.isPending}
            onClick={confirmCancel}
          >
            Отменить
          </Button>
        ) : null}
        <Button
          component={Link}
          to={run?.parent_run_id ? `/runs/${run.parent_run_id}` : '/runs'}
          variant="default"
          leftSection={<IconArrowLeft size={16} />}
        >
          {run?.parent_run_id ? `К пайплайну №${run.parent_run_id}` : 'К списку'}
        </Button>
      </PageHeader>

      <QueryState isLoading={isLoading} error={error}>
        {run ? (
          <Stack gap="lg">
            <Card withBorder padding="md">
              <SimpleGrid cols={{ base: 2, sm: 3, md: 6 }} spacing="md">
                <Field label="Задача">
                  <Text fw={600}>{TASK_TYPE_LABELS[run.task_type]}</Text>
                </Field>
                <Field label="Статус">
                  <RunStatusBadge status={run.status} />
                </Field>
                <Field label="Запущен">
                  <Text size="sm">
                    {RUN_TRIGGER_LABELS[run.trigger]}
                    {run.schedule_id ? ` (расписание №${run.schedule_id})` : ''}
                  </Text>
                </Field>
                <Field label="Создан">
                  <Text size="sm">{fmt.dateTime(run.created_at)}</Text>
                </Field>
                <Field label="Начат / завершён">
                  <Text size="sm">
                    {fmt.time(run.started_at)} / {fmt.time(run.finished_at)}
                  </Text>
                </Field>
                <Field label="Длительность">
                  <Text size="sm">{formatDuration(run.duration_ms)}</Text>
                </Field>
              </SimpleGrid>
              {params.length > 0 ? (
                <Group gap="lg" mt="md">
                  {params.map(([key, value]) => (
                    <Text key={key} size="sm" c="dimmed">
                      {statLabel(key)}:{' '}
                      <Text span c="dark" inherit>
                        {key.startsWith('period_') && typeof value === 'string'
                          ? fmt.dateTime(value)
                          : key === 'digest_content' && typeof value === 'string'
                            ? (DIGEST_CONTENT_LABELS[value as DigestContent] ?? value)
                            : JSON.stringify(value)}
                      </Text>
                    </Text>
                  ))}
                </Group>
              ) : null}
            </Card>

            {run.error ? (
              <Alert
                color={run.status === 'failed' ? 'red' : 'yellow'}
                title={run.status === 'failed' ? 'Ошибка' : 'Причина'}
              >
                <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                  {run.error}
                </Text>
                {run.traceback ? (
                  <Spoiler
                    maxHeight={0}
                    showLabel="Показать traceback"
                    hideLabel="Скрыть traceback"
                    mt="sm"
                  >
                    <Code block fz={12}>
                      {run.traceback}
                    </Code>
                  </Spoiler>
                ) : null}
              </Alert>
            ) : null}

            {run.children.length > 0 ? (
              <Card withBorder padding="md">
                <Title order={5} mb="sm">
                  Шаги пайплайна
                </Title>
                <Table verticalSpacing="sm">
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>№</Table.Th>
                      <Table.Th>Шаг</Table.Th>
                      <Table.Th>Статус</Table.Th>
                      <Table.Th>Начат</Table.Th>
                      <Table.Th>Длительность</Table.Th>
                      <Table.Th>Комментарий</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {run.children.map((step) => (
                      <Table.Tr key={step.id}>
                        <Table.Td>
                          <Anchor component={Link} to={`/runs/${step.id}`}>
                            {step.id}
                          </Anchor>
                        </Table.Td>
                        <Table.Td>{TASK_TYPE_LABELS[step.task_type]}</Table.Td>
                        <Table.Td>
                          <RunStatusBadge status={step.status} />
                        </Table.Td>
                        <Table.Td>{fmt.time(step.started_at)}</Table.Td>
                        <Table.Td>{formatDuration(step.duration_ms)}</Table.Td>
                        <Table.Td maw={420}>
                          <Text
                            size="sm"
                            c={step.status === 'failed' ? 'red' : 'dimmed'}
                            lineClamp={2}
                          >
                            {step.error ?? ''}
                          </Text>
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </Card>
            ) : null}

            <Card withBorder padding="md">
              <Title order={5} mb="sm">
                Статистика
              </Title>
              <StatsView stats={run.stats} />
            </Card>

            <LogsViewer runId={run.id} live={isActive(run)} />
          </Stack>
        ) : null}
      </QueryState>
    </>
  );
}
