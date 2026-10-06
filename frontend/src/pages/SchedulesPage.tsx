import {
  ActionIcon,
  Alert,
  Button,
  Chip,
  Group,
  Input,
  List,
  Loader,
  Modal,
  SegmentedControl,
  Select,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
} from '@mantine/core';
import { TimeInput } from '@mantine/dates';
import { useDebouncedValue } from '@mantine/hooks';
import { zodResolver } from '@hookform/resolvers/zod';
import { IconPencil, IconPlus, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { Controller, useForm, useWatch } from 'react-hook-form';
import { z } from 'zod';

import {
  createSchedule,
  deleteSchedule,
  listSchedules,
  updateSchedule,
  validateCron,
} from '../api/endpoints';
import type { Schedule, TaskType } from '../api/types';
import {
  PageHeader,
  QueryState,
  TablePagination,
  confirmDelete,
  usePagination,
} from '../components/common';
import { ALL_WEEKDAYS, WEEKDAYS, buildCron, parseSimpleCron } from '../lib/cron';
import { useFormat } from '../lib/format';
import { TASK_TYPE_LABELS, TASK_TYPE_OPTIONS } from '../lib/labels';
import { notifySuccess } from '../lib/queryClient';

const TASK_TYPES = TASK_TYPE_OPTIONS.map((option) => option.value) as [TaskType, ...TaskType[]];

const schema = z
  .object({
    name: z.string().trim().min(1, 'Введите название').max(255),
    task_type: z.enum(TASK_TYPES),
    timezone: z.string().min(1, 'Выберите таймзону'),
    is_active: z.boolean(),
    mode: z.enum(['simple', 'advanced']),
    time: z.string(),
    days: z.array(z.string()),
    cron: z.string(),
  })
  .superRefine((value, ctx) => {
    if (value.mode === 'simple') {
      if (!/^\d{1,2}:\d{2}$/.test(value.time)) {
        ctx.addIssue({ code: 'custom', path: ['time'], message: 'Укажите время' });
      }
      if (value.days.length === 0) {
        ctx.addIssue({ code: 'custom', path: ['days'], message: 'Выберите хотя бы один день' });
      }
    } else if (value.cron.trim().split(/\s+/).length !== 5) {
      ctx.addIssue({
        code: 'custom',
        path: ['cron'],
        message: 'Нужно 5 полей: минута час день месяц день_недели',
      });
    }
  });

type ScheduleForm = z.infer<typeof schema>;

function defaults(schedule: Schedule | null, fallbackTimezone: string): ScheduleForm {
  if (!schedule) {
    return {
      name: '',
      task_type: 'full_pipeline',
      timezone: fallbackTimezone,
      is_active: true,
      mode: 'simple',
      time: '09:00',
      days: [...ALL_WEEKDAYS],
      cron: '0 9 * * *',
    };
  }
  const simple = parseSimpleCron(schedule.cron_expression);
  return {
    name: schedule.name,
    task_type: schedule.task_type,
    timezone: schedule.timezone,
    is_active: schedule.is_active,
    // расписание, которое не укладывается в «время + дни недели», открывается в продвинутом режиме
    mode: simple ? 'simple' : 'advanced',
    time: simple?.time ?? '09:00',
    days: simple?.days ?? [...ALL_WEEKDAYS],
    cron: schedule.cron_expression,
  };
}

function expressionOf(values: Pick<ScheduleForm, 'mode' | 'time' | 'days' | 'cron'>): string {
  if (values.mode === 'simple') {
    return buildCron({ time: values.time, days: values.days }) ?? '';
  }
  return values.cron.trim().replace(/\s+/g, ' ');
}

/** Live-превью: 5 ближайших запусков по данным POST /schedules/validate-cron. */
function CronPreview({ expression, timezone }: { expression: string; timezone: string }) {
  const fmt = useFormat();
  const [debounced] = useDebouncedValue(expression, 350);
  const { data, isFetching } = useQuery({
    queryKey: ['cron-preview', debounced, timezone],
    queryFn: () => validateCron(debounced, timezone),
    enabled: debounced.length > 0 && timezone.length > 0,
    staleTime: 30_000,
  });

  if (!debounced) {
    return null;
  }
  if (!data) {
    return isFetching ? <Loader size="xs" /> : null;
  }
  if (!data.valid) {
    return (
      <Alert color="red" variant="light" title="Некорректное расписание">
        {data.error}
      </Alert>
    );
  }
  return (
    <Alert color="blue" variant="light" title={`Ближайшие запуски (cron: ${data.cron_expression})`}>
      <List size="sm" spacing={2}>
        {data.next_runs.map((run) => (
          <List.Item key={run}>{fmt.dateTime(run)}</List.Item>
        ))}
      </List>
      <Text size="xs" c="dimmed" mt={4}>
        Время показано в таймзоне сервиса ({fmt.timezone}); расписание считается в {timezone}
      </Text>
    </Alert>
  );
}

function ScheduleModal({
  schedule,
  opened,
  onClose,
}: {
  schedule: Schedule | null;
  opened: boolean;
  onClose: () => void;
}) {
  const fmt = useFormat();
  const queryClient = useQueryClient();
  const form = useForm<ScheduleForm>({
    resolver: zodResolver(schema),
    defaultValues: defaults(null, fmt.timezone),
  });
  const { reset, control } = form;

  useEffect(() => {
    if (opened) {
      reset(defaults(schedule, fmt.timezone));
    }
  }, [opened, schedule, reset, fmt.timezone]);

  const [mode, time, days, cron, timezone] = useWatch({
    control,
    name: ['mode', 'time', 'days', 'cron', 'timezone'],
  });
  const expression = expressionOf({ mode, time, days, cron });

  const timezones = useMemo(() => {
    const supported = Intl.supportedValuesOf('timeZone');
    return [...new Set(['UTC', fmt.timezone, ...supported])];
  }, [fmt.timezone]);

  const mutation = useMutation({
    mutationFn: (values: ScheduleForm) => {
      const payload = {
        name: values.name,
        task_type: values.task_type,
        timezone: values.timezone,
        is_active: values.is_active,
        cron_expression: expressionOf(values),
      };
      return schedule ? updateSchedule(schedule.id, payload) : createSchedule(payload);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['schedules'] });
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] });
      notifySuccess(schedule ? 'Расписание обновлено' : 'Расписание создано');
      onClose();
    },
  });
  const { errors } = form.formState;

  const switchMode = (next: string) => {
    if (next === 'advanced') {
      // при переходе в продвинутый режим подставляем то, что набрано в простом
      form.setValue('cron', buildCron({ time, days }) ?? cron);
    } else {
      const simple = parseSimpleCron(cron);
      if (simple) {
        form.setValue('time', simple.time);
        form.setValue('days', simple.days);
      }
    }
    form.setValue('mode', next as ScheduleForm['mode'], { shouldValidate: true });
  };

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={schedule ? 'Редактирование расписания' : 'Новое расписание'}
      centered
      size="lg"
    >
      <form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}>
        <Stack>
          <TextInput
            label="Название"
            placeholder="Ежедневный дайджест"
            error={errors.name?.message}
            {...form.register('name')}
          />
          <Group grow align="flex-start">
            <Controller
              control={control}
              name="task_type"
              render={({ field }) => (
                <Select
                  label="Тип задачи"
                  data={TASK_TYPE_OPTIONS}
                  value={field.value}
                  onChange={(value) => value && field.onChange(value)}
                  allowDeselect={false}
                />
              )}
            />
            <Controller
              control={control}
              name="timezone"
              render={({ field }) => (
                <Select
                  label="Таймзона"
                  data={timezones}
                  value={field.value}
                  onChange={(value) => value && field.onChange(value)}
                  searchable
                  allowDeselect={false}
                  error={errors.timezone?.message}
                />
              )}
            />
          </Group>

          <SegmentedControl
            value={mode}
            onChange={switchMode}
            data={[
              { value: 'simple', label: 'Простой режим' },
              { value: 'advanced', label: 'Продвинутый (cron)' },
            ]}
          />

          {mode === 'simple' ? (
            <>
              <Controller
                control={control}
                name="time"
                render={({ field }) => (
                  <TimeInput
                    label="Время запуска"
                    value={field.value}
                    onChange={(event) => field.onChange(event.currentTarget.value)}
                    error={errors.time?.message}
                    w={160}
                  />
                )}
              />
              <Controller
                control={control}
                name="days"
                render={({ field }) => (
                  <Input.Wrapper label="Дни недели" error={errors.days?.message}>
                    <Chip.Group multiple value={field.value} onChange={field.onChange}>
                      <Group gap="xs" mt={4}>
                        {WEEKDAYS.map((day) => (
                          <Chip key={day.value} value={day.value} size="sm">
                            {day.label}
                          </Chip>
                        ))}
                      </Group>
                    </Chip.Group>
                  </Input.Wrapper>
                )}
              />
            </>
          ) : (
            <TextInput
              label="Cron-выражение"
              description="минута час день_месяца месяц день_недели, например: 30 8 * * 1-5"
              placeholder="0 9 * * *"
              styles={{ input: { fontFamily: 'monospace' } }}
              error={errors.cron?.message}
              {...form.register('cron')}
            />
          )}

          <CronPreview expression={expression} timezone={timezone} />

          <Controller
            control={control}
            name="is_active"
            render={({ field }) => (
              <Switch
                label="Расписание активно"
                checked={field.value}
                onChange={(event) => field.onChange(event.currentTarget.checked)}
              />
            )}
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              Отмена
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              Сохранить
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}

export function SchedulesPage() {
  const fmt = useFormat();
  const queryClient = useQueryClient();
  const pagination = usePagination();
  const [editing, setEditing] = useState<Schedule | null>(null);
  const [modalOpened, setModalOpened] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ['schedules', pagination.page],
    queryFn: () => listSchedules({ limit: pagination.limit, offset: pagination.offset }),
    refetchInterval: 30_000,
  });
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['schedules'] });
    void queryClient.invalidateQueries({ queryKey: ['dashboard'] });
  };

  const toggle = useMutation({
    mutationFn: (schedule: Schedule) =>
      updateSchedule(schedule.id, { is_active: !schedule.is_active }),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (schedule: Schedule) => deleteSchedule(schedule.id),
    onSuccess: () => {
      invalidate();
      notifySuccess('Расписание удалено');
    },
  });

  const openModal = (schedule: Schedule | null) => {
    setEditing(schedule);
    setModalOpened(true);
  };

  return (
    <>
      <PageHeader title="Расписания">
        <Button leftSection={<IconPlus size={16} />} onClick={() => openModal(null)}>
          Добавить расписание
        </Button>
      </PageHeader>

      <QueryState
        isLoading={isLoading}
        error={error}
        isEmpty={data?.items.length === 0}
        emptyText="Расписаний пока нет — задачи запускаются только вручную"
      >
        <Table highlightOnHover verticalSpacing="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Название</Table.Th>
              <Table.Th>Тип задачи</Table.Th>
              <Table.Th>Cron</Table.Th>
              <Table.Th>Таймзона</Table.Th>
              <Table.Th>Следующий запуск</Table.Th>
              <Table.Th>Последний запуск</Table.Th>
              <Table.Th>Активно</Table.Th>
              <Table.Th w={90} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data?.items.map((schedule) => (
              <Table.Tr key={schedule.id}>
                <Table.Td fw={500}>{schedule.name}</Table.Td>
                <Table.Td>{TASK_TYPE_LABELS[schedule.task_type]}</Table.Td>
                <Table.Td>
                  <Text size="sm" ff="monospace">
                    {schedule.cron_expression}
                  </Text>
                </Table.Td>
                <Table.Td>{schedule.timezone}</Table.Td>
                <Table.Td>{fmt.dateTime(schedule.next_fire_at)}</Table.Td>
                <Table.Td>{fmt.dateTime(schedule.last_fired_at)}</Table.Td>
                <Table.Td>
                  <Switch
                    checked={schedule.is_active}
                    onChange={() => toggle.mutate(schedule)}
                    aria-label={`Расписание ${schedule.name} активно`}
                  />
                </Table.Td>
                <Table.Td>
                  <Group gap={4} justify="flex-end" wrap="nowrap">
                    <ActionIcon
                      variant="subtle"
                      aria-label="Редактировать"
                      onClick={() => openModal(schedule)}
                    >
                      <IconPencil size={16} />
                    </ActionIcon>
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      aria-label="Удалить"
                      onClick={() =>
                        confirmDelete(`расписание «${schedule.name}»`, () =>
                          remove.mutate(schedule),
                        )
                      }
                    >
                      <IconTrash size={16} />
                    </ActionIcon>
                  </Group>
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

      <ScheduleModal
        schedule={editing}
        opened={modalOpened}
        onClose={() => setModalOpened(false)}
      />
    </>
  );
}
