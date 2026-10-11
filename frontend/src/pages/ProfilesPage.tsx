import {
  ActionIcon,
  Badge,
  Button,
  Group,
  Modal,
  Stack,
  Table,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { zodResolver } from '@hookform/resolvers/zod';
import { IconPencil, IconPlus, IconTrash } from '@tabler/icons-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { z } from 'zod';

import { createProfile, deleteProfile, updateProfile } from '../api/endpoints';
import type { Profile } from '../api/types';
import { PageHeader, confirmDelete } from '../components/common';
import { useFormat } from '../lib/format';
import { notifySuccess } from '../lib/queryClient';
import { useProfile } from '../profile/ProfileContext';

const schema = z.object({
  name: z.string().trim().min(1, 'Введите название').max(255, 'Не длиннее 255 символов'),
});

type ProfileForm = z.infer<typeof schema>;

function ProfileModal({
  profile,
  opened,
  onClose,
}: {
  profile: Profile | null;
  opened: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const form = useForm<ProfileForm>({
    resolver: zodResolver(schema),
    defaultValues: { name: '' },
  });
  const { reset } = form;

  useEffect(() => {
    if (opened) {
      reset({ name: profile?.name ?? '' });
    }
  }, [opened, profile, reset]);

  const mutation = useMutation({
    mutationFn: (values: ProfileForm) =>
      profile ? updateProfile(profile.id, values) : createProfile(values),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['profiles'] });
      notifySuccess(profile ? 'Профиль переименован' : 'Профиль создан');
      onClose();
    },
  });

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={profile ? 'Переименование профиля' : 'Новый профиль'}
      centered
    >
      <form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}>
        <Stack>
          <TextInput
            label="Название"
            description="Попадает в тему письма с дайджестом"
            placeholder="Например, Беспилотный транспорт"
            data-autofocus
            error={form.formState.errors.name?.message}
            {...form.register('name')}
          />
          {profile ? null : (
            <Text size="xs" c="dimmed">
              Новый профиль пуст: темы, получателей и расписания нужно настроить в нём отдельно.
              Настройки SMTP и LLM общие.
            </Text>
          )}
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

/** Почему профиль нельзя удалить; null — можно. */
function deleteBlocker(profile: Profile, isCurrent: boolean, total: number): string | null {
  if (total === 1) {
    return 'Единственный профиль удалить нельзя';
  }
  if (isCurrent) {
    return 'Это текущий профиль: сначала переключитесь на другой';
  }
  const hasData = Object.values(profile.counts).some((count) => count > 0);
  return hasData
    ? 'В профиле есть данные: сначала удалите его темы, получателей, расписания, статьи и дайджесты'
    : null;
}

export function ProfilesPage() {
  const fmt = useFormat();
  const queryClient = useQueryClient();
  const { profiles, current, select } = useProfile();
  const [editing, setEditing] = useState<Profile | null>(null);
  const [modalOpened, setModalOpened] = useState(false);

  // счётчики данных меняются на других страницах: при открытии раздела список перечитывается
  useEffect(() => {
    void queryClient.invalidateQueries({ queryKey: ['profiles'] });
  }, [queryClient]);

  const remove = useMutation({
    mutationFn: (profile: Profile) => deleteProfile(profile.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['profiles'] });
      notifySuccess('Профиль удалён');
    },
  });

  const openModal = (profile: Profile | null) => {
    setEditing(profile);
    setModalOpened(true);
  };

  return (
    <>
      <PageHeader title="Профили">
        <Button leftSection={<IconPlus size={16} />} onClick={() => openModal(null)}>
          Создать профиль
        </Button>
      </PageHeader>
      <Text size="sm" c="dimmed" mb="md">
        У каждого профиля свои темы, получатели, расписания, запуски, статьи, дайджесты и переводы.
        Настройки SMTP и LLM общие. Удалить можно только пустой профиль.
      </Text>

      <Table highlightOnHover verticalSpacing="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Название</Table.Th>
            <Table.Th ta="right">Темы</Table.Th>
            <Table.Th ta="right">Получатели</Table.Th>
            <Table.Th ta="right">Расписания</Table.Th>
            <Table.Th ta="right">Статьи</Table.Th>
            <Table.Th ta="right">Дайджесты</Table.Th>
            <Table.Th>Создан</Table.Th>
            <Table.Th w={190} />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {profiles.map((profile) => {
            const isCurrent = profile.id === current.id;
            const blocker = deleteBlocker(profile, isCurrent, profiles.length);
            return (
              <Table.Tr key={profile.id}>
                <Table.Td fw={500}>
                  <Group gap="xs" wrap="nowrap">
                    {profile.name}
                    {isCurrent ? (
                      <Badge size="sm" variant="light">
                        текущий
                      </Badge>
                    ) : null}
                  </Group>
                </Table.Td>
                <Table.Td ta="right">{profile.counts.topics}</Table.Td>
                <Table.Td ta="right">{profile.counts.recipients}</Table.Td>
                <Table.Td ta="right">{profile.counts.schedules}</Table.Td>
                <Table.Td ta="right">{profile.counts.papers}</Table.Td>
                <Table.Td ta="right">{profile.counts.digests}</Table.Td>
                <Table.Td>{fmt.date(profile.created_at)}</Table.Td>
                <Table.Td>
                  <Group gap={4} justify="flex-end" wrap="nowrap">
                    {isCurrent ? null : (
                      <Button size="compact-sm" variant="subtle" onClick={() => select(profile.id)}>
                        Выбрать
                      </Button>
                    )}
                    <ActionIcon
                      variant="subtle"
                      aria-label="Переименовать"
                      onClick={() => openModal(profile)}
                    >
                      <IconPencil size={16} />
                    </ActionIcon>
                    <Tooltip label={blocker} disabled={blocker === null} multiline maw={280}>
                      <ActionIcon
                        variant="subtle"
                        color="red"
                        aria-label="Удалить"
                        disabled={blocker !== null}
                        onClick={() =>
                          confirmDelete(`профиль «${profile.name}»`, () => remove.mutate(profile))
                        }
                      >
                        <IconTrash size={16} />
                      </ActionIcon>
                    </Tooltip>
                  </Group>
                </Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>

      <ProfileModal profile={editing} opened={modalOpened} onClose={() => setModalOpened(false)} />
    </>
  );
}
