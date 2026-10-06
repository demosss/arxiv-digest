import {
  AppShell,
  Burger,
  Button,
  Group,
  Menu,
  Modal,
  NavLink,
  PasswordInput,
  Stack,
  Text,
  Title,
} from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import { zodResolver } from '@hookform/resolvers/zod';
import {
  IconClock,
  IconFileText,
  IconLayoutDashboard,
  IconLogout,
  IconMail,
  IconNews,
  IconPassword,
  IconPlayerPlay,
  IconTags,
  IconUser,
  IconUsers,
} from '@tabler/icons-react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useForm } from 'react-hook-form';
import { NavLink as RouterNavLink, Outlet, useLocation } from 'react-router-dom';
import { z } from 'zod';

import { changePassword, getMe } from '../api/endpoints';
import { useAuth } from '../auth/AuthContext';
import { useFormat } from '../lib/format';
import { notifySuccess } from '../lib/queryClient';

const NAV_ITEMS = [
  { to: '/', label: 'Дашборд', icon: IconLayoutDashboard },
  { to: '/topics', label: 'Темы', icon: IconTags },
  { to: '/recipients', label: 'Получатели', icon: IconUsers },
  { to: '/schedules', label: 'Расписания', icon: IconClock },
  { to: '/smtp', label: 'Настройки SMTP', icon: IconMail },
  { to: '/runs', label: 'Запуски', icon: IconPlayerPlay },
  { to: '/papers', label: 'Статьи', icon: IconFileText },
  { to: '/digests', label: 'Дайджесты', icon: IconNews },
];

const passwordSchema = z
  .object({
    current_password: z.string().min(1, 'Введите текущий пароль'),
    new_password: z.string().min(8, 'Не короче 8 символов'),
    confirm: z.string(),
  })
  .refine((value) => value.new_password === value.confirm, {
    path: ['confirm'],
    message: 'Пароли не совпадают',
  });

type PasswordForm = z.infer<typeof passwordSchema>;

function ChangePasswordModal({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const form = useForm<PasswordForm>({
    resolver: zodResolver(passwordSchema),
    defaultValues: { current_password: '', new_password: '', confirm: '' },
  });
  const mutation = useMutation({
    mutationFn: (values: PasswordForm) =>
      changePassword(values.current_password, values.new_password),
    onSuccess: () => {
      notifySuccess('Пароль изменён');
      form.reset();
      onClose();
    },
  });
  const { errors } = form.formState;

  return (
    <Modal opened={opened} onClose={onClose} title="Смена пароля" centered>
      <form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}>
        <Stack>
          <PasswordInput
            label="Текущий пароль"
            autoComplete="current-password"
            error={errors.current_password?.message}
            {...form.register('current_password')}
          />
          <PasswordInput
            label="Новый пароль"
            autoComplete="new-password"
            error={errors.new_password?.message}
            {...form.register('new_password')}
          />
          <PasswordInput
            label="Новый пароль ещё раз"
            autoComplete="new-password"
            error={errors.confirm?.message}
            {...form.register('confirm')}
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              Отмена
            </Button>
            <Button type="submit" loading={mutation.isPending}>
              Сменить пароль
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}

export function AppLayout() {
  const [navOpened, nav] = useDisclosure(false);
  const [passwordOpened, password] = useDisclosure(false);
  const { logout } = useAuth();
  const { timezone } = useFormat();
  const location = useLocation();
  const { data: user } = useQuery({ queryKey: ['me'], queryFn: getMe, staleTime: Infinity });

  return (
    <AppShell
      header={{ height: 56 }}
      navbar={{ width: 240, breakpoint: 'sm', collapsed: { mobile: !navOpened } }}
      padding="md"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="sm">
            <Burger opened={navOpened} onClick={nav.toggle} hiddenFrom="sm" size="sm" />
            <Title order={4}>arXiv Digest</Title>
          </Group>
          <Group gap="md">
            <Text size="xs" c="dimmed" visibleFrom="sm">
              Время: {timezone}
            </Text>
            <Menu position="bottom-end" withinPortal>
              <Menu.Target>
                <Button variant="subtle" leftSection={<IconUser size={16} />}>
                  {user?.username ?? '…'}
                </Button>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Item leftSection={<IconPassword size={16} />} onClick={password.open}>
                  Сменить пароль
                </Menu.Item>
                <Menu.Item color="red" leftSection={<IconLogout size={16} />} onClick={logout}>
                  Выйти
                </Menu.Item>
              </Menu.Dropdown>
            </Menu>
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="xs">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            component={RouterNavLink}
            to={item.to}
            label={item.label}
            leftSection={<item.icon size={18} stroke={1.6} />}
            active={
              item.to === '/' ? location.pathname === '/' : location.pathname.startsWith(item.to)
            }
            onClick={nav.close}
          />
        ))}
      </AppShell.Navbar>

      <AppShell.Main>
        <Outlet />
      </AppShell.Main>

      <ChangePasswordModal opened={passwordOpened} onClose={password.close} />
    </AppShell>
  );
}
