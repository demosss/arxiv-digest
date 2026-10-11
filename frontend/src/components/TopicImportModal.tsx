import {
  Alert,
  Button,
  Group,
  List,
  Loader,
  Modal,
  SegmentedControl,
  Stack,
  Text,
} from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { getErrorMessage } from '../api/client';
import { importTopicsYaml } from '../api/endpoints';
import type { ImportIssue, TopicImportMode, TopicImportReport } from '../api/types';
import { notifySuccess } from '../lib/queryClient';

const MODE_OPTIONS = [
  { value: 'merge', label: 'Объединить' },
  { value: 'replace', label: 'Заменить все' },
];

/** «тема №2 «Датасеты», запрос №3, позиция 1: Незакрытая скобка» */
function formatIssue(issue: ImportIssue): string {
  const [, topicIndex, field, itemIndex] = issue.loc;
  const parts: string[] = [];
  if (typeof topicIndex === 'number') {
    parts.push(`тема №${topicIndex + 1}${issue.topic ? ` «${issue.topic}»` : ''}`);
  }
  if (field === 'queries' && typeof itemIndex === 'number') {
    parts.push(`запрос №${itemIndex + 1}`);
  } else if (typeof field === 'string') {
    parts.push(`поле ${field}`);
  }
  return parts.length ? `${parts.join(', ')}: ${issue.msg}` : issue.msg;
}

function NameList({ title, names, color }: { title: string; names: string[]; color?: string }) {
  if (names.length === 0) {
    return null;
  }
  return (
    <div>
      <Text size="sm" fw={600} c={color}>
        {title}: {names.length}
      </Text>
      <List size="sm" spacing={2}>
        {names.map((name) => (
          <List.Item key={name}>{name}</List.Item>
        ))}
      </List>
    </div>
  );
}

function Report({ report }: { report: TopicImportReport }) {
  if (report.errors.length > 0) {
    return (
      <Alert color="red" title="В файле есть ошибки — импорт невозможен">
        <List size="sm" spacing={4}>
          {report.errors.map((issue, index) => (
            <List.Item key={index}>{formatIssue(issue)}</List.Item>
          ))}
        </List>
      </Alert>
    );
  }
  const changes = report.created.length + report.updated.length + report.deleted.length;
  return (
    <Stack gap="sm">
      {changes === 0 ? <Alert color="gray">Изменений нет: темы совпадают с файлом</Alert> : null}
      <NameList title="Будут созданы" names={report.created} color="green.8" />
      <NameList title="Будут обновлены" names={report.updated} color="blue.8" />
      <NameList title="Будут удалены" names={report.deleted} color="red.8" />
      {report.unchanged.length > 0 ? (
        <Text size="sm" c="dimmed">
          Без изменений: {report.unchanged.length}
        </Text>
      ) : null}
    </Stack>
  );
}

function ImportForm({ file, onClose }: { file: File; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<TopicImportMode>('merge');

  // пробный прогон: сервер проверяет файл и показывает изменения, ничего не записывая
  const dryRun = useQuery({
    queryKey: ['topics-import-preview', file.name, file.size, file.lastModified, mode],
    queryFn: () => importTopicsYaml(file, mode, true),
    gcTime: 0,
    staleTime: Infinity,
    retry: false,
  });
  const apply = useMutation({
    mutationFn: () => importTopicsYaml(file, mode, false),
    onSuccess: (report) => {
      void queryClient.invalidateQueries({ queryKey: ['topics'] });
      notifySuccess(
        `Создано: ${report.created.length}, обновлено: ${report.updated.length}, удалено: ${report.deleted.length}`,
        'Темы импортированы',
      );
      onClose();
    },
  });

  const report = dryRun.data;
  const changes = report
    ? report.created.length + report.updated.length + report.deleted.length
    : 0;

  return (
    <Stack>
      <Text size="sm">
        Файл: <b>{file.name}</b>
      </Text>
      <div>
        <SegmentedControl
          data={MODE_OPTIONS}
          value={mode}
          onChange={(value) => setMode(value as TopicImportMode)}
        />
        <Text size="xs" c="dimmed" mt={4}>
          {mode === 'merge'
            ? 'Темы с тем же названием обновляются, новые добавляются, остальные не меняются'
            : 'Темы, которых нет в файле, будут удалены вместе со связями со статьями'}
        </Text>
      </div>

      {dryRun.isLoading ? <Loader size="sm" /> : null}
      {dryRun.error ? (
        <Alert color="red" title="Не удалось проверить файл">
          {getErrorMessage(dryRun.error)}
        </Alert>
      ) : null}
      {report ? <Report report={report} /> : null}

      <Group justify="flex-end">
        <Button variant="default" onClick={onClose}>
          Отмена
        </Button>
        <Button
          color={mode === 'replace' && report?.deleted.length ? 'red' : undefined}
          loading={apply.isPending}
          disabled={!report || report.errors.length > 0 || changes === 0 || dryRun.isFetching}
          onClick={() => apply.mutate()}
        >
          Импортировать
        </Button>
      </Group>
    </Stack>
  );
}

export function TopicImportModal({ file, onClose }: { file: File | null; onClose: () => void }) {
  return (
    <Modal opened={file !== null} onClose={onClose} title="Импорт тем из YAML" centered size="lg">
      {file ? <ImportForm file={file} onClose={onClose} /> : null}
    </Modal>
  );
}
