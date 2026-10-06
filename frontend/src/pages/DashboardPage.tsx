import {
  Anchor,
  Button,
  Card,
  Group,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
} from '@mantine/core';
import { IconPlayerPlay } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';

import { createRun, getDashboard } from '../api/endpoints';
import { HealthBadge, RunStatusBadge } from '../components/Badges';
import { PageHeader, QueryState } from '../components/common';
import { formatDuration } from '../lib/dates';
import { useFormat } from '../lib/format';
import { RUN_TRIGGER_LABELS, TASK_TYPE_LABELS } from '../lib/labels';
import { notifySuccess } from '../lib/queryClient';

function StatCard({ label, value, hint }: { label: string; value: number; hint?: string }) {
  return (
    <Card withBorder padding="md">
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <Text fz={32} fw={700} lh={1.2}>
        {value}
      </Text>
      {hint ? (
        <Text size="xs" c="dimmed">
          {hint}
        </Text>
      ) : null}
    </Card>
  );
}

export function DashboardPage() {
  const fmt = useFormat();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ['dashboard'],
    queryFn: getDashboard,
    refetchInterval: 15_000,
  });

  const launch = useMutation({
    mutationFn: () => createRun({ task_type: 'full_pipeline' }),
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] });
      if (run.status === 'skipped') {
        notifySuccess(run.error ?? 'Пайплайн уже выполняется', 'Запуск пропущен');
      } else {
        notifySuccess(`Пайплайн поставлен в очередь, запуск №${run.id}`);
      }
      navigate(`/runs/${run.id}`);
    },
  });

  return (
    <>
      <PageHeader title="Дашборд">
        <Button
          leftSection={<IconPlayerPlay size={16} />}
          loading={launch.isPending}
          disabled={data?.pipeline_running}
          onClick={() => launch.mutate()}
        >
          {data?.pipeline_running ? 'Пайплайн выполняется' : 'Запустить пайплайн сейчас'}
        </Button>
      </PageHeader>

      <QueryState isLoading={isLoading} error={error}>
        {data ? (
          <Stack gap="lg">
            <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }}>
              <StatCard
                label="Статей за 7 дней"
                value={data.counters.papers_collected}
                hint={`с резюме: ${data.counters.papers_summarized}`}
              />
              <StatCard
                label="Дайджестов отправлено"
                value={data.counters.digests_sent}
                hint="за 7 дней"
              />
              <StatCard
                label="Успешных запусков"
                value={data.counters.runs_success}
                hint="за 7 дней"
              />
              <StatCard
                label="Запусков с ошибкой"
                value={data.counters.runs_failed}
                hint="за 7 дней"
              />
            </SimpleGrid>

            <SimpleGrid cols={{ base: 1, md: 2 }}>
              <Card withBorder padding="md">
                <Group justify="space-between" mb="xs">
                  <Title order={5}>Локальная LLM</Title>
                  <HealthBadge ok={data.llm.ok} okLabel="Доступна" failLabel="Недоступна" />
                </Group>
                <Text size="sm">Модель: {data.llm.model}</Text>
                <Text size="sm" c="dimmed">
                  {data.llm.base_url}
                </Text>
                {data.llm.ok && data.llm.latency_ms !== null ? (
                  <Text size="sm" c="dimmed">
                    Ответ за {data.llm.latency_ms} мс
                  </Text>
                ) : null}
                {data.llm.error ? (
                  <Text size="sm" c="red" mt={4}>
                    {data.llm.error}
                  </Text>
                ) : null}
              </Card>
              <Card withBorder padding="md">
                <Group justify="space-between" mb="xs">
                  <Title order={5}>SMTP</Title>
                  <HealthBadge ok={data.smtp.ok} okLabel="Настроен" failLabel="Не настроен" />
                </Group>
                <Text size="sm">Сервер: {data.smtp.host || '—'}</Text>
                <Text size="sm" c="dimmed">
                  Пароль в .env: {data.smtp.password_set ? 'задан' : 'не задан'}
                </Text>
                {data.smtp.error ? (
                  <Text size="sm" c="red" mt={4}>
                    {data.smtp.error}.{' '}
                    <Anchor component={Link} to="/smtp" size="sm">
                      Открыть настройки
                    </Anchor>
                  </Text>
                ) : null}
              </Card>
            </SimpleGrid>

            <Card withBorder padding="md">
              <Title order={5} mb="sm">
                Ближайшие запуски по расписанию
              </Title>
              {data.upcoming_schedules.length === 0 ? (
                <Text size="sm" c="dimmed">
                  Активных расписаний нет.{' '}
                  <Anchor component={Link} to="/schedules" size="sm">
                    Создать расписание
                  </Anchor>
                </Text>
              ) : (
                <Table>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Расписание</Table.Th>
                      <Table.Th>Задача</Table.Th>
                      <Table.Th>Cron</Table.Th>
                      <Table.Th>Следующий запуск</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {data.upcoming_schedules.map((schedule) => (
                      <Table.Tr key={schedule.id}>
                        <Table.Td>{schedule.name}</Table.Td>
                        <Table.Td>{TASK_TYPE_LABELS[schedule.task_type]}</Table.Td>
                        <Table.Td>
                          <Tooltip label={`Таймзона: ${schedule.timezone}`}>
                            <Text size="sm" ff="monospace">
                              {schedule.cron_expression}
                            </Text>
                          </Tooltip>
                        </Table.Td>
                        <Table.Td>{fmt.dateTime(schedule.next_fire_at)}</Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              )}
            </Card>

            <Card withBorder padding="md">
              <Group justify="space-between" mb="sm">
                <Title order={5}>Последние запуски</Title>
                <Anchor component={Link} to="/runs" size="sm">
                  Все запуски
                </Anchor>
              </Group>
              {data.recent_runs.length === 0 ? (
                <Text size="sm" c="dimmed">
                  Запусков ещё не было
                </Text>
              ) : (
                <Table highlightOnHover>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>№</Table.Th>
                      <Table.Th>Задача</Table.Th>
                      <Table.Th>Запущен</Table.Th>
                      <Table.Th>Статус</Table.Th>
                      <Table.Th>Создан</Table.Th>
                      <Table.Th>Длительность</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {data.recent_runs.map((run) => (
                      <Table.Tr key={run.id}>
                        <Table.Td>
                          <Anchor component={Link} to={`/runs/${run.id}`}>
                            {run.id}
                          </Anchor>
                        </Table.Td>
                        <Table.Td>{TASK_TYPE_LABELS[run.task_type]}</Table.Td>
                        <Table.Td>{RUN_TRIGGER_LABELS[run.trigger]}</Table.Td>
                        <Table.Td>
                          <RunStatusBadge status={run.status} />
                        </Table.Td>
                        <Table.Td>{fmt.dateTime(run.created_at)}</Table.Td>
                        <Table.Td>{formatDuration(run.duration_ms)}</Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              )}
            </Card>
          </Stack>
        ) : null}
      </QueryState>
    </>
  );
}
