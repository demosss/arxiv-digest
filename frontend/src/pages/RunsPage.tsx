import { ActionIcon, Anchor, Button, Group, Select, Table, Text } from '@mantine/core';
import { DatePickerInput, type DatesRangeValue } from '@mantine/dates';
import { IconPlayerPlay, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';

import { clearRuns, deleteRun, listRuns, type RunFilters } from '../api/endpoints';
import type { Run, RunStatus, RunTrigger, TaskType } from '../api/types';
import { RunStatusBadge } from '../components/Badges';
import {
  PageHeader,
  QueryState,
  TablePagination,
  confirmDelete,
  usePagination,
} from '../components/common';
import { LaunchRunModal } from '../components/LaunchRunModal';
import { formatDuration } from '../lib/dates';
import { useFormat } from '../lib/format';
import { RUN_STATUS, RUN_TRIGGER_LABELS, TASK_TYPE_LABELS, TASK_TYPE_OPTIONS } from '../lib/labels';
import { notifySuccess } from '../lib/queryClient';

const STATUS_OPTIONS = (Object.keys(RUN_STATUS) as RunStatus[]).map((value) => ({
  value,
  label: RUN_STATUS[value].label,
}));
const TRIGGER_OPTIONS = (Object.keys(RUN_TRIGGER_LABELS) as RunTrigger[]).map((value) => ({
  value,
  label: RUN_TRIGGER_LABELS[value],
}));

function endOfDay(date: Date): Date {
  const end = new Date(date);
  end.setHours(23, 59, 59, 999);
  return end;
}

export function RunsPage() {
  const fmt = useFormat();
  const pagination = usePagination();
  const [taskType, setTaskType] = useState<TaskType | null>(null);
  const [status, setStatus] = useState<RunStatus | null>(null);
  const [trigger, setTrigger] = useState<RunTrigger | null>(null);
  const [dates, setDates] = useState<DatesRangeValue>([null, null]);
  const [launchOpened, setLaunchOpened] = useState(false);

  const [from, to] = dates;
  const filters: RunFilters = {
    task_type: taskType ?? undefined,
    status: status ?? undefined,
    trigger: trigger ?? undefined,
    date_from: from ? from.toISOString() : undefined,
    date_to: to ? endOfDay(to).toISOString() : undefined,
  };

  const { data, isLoading, error } = useQuery({
    queryKey: ['runs', pagination.page, filters],
    queryFn: () => listRuns({ limit: pagination.limit, offset: pagination.offset, ...filters }),
    // пока в списке есть активные запуски, статусы обновляются сами
    refetchInterval: (query) =>
      query.state.data?.items.some((run) => run.status === 'queued' || run.status === 'running')
        ? 3000
        : 15_000,
  });

  const resetPage = () => pagination.setPage(1);

  const queryClient = useQueryClient();
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['runs'] });
    void queryClient.invalidateQueries({ queryKey: ['dashboard'] });
  };
  const remove = useMutation({
    mutationFn: (run: Run) => deleteRun(run.id),
    onSuccess: () => {
      invalidate();
      notifySuccess('Запуск удалён');
    },
  });
  const clear = useMutation({
    mutationFn: () => clearRuns(filters),
    onSuccess: ({ deleted }) => {
      resetPage();
      invalidate();
      notifySuccess(`Удалено запусков: ${deleted}`);
    },
  });
  const hasFilters = Object.values(filters).some((value) => value !== undefined);

  return (
    <>
      <PageHeader title="Запуски">
        <Button
          color="red"
          variant="light"
          leftSection={<IconTrash size={16} />}
          loading={clear.isPending}
          disabled={!data?.total}
          onClick={() =>
            confirmDelete(
              `${hasFilters ? 'завершённые запуски под текущими фильтрами' : 'все завершённые запуски'} вместе с логами (активные останутся)`,
              () => clear.mutate(),
            )
          }
        >
          Очистить историю
        </Button>
        <Button leftSection={<IconPlayerPlay size={16} />} onClick={() => setLaunchOpened(true)}>
          Запустить вручную
        </Button>
      </PageHeader>

      <Group mb="md" align="flex-end" wrap="wrap">
        <Select
          label="Тип задачи"
          placeholder="Все"
          data={TASK_TYPE_OPTIONS}
          value={taskType}
          onChange={(value) => {
            setTaskType(value as TaskType | null);
            resetPage();
          }}
          clearable
          w={210}
        />
        <Select
          label="Статус"
          placeholder="Все"
          data={STATUS_OPTIONS}
          value={status}
          onChange={(value) => {
            setStatus(value as RunStatus | null);
            resetPage();
          }}
          clearable
          w={170}
        />
        <Select
          label="Запущен"
          placeholder="Как угодно"
          data={TRIGGER_OPTIONS}
          value={trigger}
          onChange={(value) => {
            setTrigger(value as RunTrigger | null);
            resetPage();
          }}
          clearable
          w={170}
        />
        <DatePickerInput
          type="range"
          label="Дата запуска"
          placeholder="Любая"
          value={dates}
          onChange={(value) => {
            setDates(value);
            resetPage();
          }}
          valueFormat="DD.MM.YYYY"
          clearable
          allowSingleDateInRange
          w={240}
        />
      </Group>

      <QueryState
        isLoading={isLoading}
        error={error}
        isEmpty={data?.items.length === 0}
        emptyText="Запусков, подходящих под фильтры, нет"
      >
        <Table highlightOnHover verticalSpacing="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>№</Table.Th>
              <Table.Th>Задача</Table.Th>
              <Table.Th>Запущен</Table.Th>
              <Table.Th>Статус</Table.Th>
              <Table.Th>Создан</Table.Th>
              <Table.Th>Длительность</Table.Th>
              <Table.Th>Комментарий</Table.Th>
              <Table.Th w={50} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data?.items.map((run) => (
              <Table.Tr key={run.id}>
                <Table.Td>
                  <Anchor component={Link} to={`/runs/${run.id}`} fw={600}>
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
                <Table.Td maw={360}>
                  <Text size="sm" c={run.status === 'failed' ? 'red' : 'dimmed'} lineClamp={2}>
                    {run.error ?? ''}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <ActionIcon
                    variant="subtle"
                    color="red"
                    aria-label="Удалить"
                    disabled={run.status === 'queued' || run.status === 'running'}
                    onClick={() =>
                      confirmDelete(`запуск №${run.id} вместе с логами`, () => remove.mutate(run))
                    }
                  >
                    <IconTrash size={16} />
                  </ActionIcon>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
        <TablePagination
          total={data?.total ?? 0}
          page={pagination.page}
          onChange={pagination.setPage}
        />
      </QueryState>

      <LaunchRunModal opened={launchOpened} onClose={() => setLaunchOpened(false)} />
    </>
  );
}
