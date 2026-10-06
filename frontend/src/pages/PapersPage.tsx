import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Divider,
  Drawer,
  Group,
  Loader,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { DatePickerInput, type DatesRangeValue } from '@mantine/dates';
import { useDebouncedValue } from '@mantine/hooks';
import { IconExternalLink, IconRefresh, IconSearch, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';

import {
  clearPapers,
  deletePaper,
  getPaper,
  listPapers,
  listTopics,
  resummarizePaper,
  type PaperFilters,
} from '../api/endpoints';
import type { Paper, PaperStatus } from '../api/types';
import { PaperStatusBadge } from '../components/Badges';
import {
  Field,
  PageHeader,
  QueryState,
  TablePagination,
  confirmDelete,
  usePagination,
} from '../components/common';
import { formatDuration } from '../lib/dates';
import { useFormat } from '../lib/format';
import { PAPER_STATUS } from '../lib/labels';
import { notifySuccess } from '../lib/queryClient';

const STATUS_OPTIONS = (Object.keys(PAPER_STATUS) as PaperStatus[]).map((value) => ({
  value,
  label: PAPER_STATUS[value].label,
}));
const SUMMARY_LABEL_RE = /^(Задача|Метод|Результаты|Значимость):\s*/;

function endOfDay(date: Date): Date {
  const end = new Date(date);
  end.setHours(23, 59, 59, 999);
  return end;
}

/** Резюме по абзацам с выделенными названиями разделов. */
function SummaryText({ text }: { text: string }) {
  const paragraphs = text.split(/\n+/).filter((line) => line.trim().length > 0);
  return (
    <Stack gap="xs">
      {paragraphs.map((paragraph, index) => {
        const match = SUMMARY_LABEL_RE.exec(paragraph);
        return (
          <Text key={index} size="sm" lh={1.6}>
            {match ? (
              <>
                <Text span fw={700} inherit>
                  {match[1]}:
                </Text>{' '}
                {paragraph.slice(match[0].length)}
              </>
            ) : (
              paragraph
            )}
          </Text>
        );
      })}
    </Stack>
  );
}

function PaperDrawer({ paperId, onClose }: { paperId: number | null; onClose: () => void }) {
  const fmt = useFormat();
  const queryClient = useQueryClient();
  const { data: paper, isLoading } = useQuery({
    queryKey: ['paper', paperId],
    queryFn: () => getPaper(paperId as number),
    enabled: paperId !== null,
  });

  const resummarize = useMutation({
    mutationFn: (id: number) => resummarizePaper(id),
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ['runs'] });
      notifySuccess(`Пересуммаризация поставлена в очередь: запуск №${run.id}`);
    },
  });

  return (
    <Drawer
      opened={paperId !== null}
      onClose={onClose}
      position="right"
      size="xl"
      title={paper ? `arXiv:${paper.arxiv_id}v${paper.version}` : 'Статья'}
    >
      {isLoading ? <Loader /> : null}
      {paper ? (
        <Stack gap="md">
          <Title order={4} lh={1.3}>
            {paper.title}
          </Title>
          <Text size="sm" c="dimmed">
            {paper.authors.join(', ')}
          </Text>
          <Group gap="xs">
            <PaperStatusBadge status={paper.status} />
            {paper.categories.map((category) => (
              <Badge key={category} variant="outline" color="gray" tt="none">
                {category}
              </Badge>
            ))}
          </Group>
          <Group gap="sm">
            <Button
              component="a"
              href={paper.abs_url}
              target="_blank"
              rel="noreferrer"
              variant="light"
              leftSection={<IconExternalLink size={16} />}
            >
              Открыть на arXiv
            </Button>
            <Button
              component="a"
              href={paper.pdf_url}
              target="_blank"
              rel="noreferrer"
              variant="default"
            >
              PDF
            </Button>
            <Button
              variant="default"
              leftSection={<IconRefresh size={16} />}
              loading={resummarize.isPending}
              disabled={paper.text_chars === null}
              onClick={() => resummarize.mutate(paper.id)}
            >
              Пересуммаризировать
            </Button>
          </Group>

          {paper.error ? (
            <Alert color="red" title="Ошибка обработки">
              {paper.error}
            </Alert>
          ) : null}

          <SimpleGrid cols={3} spacing="md">
            <Field label="Опубликована">
              <Text size="sm">{fmt.dateTime(paper.published_at)}</Text>
            </Field>
            <Field label="Обновлена на arXiv">
              <Text size="sm">{fmt.dateTime(paper.updated_at_arxiv)}</Text>
            </Field>
            <Field label="Объём текста">
              <Text size="sm">
                {paper.text_chars !== null
                  ? `${paper.text_chars.toLocaleString('ru-RU')} симв.`
                  : 'не извлечён'}
              </Text>
            </Field>
          </SimpleGrid>

          <Field label="Темы">
            {paper.topics.length ? (
              <Stack gap="xs">
                {paper.topics.map((topic) => (
                  <div key={topic.id}>
                    <Text size="sm" fw={600}>
                      {topic.name}
                    </Text>
                    {topic.matched_queries.length ? (
                      <Stack gap={4} mt={4}>
                        <Text size="xs" c="dimmed">
                          Найдена запросами:
                        </Text>
                        {topic.matched_queries.map((query) => (
                          <Code key={query} block fz={12} style={{ whiteSpace: 'pre-wrap' }}>
                            {query}
                          </Code>
                        ))}
                      </Stack>
                    ) : topic.matched_keywords.length ? (
                      <Text size="xs" c="dimmed">
                        Совпавшие ключевые слова: {topic.matched_keywords.join(', ')}
                      </Text>
                    ) : null}
                  </div>
                ))}
              </Stack>
            ) : (
              <Text size="sm" c="dimmed">
                нет
              </Text>
            )}
          </Field>

          <Divider label="Резюме" labelPosition="left" />
          {paper.summary ? (
            <>
              <SummaryText text={paper.summary.text_ru} />
              <Text size="xs" c="dimmed">
                Модель {paper.summary.model} · чанков: {paper.summary.chunks_count} · токены:{' '}
                {paper.summary.prompt_tokens} → {paper.summary.completion_tokens} ·{' '}
                {formatDuration(paper.summary.duration_ms)} ·{' '}
                {fmt.dateTime(paper.summary.updated_at)}
              </Text>
            </>
          ) : (
            <Text size="sm" c="dimmed">
              Резюме ещё нет.{' '}
              <Anchor component={Link} to="/runs" size="sm">
                Запустите суммаризацию
              </Anchor>{' '}
              или нажмите «Пересуммаризировать».
            </Text>
          )}

          <Divider label="Аннотация (оригинал)" labelPosition="left" />
          <Text size="sm" lh={1.6}>
            {paper.abstract}
          </Text>
        </Stack>
      ) : null}
    </Drawer>
  );
}

export function PapersPage() {
  const fmt = useFormat();
  const pagination = usePagination();
  const [search, setSearch] = useState('');
  const [debouncedSearch] = useDebouncedValue(search, 300);
  const [topicId, setTopicId] = useState<string | null>(null);
  const [status, setStatus] = useState<PaperStatus | null>(null);
  const [dates, setDates] = useState<DatesRangeValue>([null, null]);
  const [selected, setSelected] = useState<number | null>(null);

  const { data: topics } = useQuery({
    queryKey: ['topics', 'all'],
    queryFn: () => listTopics({ limit: 500, offset: 0 }),
    staleTime: 60_000,
  });

  const [from, to] = dates;
  const filters: PaperFilters = {
    topic_id: topicId ? Number(topicId) : undefined,
    status: status ?? undefined,
    date_from: from ? from.toISOString() : undefined,
    date_to: to ? endOfDay(to).toISOString() : undefined,
    q: debouncedSearch.trim() || undefined,
  };
  const { data, isLoading, error } = useQuery({
    queryKey: ['papers', pagination.page, filters],
    queryFn: () => listPapers({ limit: pagination.limit, offset: pagination.offset, ...filters }),
  });

  const resetPage = () => pagination.setPage(1);

  const queryClient = useQueryClient();
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['papers'] });
    void queryClient.invalidateQueries({ queryKey: ['digests'] });
    void queryClient.invalidateQueries({ queryKey: ['dashboard'] });
  };
  const remove = useMutation({
    mutationFn: (paper: Paper) => deletePaper(paper.id),
    onSuccess: (_, paper) => {
      if (selected === paper.id) setSelected(null);
      invalidate();
      notifySuccess('Статья удалена');
    },
  });
  const clear = useMutation({
    mutationFn: () => clearPapers(filters),
    onSuccess: ({ deleted }) => {
      setSelected(null);
      resetPage();
      invalidate();
      notifySuccess(`Удалено статей: ${deleted}`);
    },
  });
  const hasFilters = Object.values(filters).some((value) => value !== undefined);

  return (
    <>
      <PageHeader title="Статьи">
        <Button
          color="red"
          variant="light"
          leftSection={<IconTrash size={16} />}
          loading={clear.isPending}
          disabled={!data?.total}
          onClick={() =>
            confirmDelete(
              `${hasFilters ? 'статьи под текущими фильтрами' : 'все статьи'} (${data?.total ?? 0}) вместе с резюме и PDF-файлами`,
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
          placeholder="Название или arXiv ID"
          leftSection={<IconSearch size={16} />}
          value={search}
          onChange={(event) => {
            setSearch(event.currentTarget.value);
            resetPage();
          }}
          w={280}
        />
        <Select
          label="Тема"
          placeholder="Все"
          data={(topics?.items ?? []).map((topic) => ({
            value: String(topic.id),
            label: topic.name,
          }))}
          value={topicId}
          onChange={(value) => {
            setTopicId(value);
            resetPage();
          }}
          clearable
          searchable
          w={220}
        />
        <Select
          label="Статус"
          placeholder="Все"
          data={STATUS_OPTIONS}
          value={status}
          onChange={(value) => {
            setStatus(value as PaperStatus | null);
            resetPage();
          }}
          clearable
          w={190}
        />
        <DatePickerInput
          type="range"
          label="Дата публикации"
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
        emptyText="Статей, подходящих под фильтры, нет"
      >
        <Table highlightOnHover verticalSpacing="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Статья</Table.Th>
              <Table.Th>Темы</Table.Th>
              <Table.Th>Опубликована</Table.Th>
              <Table.Th>Статус</Table.Th>
              <Table.Th w={50} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data?.items.map((paper) => (
              <Table.Tr
                key={paper.id}
                onClick={() => setSelected(paper.id)}
                style={{ cursor: 'pointer' }}
              >
                <Table.Td maw={560}>
                  <Text size="sm" fw={600} lineClamp={2}>
                    {paper.title}
                  </Text>
                  <Text size="xs" c="dimmed" lineClamp={1}>
                    arXiv:{paper.arxiv_id} · {paper.authors.slice(0, 3).join(', ')}
                    {paper.authors.length > 3 ? ' и др.' : ''}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Group gap={4}>
                    {paper.topics.map((topic) => (
                      <Badge key={topic.id} variant="light" tt="none">
                        {topic.name}
                      </Badge>
                    ))}
                  </Group>
                </Table.Td>
                <Table.Td>{fmt.date(paper.published_at)}</Table.Td>
                <Table.Td>
                  <PaperStatusBadge status={paper.status} />
                </Table.Td>
                <Table.Td>
                  <ActionIcon
                    variant="subtle"
                    color="red"
                    aria-label="Удалить"
                    onClick={(event) => {
                      event.stopPropagation();
                      confirmDelete(`статью arXiv:${paper.arxiv_id}`, () => remove.mutate(paper));
                    }}
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

      <PaperDrawer paperId={selected} onClose={() => setSelected(null)} />
    </>
  );
}
