import {
  ActionIcon,
  Badge,
  Button,
  Code,
  FileButton,
  Group,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { IconDownload, IconPencil, IconPlus, IconTrash, IconUpload } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { deleteTopic, exportTopicsYaml, listTopics, updateTopic } from '../api/endpoints';
import type { Topic } from '../api/types';
import {
  PageHeader,
  QueryState,
  TablePagination,
  confirmDelete,
  usePagination,
} from '../components/common';
import { TopicEditor } from '../components/TopicEditor';
import { TopicImportModal } from '../components/TopicImportModal';
import { notifySuccess } from '../lib/queryClient';

const MAX_QUERIES_IN_TOOLTIP = 12;

function QueriesCount({ queries }: { queries: string[] }) {
  return (
    <Tooltip
      multiline
      maw={520}
      label={
        <Stack gap={2}>
          {queries.slice(0, MAX_QUERIES_IN_TOOLTIP).map((query) => (
            <Code key={query} fz={11} bg="transparent" c="inherit">
              {query}
            </Code>
          ))}
          {queries.length > MAX_QUERIES_IN_TOOLTIP ? (
            <Text size="xs">и ещё {queries.length - MAX_QUERIES_IN_TOOLTIP}</Text>
          ) : null}
        </Stack>
      }
    >
      <Badge variant="light" color={queries.length ? 'blue' : 'red'}>
        {queries.length}
      </Badge>
    </Tooltip>
  );
}

export function TopicsPage() {
  const queryClient = useQueryClient();
  const pagination = usePagination();
  const [search, setSearch] = useState('');
  const [editing, setEditing] = useState<Topic | null>(null);
  const [editorOpened, setEditorOpened] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ['topics', pagination.page, search],
    queryFn: () => listTopics({ limit: pagination.limit, offset: pagination.offset, q: search }),
  });
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['topics'] });

  const toggle = useMutation({
    mutationFn: (topic: Topic) => updateTopic(topic.id, { is_active: !topic.is_active }),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (topic: Topic) => deleteTopic(topic.id),
    onSuccess: () => {
      invalidate();
      notifySuccess('Тема удалена');
    },
  });
  const exportYaml = useMutation({ mutationFn: exportTopicsYaml });

  const openEditor = (topic: Topic | null) => {
    setEditing(topic);
    setEditorOpened(true);
  };

  return (
    <>
      <PageHeader title="Темы">
        <TextInput
          placeholder="Поиск по названию"
          value={search}
          onChange={(event) => {
            setSearch(event.currentTarget.value);
            pagination.setPage(1);
          }}
        />
        <Button
          variant="default"
          leftSection={<IconDownload size={16} />}
          loading={exportYaml.isPending}
          onClick={() => exportYaml.mutate()}
        >
          Экспорт YAML
        </Button>
        {/* key сбрасывает кнопку, чтобы тот же файл можно было выбрать повторно */}
        <FileButton
          key={importFile ? 'selected' : 'idle'}
          accept=".yaml,.yml,application/x-yaml,text/yaml"
          onChange={setImportFile}
        >
          {(props) => (
            <Button variant="default" leftSection={<IconUpload size={16} />} {...props}>
              Импорт YAML
            </Button>
          )}
        </FileButton>
        <Button leftSection={<IconPlus size={16} />} onClick={() => openEditor(null)}>
          Добавить тему
        </Button>
      </PageHeader>

      <QueryState
        isLoading={isLoading}
        error={error}
        isEmpty={data?.items.length === 0}
        emptyText="Тем пока нет. Добавьте первую, чтобы сервис знал, какие статьи собирать"
      >
        <Table highlightOnHover verticalSpacing="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Название</Table.Th>
              <Table.Th>Запросов</Table.Th>
              <Table.Th>Категории arXiv</Table.Th>
              <Table.Th>Активна</Table.Th>
              <Table.Th w={90} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data?.items.map((topic) => (
              <Table.Tr key={topic.id}>
                <Table.Td maw={520}>
                  <Text size="sm" fw={500}>
                    {topic.name}
                  </Text>
                  {topic.description ? (
                    <Text size="xs" c="dimmed" lineClamp={2}>
                      {topic.description}
                    </Text>
                  ) : null}
                </Table.Td>
                <Table.Td>
                  <QueriesCount queries={topic.queries} />
                </Table.Td>
                <Table.Td>
                  {topic.arxiv_categories.length ? (
                    <Group gap={4}>
                      {topic.arxiv_categories.map((category) => (
                        <Badge key={category} variant="outline" color="gray" tt="none">
                          {category}
                        </Badge>
                      ))}
                    </Group>
                  ) : (
                    <Text size="sm" c="dimmed">
                      любые
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  <Switch
                    checked={topic.is_active}
                    onChange={() => toggle.mutate(topic)}
                    aria-label={`Тема ${topic.name} активна`}
                  />
                </Table.Td>
                <Table.Td>
                  <Group gap={4} justify="flex-end" wrap="nowrap">
                    <ActionIcon
                      variant="subtle"
                      aria-label="Редактировать"
                      onClick={() => openEditor(topic)}
                    >
                      <IconPencil size={16} />
                    </ActionIcon>
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      aria-label="Удалить"
                      onClick={() =>
                        confirmDelete(`тему «${topic.name}»`, () => remove.mutate(topic))
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

      <TopicEditor topic={editing} opened={editorOpened} onClose={() => setEditorOpened(false)} />
      <TopicImportModal file={importFile} onClose={() => setImportFile(null)} />
    </>
  );
}
