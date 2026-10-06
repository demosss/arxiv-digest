import {
  Alert,
  Badge,
  Button,
  Card,
  Group,
  NumberInput,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { zodResolver } from '@hookform/resolvers/zod';
import { IconSend } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { z } from 'zod';

import { getSmtp, sendSmtpTest, updateSmtp } from '../api/endpoints';
import { PageHeader, QueryState } from '../components/common';
import { useFormat } from '../lib/format';
import { SMTP_SECURITY_OPTIONS } from '../lib/labels';
import { notifySuccess } from '../lib/queryClient';

const schema = z.object({
  host: z.string().trim().min(1, 'Укажите SMTP-сервер').max(255),
  port: z.number({ invalid_type_error: 'Укажите порт' }).int().min(1).max(65535),
  username: z.string().trim().max(255),
  from_email: z.string().trim().min(1, 'Укажите адрес отправителя').email('Некорректный email'),
  from_name: z.string().trim().max(255),
  security: z.enum(['none', 'starttls', 'ssl']),
  timeout_seconds: z.number({ invalid_type_error: 'Укажите таймаут' }).int().min(1).max(300),
});

type SmtpForm = z.infer<typeof schema>;

const emailSchema = z.string().trim().email();

export function SmtpPage() {
  const fmt = useFormat();
  const queryClient = useQueryClient();
  const [testEmail, setTestEmail] = useState('');
  const { data, isLoading, error } = useQuery({ queryKey: ['smtp'], queryFn: getSmtp });

  const form = useForm<SmtpForm>({
    resolver: zodResolver(schema),
    defaultValues: {
      host: '',
      port: 587,
      username: '',
      from_email: '',
      from_name: '',
      security: 'starttls',
      timeout_seconds: 30,
    },
  });
  const { reset } = form;

  useEffect(() => {
    if (data) {
      reset({
        host: data.host,
        port: data.port,
        username: data.username,
        from_email: data.from_email,
        from_name: data.from_name,
        security: data.security,
        timeout_seconds: data.timeout_seconds,
      });
    }
  }, [data, reset]);

  const save = useMutation({
    mutationFn: updateSmtp,
    onSuccess: (saved) => {
      queryClient.setQueryData(['smtp'], saved);
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] });
      notifySuccess('Настройки SMTP сохранены');
    },
  });
  const test = useMutation({
    mutationFn: sendSmtpTest,
    onSuccess: (result) => notifySuccess(`Тестовое письмо отправлено на ${result.to}`),
  });

  const { errors, isDirty } = form.formState;
  const testEmailValid = emailSchema.safeParse(testEmail).success;

  return (
    <>
      <PageHeader title="Настройки SMTP" />
      <QueryState isLoading={isLoading} error={error}>
        <Stack gap="lg" maw={760}>
          <Card withBorder padding="lg">
            <Group justify="space-between" mb="md">
              <Title order={5}>Параметры сервера</Title>
              <Group gap="xs">
                <Text size="sm">Пароль задан в .env:</Text>
                <Badge color={data?.password_set ? 'green' : 'red'} variant="filled">
                  {data?.password_set ? 'да' : 'нет'}
                </Badge>
              </Group>
            </Group>
            <Alert color="gray" variant="light" mb="md">
              Пароль SMTP здесь не вводится и не хранится в базе: он берётся только из переменной
              окружения SMTP_PASSWORD. Чтобы изменить его, поправьте .env и перезапустите сервисы.
            </Alert>
            <form onSubmit={form.handleSubmit((values) => save.mutate(values))}>
              <Stack>
                <SimpleGrid cols={{ base: 1, sm: 2 }}>
                  <TextInput
                    label="SMTP-сервер"
                    placeholder="smtp.example.com"
                    error={errors.host?.message}
                    {...form.register('host')}
                  />
                  <Controller
                    control={form.control}
                    name="port"
                    render={({ field }) => (
                      <NumberInput
                        label="Порт"
                        min={1}
                        max={65535}
                        allowDecimal={false}
                        value={field.value}
                        onChange={(value) =>
                          field.onChange(typeof value === 'number' ? value : NaN)
                        }
                        error={errors.port?.message}
                      />
                    )}
                  />
                  <Controller
                    control={form.control}
                    name="security"
                    render={({ field }) => (
                      <Select
                        label="Шифрование"
                        data={SMTP_SECURITY_OPTIONS}
                        value={field.value}
                        onChange={(value) => value && field.onChange(value)}
                        allowDeselect={false}
                      />
                    )}
                  />
                  <Controller
                    control={form.control}
                    name="timeout_seconds"
                    render={({ field }) => (
                      <NumberInput
                        label="Таймаут, секунд"
                        min={1}
                        max={300}
                        allowDecimal={false}
                        value={field.value}
                        onChange={(value) =>
                          field.onChange(typeof value === 'number' ? value : NaN)
                        }
                        error={errors.timeout_seconds?.message}
                      />
                    )}
                  />
                  <TextInput
                    label="Логин"
                    description="Оставьте пустым, если сервер не требует авторизации"
                    autoComplete="off"
                    error={errors.username?.message}
                    {...form.register('username')}
                  />
                  <div />
                  <TextInput
                    label="Адрес отправителя"
                    placeholder="digest@example.com"
                    error={errors.from_email?.message}
                    {...form.register('from_email')}
                  />
                  <TextInput
                    label="Имя отправителя"
                    placeholder="arXiv Digest"
                    error={errors.from_name?.message}
                    {...form.register('from_name')}
                  />
                </SimpleGrid>
                <Group justify="space-between">
                  <Text size="xs" c="dimmed">
                    {data ? `Изменено: ${fmt.dateTime(data.updated_at)}` : ''}
                  </Text>
                  <Button type="submit" loading={save.isPending} disabled={!isDirty}>
                    Сохранить
                  </Button>
                </Group>
              </Stack>
            </form>
          </Card>

          <Card withBorder padding="lg">
            <Title order={5} mb="xs">
              Проверка
            </Title>
            <Text size="sm" c="dimmed" mb="sm">
              Письмо уйдёт с сохранёнными настройками
              {isDirty ? ' — сначала сохраните изменения' : ''}.
            </Text>
            <Group align="flex-end">
              <TextInput
                label="Адрес для тестового письма"
                placeholder="you@example.com"
                value={testEmail}
                onChange={(event) => setTestEmail(event.currentTarget.value)}
                style={{ flex: 1 }}
              />
              <Button
                variant="light"
                leftSection={<IconSend size={16} />}
                loading={test.isPending}
                disabled={!testEmailValid || isDirty || !data?.is_configured}
                onClick={() => test.mutate(testEmail.trim())}
              >
                Отправить тестовое письмо
              </Button>
            </Group>
          </Card>
        </Stack>
      </QueryState>
    </>
  );
}
