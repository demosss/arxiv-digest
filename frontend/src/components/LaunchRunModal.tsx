import {
  Alert,
  Button,
  Checkbox,
  Group,
  Modal,
  NumberInput,
  Select,
  Stack,
  Text,
} from '@mantine/core';
import { DatePickerInput, type DatesRangeValue } from '@mantine/dates';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { createRun } from '../api/endpoints';
import type { RunCreate, TaskType } from '../api/types';
import { utcDayBounds } from '../lib/dates';
import { TASK_TYPE_OPTIONS } from '../lib/labels';
import { notifySuccess } from '../lib/queryClient';

const PERIOD_TASKS: TaskType[] = ['full_pipeline', 'collect', 'build_digest'];

const DESCRIPTIONS: Record<TaskType, string> = {
  full_pipeline: 'Сбор → суммаризация → сборка дайджеста → рассылка, одной цепочкой.',
  collect: 'Поиск препринтов по активным темам, скачивание PDF и извлечение текста.',
  summarize: 'Резюме для всех статей с извлечённым текстом.',
  build_digest: 'PDF-дайджест из статей периода, у которых уже есть резюме.',
  send_digest: 'Рассылка дайджеста активным получателям.',
};

interface Props {
  opened: boolean;
  onClose: () => void;
  initialType?: TaskType;
}

/** Ручной запуск любого шага или всего пайплайна с выбором периода. */
export function LaunchRunModal({ opened, onClose, initialType = 'full_pipeline' }: Props) {
  return (
    <Modal opened={opened} onClose={onClose} title="Ручной запуск" centered size="md">
      {/* содержимое монтируется при каждом открытии, поэтому форма всегда начинается с чистого листа */}
      <LaunchRunForm onClose={onClose} initialType={initialType} />
    </Modal>
  );
}

function LaunchRunForm({ onClose, initialType }: { onClose: () => void; initialType: TaskType }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [taskType, setTaskType] = useState<TaskType>(initialType);
  const [period, setPeriod] = useState<DatesRangeValue>([null, null]);
  const [digestId, setDigestId] = useState<number | string>('');
  const [force, setForce] = useState(false);
  const [retryFailed, setRetryFailed] = useState(false);

  const mutation = useMutation({
    mutationFn: createRun,
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ['runs'] });
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] });
      notifySuccess(
        run.status === 'skipped'
          ? (run.error ?? 'Запуск пропущен')
          : `Запуск №${run.id} поставлен в очередь`,
      );
      onClose();
      navigate(`/runs/${run.id}`);
    },
  });

  const usesPeriod = PERIOD_TASKS.includes(taskType);
  const [from, to] = period;
  // в диапазоне выбрана только первая дата: ждём вторую
  const periodIncomplete = usesPeriod && from !== null && to === null;

  const submit = () => {
    const payload: RunCreate = { task_type: taskType };
    if (usesPeriod && from && to) {
      const bounds = utcDayBounds(from, to);
      payload.period_start = bounds.start;
      payload.period_end = bounds.end;
    }
    if (taskType === 'send_digest') {
      if (typeof digestId === 'number') {
        payload.digest_id = digestId;
      }
      payload.force = force;
    }
    if (taskType === 'summarize') {
      payload.retry_failed = retryFailed;
    }
    mutation.mutate(payload);
  };

  return (
    <Stack>
      <Select
        label="Задача"
        data={TASK_TYPE_OPTIONS}
        value={taskType}
        onChange={(value) => value && setTaskType(value as TaskType)}
        allowDeselect={false}
      />
      <Text size="sm" c="dimmed">
        {DESCRIPTIONS[taskType]}
      </Text>

      {usesPeriod ? (
        <DatePickerInput
          type="range"
          label="Период (по датам UTC)"
          description="Если не выбирать — вчерашние сутки"
          placeholder="Вчерашний день"
          value={period}
          onChange={setPeriod}
          maxDate={new Date()}
          valueFormat="DD.MM.YYYY"
          clearable
          allowSingleDateInRange
        />
      ) : null}

      {taskType === 'summarize' ? (
        <Checkbox
          label="Повторить и для статей, у которых суммаризация раньше не удалась"
          checked={retryFailed}
          onChange={(event) => setRetryFailed(event.currentTarget.checked)}
        />
      ) : null}

      {taskType === 'send_digest' ? (
        <>
          <NumberInput
            label="Номер дайджеста"
            description="Пусто — последний дайджест со статусом «Готов»"
            value={digestId}
            onChange={setDigestId}
            min={1}
            allowDecimal={false}
          />
          <Checkbox
            label="Отправить повторно тем, кто уже получил этот дайджест"
            checked={force}
            onChange={(event) => setForce(event.currentTarget.checked)}
          />
        </>
      ) : null}

      {taskType === 'full_pipeline' ? (
        <Alert color="gray" variant="light">
          Если пайплайн уже выполняется, новый запуск будет создан со статусом «Пропущен».
        </Alert>
      ) : null}

      <Group justify="flex-end">
        <Button variant="default" onClick={onClose}>
          Отмена
        </Button>
        <Button onClick={submit} loading={mutation.isPending} disabled={periodIncomplete}>
          Запустить
        </Button>
      </Group>
    </Stack>
  );
}
