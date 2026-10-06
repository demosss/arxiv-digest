import { Alert, Center, Group, Loader, Pagination, Stack, Text, Title } from '@mantine/core';
import { modals } from '@mantine/modals';
import { IconAlertCircle } from '@tabler/icons-react';
import { useState, type ReactNode } from 'react';

import { getErrorMessage } from '../api/client';

export const PAGE_SIZE = 20;

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <Group justify="space-between" mb="md" wrap="wrap">
      <Title order={2}>{title}</Title>
      {children ? <Group gap="sm">{children}</Group> : null}
    </Group>
  );
}

interface QueryStateProps {
  isLoading: boolean;
  error: unknown;
  isEmpty?: boolean;
  emptyText?: string;
  children: ReactNode;
}

/** Общие состояния списка: загрузка, ошибка, пусто. */
export function QueryState({ isLoading, error, isEmpty, emptyText, children }: QueryStateProps) {
  if (isLoading) {
    return (
      <Center py="xl">
        <Loader />
      </Center>
    );
  }
  if (error) {
    return (
      <Alert color="red" icon={<IconAlertCircle size={18} />} title="Не удалось загрузить данные">
        {getErrorMessage(error)}
      </Alert>
    );
  }
  if (isEmpty) {
    return (
      <Center py="xl">
        <Text c="dimmed">{emptyText ?? 'Пока ничего нет'}</Text>
      </Center>
    );
  }
  return <>{children}</>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function usePagination(pageSize = PAGE_SIZE) {
  const [page, setPage] = useState(1);
  return { page, setPage, limit: pageSize, offset: (page - 1) * pageSize };
}

interface TablePaginationProps {
  total: number;
  page: number;
  onChange: (page: number) => void;
  pageSize?: number;
}

export function TablePagination({
  total,
  page,
  onChange,
  pageSize = PAGE_SIZE,
}: TablePaginationProps) {
  const pages = Math.ceil(total / pageSize);
  return (
    <Group justify="space-between" mt="md">
      <Text size="sm" c="dimmed">
        Всего: {total}
      </Text>
      {pages > 1 ? <Pagination total={pages} value={page} onChange={onChange} size="sm" /> : null}
    </Group>
  );
}

/** Модальное подтверждение удаления. */
// eslint-disable-next-line react-refresh/only-export-components
export function confirmDelete(what: string, onConfirm: () => void): void {
  modals.openConfirmModal({
    title: 'Подтвердите удаление',
    centered: true,
    children: <Text size="sm">Удалить {what}? Это действие нельзя отменить.</Text>,
    labels: { confirm: 'Удалить', cancel: 'Отмена' },
    confirmProps: { color: 'red' },
    onConfirm,
  });
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Stack gap={2}>
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <div>{children}</div>
    </Stack>
  );
}
