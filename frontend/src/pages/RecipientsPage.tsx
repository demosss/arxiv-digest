import {
  ActionIcon,
  Button,
  Group,
  Modal,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
} from '@mantine/core';
import { zodResolver } from '@hookform/resolvers/zod';
import { IconPencil, IconPlus, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { z } from 'zod';

import {
  createRecipient,
  deleteRecipient,
  listRecipients,
  updateRecipient,
} from '../api/endpoints';
import type { Recipient } from '../api/types';
import {
  PageHeader,
  QueryState,
  TablePagination,
  confirmDelete,
  usePagination,
} from '../components/common';
import { useFormat } from '../lib/format';
import { notifySuccess } from '../lib/queryClient';

const schema = z.object({
  email: z.string().trim().min(1, 'Введите email').email('Некорректный email'),
  name: z.string().trim().max(255),
  is_active: z.boolean(),
});

type RecipientForm = z.infer<typeof schema>;

const EMPTY: RecipientForm = { email: '', name: '', is_active: true };

function RecipientModal({
  recipient,
  opened,
  onClose,
}: {
  recipient: Recipient | null;
  opened: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const form = useForm<RecipientForm>({ resolver: zodResolver(schema), defaultValues: EMPTY });
  const { reset } = form;

  useEffect(() => {
    if (opened) {
      reset(
        recipient
          ? { email: recipient.email, name: recipient.name ?? '', is_active: recipient.is_active }
          : EMPTY,
      );
    }
  }, [opened, recipient, reset]);

  const mutation = useMutation({
    mutationFn: (values: RecipientForm) => {
      const payload = { ...values, name: values.name || null };
      return recipient ? updateRecipient(recipient.id, payload) : createRecipient(payload);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['recipients'] });
      notifySuccess(recipient ? 'Получатель обновлён' : 'Получатель добавлен');
      onClose();
    },
  });
  const { errors } = form.formState;

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={recipient ? 'Редактирование получателя' : 'Новый получатель'}
      centered
    >
      <form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}>
        <Stack>
          <TextInput
            label="Email"
            placeholder="reader@example.com"
            error={errors.email?.message}
            {...form.register('email')}
          />
          <TextInput
            label="Имя"
            description="Необязательно"
            error={errors.name?.message}
            {...form.register('name')}
          />
          <Controller
            control={form.control}
            name="is_active"
            render={({ field }) => (
              <Switch
                label="Получает дайджесты"
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

export function RecipientsPage() {
  const fmt = useFormat();
  const queryClient = useQueryClient();
  const pagination = usePagination();
  const [search, setSearch] = useState('');
  const [editing, setEditing] = useState<Recipient | null>(null);
  const [modalOpened, setModalOpened] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ['recipients', pagination.page, search],
    queryFn: () =>
      listRecipients({ limit: pagination.limit, offset: pagination.offset, q: search }),
  });
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['recipients'] });

  const toggle = useMutation({
    mutationFn: (recipient: Recipient) =>
      updateRecipient(recipient.id, { is_active: !recipient.is_active }),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (recipient: Recipient) => deleteRecipient(recipient.id),
    onSuccess: () => {
      invalidate();
      notifySuccess('Получатель удалён');
    },
  });

  const openModal = (recipient: Recipient | null) => {
    setEditing(recipient);
    setModalOpened(true);
  };

  return (
    <>
      <PageHeader title="Получатели">
        <TextInput
          placeholder="Поиск по email и имени"
          value={search}
          onChange={(event) => {
            setSearch(event.currentTarget.value);
            pagination.setPage(1);
          }}
        />
        <Button leftSection={<IconPlus size={16} />} onClick={() => openModal(null)}>
          Добавить получателя
        </Button>
      </PageHeader>

      <QueryState
        isLoading={isLoading}
        error={error}
        isEmpty={data?.items.length === 0}
        emptyText="Получателей пока нет — дайджест некому отправлять"
      >
        <Table highlightOnHover verticalSpacing="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Email</Table.Th>
              <Table.Th>Имя</Table.Th>
              <Table.Th>Добавлен</Table.Th>
              <Table.Th>Активен</Table.Th>
              <Table.Th w={90} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data?.items.map((recipient) => (
              <Table.Tr key={recipient.id}>
                <Table.Td fw={500}>{recipient.email}</Table.Td>
                <Table.Td>
                  {recipient.name ?? (
                    <Text span c="dimmed">
                      —
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>{fmt.date(recipient.created_at)}</Table.Td>
                <Table.Td>
                  <Switch
                    checked={recipient.is_active}
                    onChange={() => toggle.mutate(recipient)}
                    aria-label={`Получатель ${recipient.email} активен`}
                  />
                </Table.Td>
                <Table.Td>
                  <Group gap={4} justify="flex-end" wrap="nowrap">
                    <ActionIcon
                      variant="subtle"
                      aria-label="Редактировать"
                      onClick={() => openModal(recipient)}
                    >
                      <IconPencil size={16} />
                    </ActionIcon>
                    <ActionIcon
                      variant="subtle"
                      color="red"
                      aria-label="Удалить"
                      onClick={() =>
                        confirmDelete(`получателя ${recipient.email}`, () =>
                          remove.mutate(recipient),
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

      <RecipientModal
        recipient={editing}
        opened={modalOpened}
        onClose={() => setModalOpened(false)}
      />
    </>
  );
}
