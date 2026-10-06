import {
  ActionIcon,
  Badge,
  Button,
  Group,
  Modal,
  Stack,
  Switch,
  Table,
  TagsInput,
  Text,
  TextInput,
} from '@mantine/core';
import { zodResolver } from '@hookform/resolvers/zod';
import { IconPencil, IconPlus, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { z } from 'zod';

import { createTopic, deleteTopic, listTopics, updateTopic } from '../api/endpoints';
import type { Topic } from '../api/types';
import {
  PageHeader,
  QueryState,
  TablePagination,
  confirmDelete,
  usePagination,
} from '../components/common';
import { POPULAR_ARXIV_CATEGORIES } from '../lib/labels';
import { notifySuccess } from '../lib/queryClient';

const CATEGORY_RE = /^[a-z][a-z-]*(\.[A-Za-z][A-Za-z-]*)?$/;

const schema = z.object({
  name: z.string().trim().min(1, 'Введите название').max(255),
  queries: z.array(z.string()).min(1, 'Добавьте хотя бы один запрос'),
  arxiv_categories: z
    .array(z.string())
    .refine((items) => items.every((item) => CATEGORY_RE.test(item)), {
      message: 'Категория должна выглядеть как cs.CL',
    }),
  is_active: z.boolean(),
});

type TopicForm = z.infer<typeof schema>;

const EMPTY: TopicForm = { name: '', queries: [], arxiv_categories: [], is_active: true };

function TopicModal({
  topic,
  opened,
  onClose,
}: {
  topic: Topic | null;
  opened: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const form = useForm<TopicForm>({ resolver: zodResolver(schema), defaultValues: EMPTY });
  const { reset } = form;

  useEffect(() => {
    if (opened) {
      reset(
        topic
          ? {
              name: topic.name,
              queries: topic.queries,
              arxiv_categories: topic.arxiv_categories,
              is_active: topic.is_active,
            }
          : EMPTY,
      );
    }
  }, [opened, topic, reset]);

  const mutation = useMutation({
    mutationFn: (values: TopicForm) =>
      topic ? updateTopic(topic.id, values) : createTopic(values),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['topics'] });
      notifySuccess(topic ? 'Тема обновлена' : 'Тема создана');
      onClose();
    },
  });
  const { errors } = form.formState;

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={topic ? 'Редактирование темы' : 'Новая тема'}
      centered
      size="lg"
    >
      <form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}>
        <Stack>
          <TextInput
            label="Название"
            placeholder="Большие языковые модели"
            error={errors.name?.message}
            {...form.register('name')}
          />
          <Controller
            control={form.control}
            name="queries"
            render={({ field }) => (
              <TagsInput
                label="Поисковые запросы"
                description="Фраза или выражение arXiv API; Enter — добавить. Статья подходит под любой из запросов"
                placeholder="large language model"
                value={field.value}
                onChange={field.onChange}
                onBlur={field.onBlur}
                error={errors.queries?.message}
                clearable
              />
            )}
          />
          <Controller
            control={form.control}
            name="arxiv_categories"
            render={({ field }) => (
              <TagsInput
                label="Категории arXiv"
                description="Необязательно. Выберите из списка или введите свою, например cs.CL"
                placeholder="cs.CL"
                data={POPULAR_ARXIV_CATEGORIES}
                value={field.value}
                onChange={field.onChange}
                onBlur={field.onBlur}
                error={errors.arxiv_categories?.message}
                clearable
              />
            )}
          />
          <Controller
            control={form.control}
            name="is_active"
            render={({ field }) => (
              <Switch
                label="Тема активна"
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

export function TopicsPage() {
  const queryClient = useQueryClient();
  const pagination = usePagination();
  const [search, setSearch] = useState('');
  const [editing, setEditing] = useState<Topic | null>(null);
  const [modalOpened, setModalOpened] = useState(false);

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

  const openModal = (topic: Topic | null) => {
    setEditing(topic);
    setModalOpened(true);
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
        <Button leftSection={<IconPlus size={16} />} onClick={() => openModal(null)}>
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
              <Table.Th>Запросы</Table.Th>
              <Table.Th>Категории arXiv</Table.Th>
              <Table.Th>Активна</Table.Th>
              <Table.Th w={90} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data?.items.map((topic) => (
              <Table.Tr key={topic.id}>
                <Table.Td fw={500}>{topic.name}</Table.Td>
                <Table.Td>
                  <Group gap={4}>
                    {topic.queries.map((keyword) => (
                      <Badge key={keyword} variant="light" tt="none">
                        {keyword}
                      </Badge>
                    ))}
                  </Group>
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
                      onClick={() => openModal(topic)}
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

      <TopicModal topic={editing} opened={modalOpened} onClose={() => setModalOpened(false)} />
    </>
  );
}
