import {
  Alert,
  Button,
  Center,
  Paper,
  PasswordInput,
  Stack,
  TextInput,
  Title,
} from '@mantine/core';
import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation } from '@tanstack/react-query';
import { useForm } from 'react-hook-form';
import { Navigate, useLocation } from 'react-router-dom';
import { z } from 'zod';

import { getErrorMessage } from '../api/client';
import { useAuth } from '../auth/AuthContext';

const schema = z.object({
  username: z.string().trim().min(1, 'Введите имя пользователя'),
  password: z.string().min(1, 'Введите пароль'),
});

type LoginForm = z.infer<typeof schema>;

export function LoginPage() {
  const { isAuthenticated, login } = useAuth();
  const location = useLocation();
  const form = useForm<LoginForm>({
    resolver: zodResolver(schema),
    defaultValues: { username: '', password: '' },
  });
  const mutation = useMutation({
    mutationFn: (values: LoginForm) => login(values.username, values.password),
    // ошибка входа показывается в самой форме
    meta: { silent: true },
  });

  if (isAuthenticated) {
    const from = (location.state as { from?: string } | null)?.from ?? '/';
    return <Navigate to={from} replace />;
  }

  const { errors } = form.formState;
  return (
    <Center mih="100vh" bg="gray.1" p="md">
      <Paper withBorder shadow="sm" p="xl" radius="md" w={380}>
        <form onSubmit={form.handleSubmit((values) => mutation.mutate(values))}>
          <Stack>
            <Title order={3} ta="center">
              arXiv Digest
            </Title>
            {mutation.isError ? (
              <Alert color="red" variant="light">
                {getErrorMessage(mutation.error)}
              </Alert>
            ) : null}
            <TextInput
              label="Имя пользователя"
              autoComplete="username"
              autoFocus
              error={errors.username?.message}
              {...form.register('username')}
            />
            <PasswordInput
              label="Пароль"
              autoComplete="current-password"
              error={errors.password?.message}
              {...form.register('password')}
            />
            <Button type="submit" loading={mutation.isPending} fullWidth>
              Войти
            </Button>
          </Stack>
        </form>
      </Paper>
    </Center>
  );
}
