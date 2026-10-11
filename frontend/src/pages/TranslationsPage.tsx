import {
  ActionIcon,
  Anchor,
  Button,
  Group,
  Select,
  Table,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { useDebouncedValue } from '@mantine/hooks';
import { IconDownload, IconListDetails, IconSearch, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';

import {
  clearTranslations,
  deleteTranslation,
  downloadTranslation,
  listTranslations,
  type TranslationFilters,
} from '../api/endpoints';
import type { TranslationListItem, TranslationStatus } from '../api/types';
import { TranslationStatusBadge } from '../components/Badges';
import {
  PageHeader,
  QueryState,
  TablePagination,
  confirmDelete,
  usePagination,
} from '../components/common';
import { formatDuration } from '../lib/dates';
import { useFormat } from '../lib/format';
import { TRANSLATION_SOURCE_LABELS, TRANSLATION_STATUS } from '../lib/labels';
import { notifySuccess } from '../lib/queryClient';

const STATUS_OPTIONS = (Object.keys(TRANSLATION_STATUS) as TranslationStatus[]).map((value) => ({
  value,
  label: TRANSLATION_STATUS[value].label,
}));

function isActive(translation: Pick<TranslationListItem, 'status'>): boolean {
  return translation.status === 'queued' || translation.status === 'running';
}

export function TranslationsPage() {
  const fmt = useFormat();
  const pagination = usePagination();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState('');
  const [debouncedSearch] = useDebouncedValue(search, 300);
  const [status, setStatus] = useState<TranslationStatus | null>(null);

  const filters: TranslationFilters = {
    status: status ?? undefined,
    q: debouncedSearch.trim() || undefined,
  };
  const { data, isLoading, error } = useQuery({
    queryKey: ['translations', 'list', pagination.page, filters],
    queryFn: () =>
      listTranslations({ limit: pagination.limit, offset: pagination.offset, ...filters }),
    // пока идёт перевод, статусы обновляются сами
    refetchInterval: (query) => (query.state.data?.items.some(isActive) ? 4000 : 15_000),
  });

  const resetPage = () => pagination.setPage(1);
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['translations'] });
    void queryClient.invalidateQueries({ queryKey: ['paper'] });
  };
  const download = useMutation({ mutationFn: downloadTranslation });
  const remove = useMutation({
    mutationFn: (translation: TranslationListItem) => deleteTranslation(translation.id),
    onSuccess: () => {
      invalidate();
      notifySuccess('Перевод удалён');
    },
  });
  const clear = useMutation({
    mutationFn: () => clearTranslations(filters),
    onSuccess: ({ deleted }) => {
      resetPage();
      invalidate();
      notifySuccess(`Удалено переводов: ${deleted}`);
    },
  });
  const hasFilters = Object.values(filters).some((value) => value !== undefined);

  return (
    <>
      <PageHeader title="Переводы">
        <Button
          color="red"
          variant="light"
          leftSection={<IconTrash size={16} />}
          loading={clear.isPending}
          disabled={!data?.total}
          onClick={() =>
            confirmDelete(
              `${hasFilters ? 'переводы под текущими фильтрами' : 'все переводы'} вместе с файлами docx (выполняющиеся останутся)`,
              () => clear.mutate(),
            )
          }
        >
          Очистить
        </Button>
      </PageHeader>

      <Group mb="md" align="flex-end" wrap="wrap">
        <TextInput
          label="Поиск"
          placeholder="Название статьи или arXiv ID"
          leftSection={<IconSearch size={16} />}
          value={search}
          onChange={(event) => {
            setSearch(event.currentTarget.value);
            resetPage();
          }}
          w={280}
        />
        <Select
          label="Статус"
          placeholder="Все"
          data={STATUS_OPTIONS}
          value={status}
          onChange={(value) => {
            setStatus(value as TranslationStatus | null);
            resetPage();
          }}
          clearable
          w={190}
        />
      </Group>

      <QueryState
        isLoading={isLoading}
        error={error}
        isEmpty={data?.items.length === 0}
        emptyText="Переводов пока нет: откройте статью в разделе «Статьи» и нажмите «Перевести»"
      >
        <Table highlightOnHover verticalSpacing="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Статья</Table.Th>
              <Table.Th>Язык</Table.Th>
              <Table.Th>Источник</Table.Th>
              <Table.Th>Статус</Table.Th>
              <Table.Th>Обновлён</Table.Th>
              <Table.Th w={130} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data?.items.map((translation) => (
              <Table.Tr key={translation.id}>
                <Table.Td maw={520}>
                  <Text size="sm" fw={600} lineClamp={2}>
                    {translation.title}
                  </Text>
                  <Text size="xs" c="dimmed">
                    arXiv:{translation.arxiv_id}
                    {translation.filename ? ` · ${translation.filename}` : ''}
                  </Text>
                  {translation.status === 'failed' && translation.error ? (
                    <Text size="xs" c="red" lineClamp={3}>
                      {translation.error}
                    </Text>
                  ) : null}
                </Table.Td>
                <Table.Td>{translation.language_label}</Table.Td>
                <Table.Td>
                  {translation.source ? TRANSLATION_SOURCE_LABELS[translation.source] : '—'}
                </Table.Td>
                <Table.Td>
                  <TranslationStatusBadge status={translation.status} />
                  {translation.status === 'ready' && translation.duration_ms ? (
                    <Text size="xs" c="dimmed" mt={2}>
                      {formatDuration(translation.duration_ms)}
                    </Text>
                  ) : null}
                </Table.Td>
                <Table.Td>{fmt.dateTime(translation.updated_at)}</Table.Td>
                <Table.Td>
                  <Group gap={4} justify="flex-end" wrap="nowrap">
                    <Tooltip
                      label={translation.has_file ? 'Скачать docx' : 'Файл перевода ещё не готов'}
                    >
                      <ActionIcon
                        variant="subtle"
                        aria-label="Скачать docx"
                        disabled={!translation.has_file}
                        loading={download.isPending && download.variables?.id === translation.id}
                        onClick={() => download.mutate(translation)}
                      >
                        <IconDownload size={16} />
                      </ActionIcon>
                    </Tooltip>
                    {translation.run_id !== null ? (
                      <Tooltip label="Запуск и логи">
                        <ActionIcon
                          component={Link}
                          to={`/runs/${translation.run_id}`}
                          variant="subtle"
                          aria-label="Запуск и логи"
                        >
                          <IconListDetails size={16} />
                        </ActionIcon>
                      </Tooltip>
                    ) : null}
                    <Tooltip label={isActive(translation) ? 'Перевод ещё выполняется' : 'Удалить'}>
                      <ActionIcon
                        variant="subtle"
                        color="red"
                        aria-label="Удалить"
                        disabled={isActive(translation)}
                        onClick={() =>
                          confirmDelete(
                            `перевод статьи arXiv:${translation.arxiv_id} (${translation.language_label})`,
                            () => remove.mutate(translation),
                          )
                        }
                      >
                        <IconTrash size={16} />
                      </ActionIcon>
                    </Tooltip>
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
        <Text size="xs" c="dimmed" mt="sm">
          Новый перевод запускается из карточки статьи в разделе{' '}
          <Anchor component={Link} to="/papers" size="xs">
            «Статьи»
          </Anchor>
          .
        </Text>
      </QueryState>
    </>
  );
}
