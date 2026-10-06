import { DragDropContext, Draggable, Droppable, type DropResult } from '@hello-pangea/dnd';
import {
  Accordion,
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Box,
  Button,
  Code,
  Group,
  List,
  Loader,
  Modal,
  NumberInput,
  Stack,
  Switch,
  Table,
  TagsInput,
  Text,
  Textarea,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { useDebouncedValue } from '@mantine/hooks';
import {
  IconChevronDown,
  IconChevronRight,
  IconCopy,
  IconGripVertical,
  IconPlus,
  IconSearch,
  IconTrash,
} from '@tabler/icons-react';
import { useMutation, useQueries, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { createTopic, previewTopic, updateTopic, validateTopicQuery } from '../api/endpoints';
import type { QueryValidation, Topic, TopicInput, TopicPreview } from '../api/types';
import { useFormat } from '../lib/format';
import { POPULAR_ARXIV_CATEGORIES } from '../lib/labels';
import { notifySuccess } from '../lib/queryClient';

const CATEGORY_RE = /^[a-z][a-z-]*(\.[A-Za-z][A-Za-z-]*)?$/;
const VALIDATION_DEBOUNCE_MS = 400;
const MONO = { input: { fontFamily: 'var(--mantine-font-family-monospace)', fontSize: 13 } };

interface QueryRow {
  id: string;
  value: string;
}

let rowCounter = 0;
const newRow = (value = ''): QueryRow => ({ id: `query-${rowCounter++}`, value });

type RowState =
  | { kind: 'empty' }
  | { kind: 'pending' }
  | { kind: 'failed' }
  | { kind: 'checked'; check: QueryValidation };

function SyntaxCheatSheet() {
  return (
    <Accordion variant="contained" chevronPosition="left">
      <Accordion.Item value="syntax">
        <Accordion.Control>
          <Text size="sm" fw={600}>
            Шпаргалка по синтаксису запросов
          </Text>
        </Accordion.Control>
        <Accordion.Panel>
          <Stack gap="xs">
            <Text size="sm">
              <b>Поля:</b> <Code>ti:</Code> заголовок, <Code>abs:</Code> аннотация,{' '}
              <Code>all:</Code> все поля, <Code>au:</Code> автор, <Code>cat:</Code> категория,{' '}
              <Code>co:</Code> комментарий.
            </Text>
            <Text size="sm">
              <b>Операторы</b> (только заглавными): <Code>AND</Code>, <Code>OR</Code>,{' '}
              <Code>ANDNOT</Code>. Группировка — скобками, фразы — в двойных кавычках.
            </Text>
            <Text size="sm">
              Строка без префиксов и операторов считается фразой: <Code>autonomous ship</Code> →{' '}
              <Code>all:&quot;autonomous ship&quot;</Code>.
            </Text>
            <Alert color="yellow" p="xs">
              arXiv ищет только по заголовку, аннотации и метаданным, не по полному тексту.
            </Alert>
            <Text size="sm" fw={600}>
              Примеры
            </Text>
            <List size="sm" spacing={4}>
              <List.Item>
                <Code>all:&quot;operational design domain&quot;</Code> — точная фраза
              </List.Item>
              <List.Item>
                <Code>(ti:dataset OR ti:benchmark) AND (all:railway OR all:rail)</Code> — датасеты
                по железной дороге
              </List.Item>
              <List.Item>
                <Code>abs:ODD AND (abs:autonomous OR abs:automated)</Code> — короткий термин только
                вместе с контекстом
              </List.Item>
              <List.Item>
                <Code>all:&quot;safety case&quot; ANDNOT cat:cs.CL</Code> — исключить категорию
              </List.Item>
            </List>
            <Text size="xs" c="dimmed">
              Запросы темы выполняются по отдельности, статья подходит под любой из них. Если заданы
              категории, они добавляются к каждому запросу через AND.
            </Text>
          </Stack>
        </Accordion.Panel>
      </Accordion.Item>
    </Accordion>
  );
}

function QueryFeedback({ value, state }: { value: string; state: RowState }) {
  if (state.kind === 'empty') {
    return null;
  }
  if (state.kind === 'pending') {
    return (
      <Text size="xs" c="dimmed">
        Проверка…
      </Text>
    );
  }
  if (state.kind === 'failed') {
    return (
      <Text size="xs" c="red">
        Не удалось проверить запрос: сервер недоступен
      </Text>
    );
  }
  const { check } = state;
  return (
    <Stack gap={2}>
      {check.normalized !== value.trim() ? (
        <Text size="xs" c="dimmed">
          Будет сохранён как <Code fz={12}>{check.normalized}</Code>
        </Text>
      ) : null}
      {check.errors.map((issue) => (
        <Text key={`e-${issue.position}-${issue.message}`} size="xs" c="red">
          Позиция {issue.position + 1}: {issue.message}
        </Text>
      ))}
      {check.warnings.map((issue) => (
        <Text key={`w-${issue.position}-${issue.message}`} size="xs" c="yellow.8">
          Позиция {issue.position + 1}: {issue.message}
        </Text>
      ))}
    </Stack>
  );
}

function PreviewResult({ preview }: { preview: TopicPreview }) {
  const fmt = useFormat();
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const toggle = (index: number) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (!next.delete(index)) {
        next.add(index);
      }
      return next;
    });

  return (
    <Stack gap="xs">
      <Text size="sm">
        За {fmt.date(preview.period_start)} — {fmt.date(preview.period_end)} уникальных статей:{' '}
        <b>
          {preview.unique_is_lower_bound ? 'не меньше ' : ''}
          {preview.unique_total}
        </b>
      </Text>
      <Table verticalSpacing={6} withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th w={36} />
            <Table.Th>Запрос</Table.Th>
            <Table.Th w={110}>Найдено</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {preview.queries.map((item, index) => {
            const isOpen = expanded.has(index);
            const background = item.error ? 'red.0' : item.total === 0 ? 'yellow.0' : undefined;
            return [
              <Table.Tr key={`row-${index}`} bg={background}>
                <Table.Td>
                  {item.examples.length > 0 ? (
                    <ActionIcon
                      variant="subtle"
                      color="gray"
                      size="sm"
                      aria-label={isOpen ? 'Скрыть примеры' : 'Показать примеры'}
                      onClick={() => toggle(index)}
                    >
                      {isOpen ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
                    </ActionIcon>
                  ) : null}
                </Table.Td>
                <Table.Td>
                  <Code fz={12} style={{ whiteSpace: 'pre-wrap' }}>
                    {item.query}
                  </Code>
                  {item.error ? (
                    <Text size="xs" c="red">
                      {item.error}
                    </Text>
                  ) : null}
                </Table.Td>
                <Table.Td>
                  {item.error ? (
                    <Badge color="red" variant="light">
                      ошибка
                    </Badge>
                  ) : item.total === 0 ? (
                    <Badge color="yellow" variant="light">
                      0
                    </Badge>
                  ) : (
                    <Text size="sm" fw={600}>
                      {item.total}
                    </Text>
                  )}
                </Table.Td>
              </Table.Tr>,
              isOpen ? (
                <Table.Tr key={`examples-${index}`}>
                  <Table.Td />
                  <Table.Td colSpan={2}>
                    <Stack gap={4}>
                      {item.examples.map((example) => (
                        <Text key={example.arxiv_id} size="xs">
                          <Text span c="dimmed" inherit>
                            {fmt.date(example.published_at)}
                          </Text>{' '}
                          <Anchor href={example.abs_url} target="_blank" rel="noreferrer" inherit>
                            {example.title}
                          </Anchor>
                        </Text>
                      ))}
                      {item.total > item.examples.length ? (
                        <Text size="xs" c="dimmed">
                          Показаны первые {item.examples.length} из {item.total}
                        </Text>
                      ) : null}
                    </Stack>
                  </Table.Td>
                </Table.Tr>
              ) : null,
            ];
          })}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}

function TopicForm({ topic, onClose }: { topic: Topic | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(topic?.name ?? '');
  const [description, setDescription] = useState(topic?.description ?? '');
  const [rows, setRows] = useState<QueryRow[]>(() =>
    topic?.queries.length ? topic.queries.map((query) => newRow(query)) : [newRow()],
  );
  const [categories, setCategories] = useState<string[]>(topic?.arxiv_categories ?? []);
  const [isActive, setIsActive] = useState(topic?.is_active ?? true);
  const [days, setDays] = useState(7);
  const [submitted, setSubmitted] = useState(false);

  // проверка на лету: запрос уходит на сервер, когда ввод остановился на 400 мс
  const [debouncedRows] = useDebouncedValue(rows, VALIDATION_DEBOUNCE_MS);
  const checks = useQueries({
    queries: debouncedRows.map((row) => ({
      queryKey: ['validate-query', row.value],
      queryFn: () => validateTopicQuery(row.value),
      enabled: row.value.trim() !== '',
      staleTime: Infinity,
      retry: false,
    })),
  });
  const debouncedById = new Map(debouncedRows.map((row, index) => [row.id, { row, index }]));

  const rowState = (row: QueryRow): RowState => {
    if (row.value.trim() === '') {
      return { kind: 'empty' };
    }
    const debounced = debouncedById.get(row.id);
    const result = debounced ? checks[debounced.index] : undefined;
    if (!debounced || debounced.row.value !== row.value || !result || result.isPending) {
      return { kind: 'pending' };
    }
    return result.data ? { kind: 'checked', check: result.data } : { kind: 'failed' };
  };
  const states = rows.map(rowState);

  const filled = rows.filter((row) => row.value.trim() !== '');
  const hasErrors = states.some((state) => state.kind === 'checked' && state.check.errors.length);
  const isChecking = states.some((state) => state.kind === 'pending');
  const checkFailed = states.some((state) => state.kind === 'failed');
  const invalidCategories = categories.filter((category) => !CATEGORY_RE.test(category));
  const nameError = submitted && name.trim() === '' ? 'Введите название' : undefined;
  const queriesReady = filled.length > 0 && !hasErrors && !isChecking && !checkFailed;
  const canSave = queriesReady && invalidCategories.length === 0;

  const updateRow = (id: string, value: string) =>
    setRows((current) => current.map((row) => (row.id === id ? { ...row, value } : row)));
  const duplicateRow = (source: QueryRow) =>
    setRows((current) =>
      current.flatMap((row) => (row.id === source.id ? [row, newRow(row.value)] : [row])),
    );
  const removeRow = (id: string) =>
    setRows((current) =>
      current.length > 1 ? current.filter((row) => row.id !== id) : [newRow()],
    );
  const onDragEnd = ({ source, destination }: DropResult) => {
    if (!destination || destination.index === source.index) {
      return;
    }
    setRows((current) => {
      const next = [...current];
      const [moved] = next.splice(source.index, 1);
      if (moved) {
        next.splice(destination.index, 0, moved);
      }
      return next;
    });
  };

  const save = useMutation({
    mutationFn: (values: TopicInput) =>
      topic ? updateTopic(topic.id, values) : createTopic(values),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['topics'] });
      notifySuccess(topic ? 'Тема обновлена' : 'Тема создана');
      onClose();
    },
  });
  const preview = useMutation({
    mutationFn: () => previewTopic({ queries: filled.map((row) => row.value), categories, days }),
  });

  const submit = () => {
    setSubmitted(true);
    if (name.trim() === '' || !canSave) {
      return;
    }
    save.mutate({
      name: name.trim(),
      description: description.trim() || null,
      queries: filled.map((row) => row.value),
      arxiv_categories: categories,
      is_active: isActive,
    });
  };

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <Stack>
        <TextInput
          label="Название"
          placeholder="Автономный железнодорожный транспорт"
          value={name}
          onChange={(event) => setName(event.currentTarget.value)}
          error={nameError}
        />
        <Textarea
          label="Описание"
          description="Необязательно. Печатается под названием раздела в дайджесте"
          autosize
          minRows={1}
          maxRows={4}
          value={description}
          onChange={(event) => setDescription(event.currentTarget.value)}
        />

        <SyntaxCheatSheet />

        <div>
          <Text size="sm" fw={500}>
            Поисковые запросы
          </Text>
          <Text size="xs" c="dimmed" mb="xs">
            Каждый запрос выполняется на arXiv отдельно; статья попадает в тему, если её нашёл любой
            из них. Порядок меняется перетаскиванием
          </Text>
          <DragDropContext onDragEnd={onDragEnd}>
            <Droppable droppableId="topic-queries">
              {(droppable) => (
                <div ref={droppable.innerRef} {...droppable.droppableProps}>
                  {rows.map((row, index) => {
                    const state = rowState(row);
                    const failed = state.kind === 'checked' && state.check.errors.length > 0;
                    return (
                      <Draggable key={row.id} draggableId={row.id} index={index}>
                        {(draggable) => (
                          <Box ref={draggable.innerRef} {...draggable.draggableProps} pb="xs">
                            <Group gap={6} align="flex-start" wrap="nowrap">
                              <ActionIcon
                                {...draggable.dragHandleProps}
                                variant="subtle"
                                color="gray"
                                mt={4}
                                aria-label="Перетащить запрос"
                              >
                                <IconGripVertical size={16} />
                              </ActionIcon>
                              <Box style={{ flex: 1, minWidth: 0 }}>
                                <Textarea
                                  autosize
                                  minRows={1}
                                  maxRows={6}
                                  styles={MONO}
                                  placeholder="(ti:dataset OR ti:benchmark) AND all:railway"
                                  aria-label={`Запрос ${index + 1}`}
                                  spellCheck={false}
                                  value={row.value}
                                  onChange={(event) => updateRow(row.id, event.currentTarget.value)}
                                  error={failed}
                                />
                                <Box mt={4}>
                                  <QueryFeedback value={row.value} state={state} />
                                </Box>
                              </Box>
                              <Tooltip label="Дублировать">
                                <ActionIcon
                                  variant="subtle"
                                  mt={4}
                                  aria-label="Дублировать запрос"
                                  onClick={() => duplicateRow(row)}
                                >
                                  <IconCopy size={16} />
                                </ActionIcon>
                              </Tooltip>
                              <Tooltip label="Удалить">
                                <ActionIcon
                                  variant="subtle"
                                  color="red"
                                  mt={4}
                                  aria-label="Удалить запрос"
                                  onClick={() => removeRow(row.id)}
                                >
                                  <IconTrash size={16} />
                                </ActionIcon>
                              </Tooltip>
                            </Group>
                          </Box>
                        )}
                      </Draggable>
                    );
                  })}
                  {droppable.placeholder}
                </div>
              )}
            </Droppable>
          </DragDropContext>
          <Button
            variant="light"
            size="xs"
            leftSection={<IconPlus size={14} />}
            onClick={() => setRows((current) => [...current, newRow()])}
          >
            Добавить запрос
          </Button>
          {submitted && filled.length === 0 ? (
            <Text size="xs" c="red" mt={4}>
              Добавьте хотя бы один запрос
            </Text>
          ) : null}
        </div>

        <TagsInput
          label="Категории arXiv"
          description="Необязательно. Добавляются к каждому запросу: статья должна быть в одной из них"
          placeholder="cs.RO"
          data={POPULAR_ARXIV_CATEGORIES}
          value={categories}
          onChange={setCategories}
          error={
            invalidCategories.length
              ? `Категория должна выглядеть как cs.CL: ${invalidCategories.join(', ')}`
              : undefined
          }
          clearable
        />
        <Switch
          label="Тема активна"
          checked={isActive}
          onChange={(event) => setIsActive(event.currentTarget.checked)}
        />

        <Group gap="sm" align="flex-end">
          <NumberInput
            label="Дней"
            min={1}
            max={30}
            allowDecimal={false}
            clampBehavior="strict"
            value={days}
            onChange={(value) => setDays(typeof value === 'number' ? value : 7)}
            w={90}
          />
          <Button
            variant="default"
            leftSection={<IconSearch size={16} />}
            loading={preview.isPending}
            disabled={!canSave}
            onClick={() => preview.mutate()}
          >
            Проверить за {days} дн.
          </Button>
          {preview.isPending ? (
            <Group gap={6}>
              <Loader size="xs" />
              <Text size="xs" c="dimmed">
                Запросы идут на arXiv по одному с паузами: около {filled.length * 3} с
              </Text>
            </Group>
          ) : null}
        </Group>
        {preview.data ? <PreviewResult preview={preview.data} /> : null}

        <Group justify="flex-end">
          {hasErrors ? (
            <Text size="sm" c="red">
              Исправьте ошибки в запросах
            </Text>
          ) : null}
          <Button variant="default" onClick={onClose}>
            Отмена
          </Button>
          <Button
            type="submit"
            loading={save.isPending}
            disabled={hasErrors || isChecking || checkFailed || invalidCategories.length > 0}
          >
            Сохранить
          </Button>
        </Group>
      </Stack>
    </form>
  );
}

interface TopicEditorProps {
  topic: Topic | null;
  opened: boolean;
  onClose: () => void;
}

export function TopicEditor({ topic, opened, onClose }: TopicEditorProps) {
  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={topic ? 'Редактирование темы' : 'Новая тема'}
      centered
      size="xl"
      // без transform: иначе перетаскиваемая строка смещается относительно курсора
      transitionProps={{ transition: 'fade' }}
    >
      {/* форма монтируется заново при каждом открытии, поэтому берёт начальные значения из темы */}
      {opened ? <TopicForm topic={topic} onClose={onClose} /> : null}
    </Modal>
  );
}
