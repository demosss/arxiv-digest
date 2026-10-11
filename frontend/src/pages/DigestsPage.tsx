import {
  ActionIcon,
  Alert,
  Anchor,
  Button,
  Divider,
  Drawer,
  Group,
  Loader,
  Menu,
  Select,
  Stack,
  Table,
  Text,
  Tooltip,
} from '@mantine/core';
import { IconChevronDown, IconDownload, IconEye, IconSend, IconTrash } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import {
  clearDigests,
  deleteDigest,
  downloadDigestPdf,
  getDigest,
  listDigests,
  resendDigest,
} from '../api/endpoints';
import type { Digest, DigestStatus } from '../api/types';
import { DeliveryStatusBadge, DigestStatusBadge } from '../components/Badges';
import {
  PageHeader,
  QueryState,
  TablePagination,
  confirmDelete,
  usePagination,
} from '../components/common';
import { formatPeriod } from '../lib/dates';
import { useFormat } from '../lib/format';
import { DIGEST_CONTENT_LABELS, DIGEST_STATUS } from '../lib/labels';
import { notifySuccess } from '../lib/queryClient';

const STATUS_OPTIONS = (Object.keys(DIGEST_STATUS) as DigestStatus[]).map((value) => ({
  value,
  label: DIGEST_STATUS[value].label,
}));

function useDigestActions() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const download = useMutation({ mutationFn: downloadDigestPdf });
  const resend = useMutation({
    mutationFn: ({ digest, force }: { digest: Digest; force: boolean }) =>
      resendDigest(digest.id, force),
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ['digests'] });
      void queryClient.invalidateQueries({ queryKey: ['runs'] });
      notifySuccess(`Рассылка поставлена в очередь: запуск №${run.id}`);
      navigate(`/runs/${run.id}`);
    },
  });
  return { download, resend };
}

function ResendButton({ digest, compact }: { digest: Digest; compact?: boolean }) {
  const { resend } = useDigestActions();
  return (
    <Menu position="bottom-end" withinPortal>
      <Menu.Target>
        {compact ? (
          <Tooltip label="Отправить повторно">
            <ActionIcon variant="subtle" aria-label="Отправить повторно" loading={resend.isPending}>
              <IconSend size={16} />
            </ActionIcon>
          </Tooltip>
        ) : (
          <Button
            variant="light"
            leftSection={<IconSend size={16} />}
            rightSection={<IconChevronDown size={14} />}
            loading={resend.isPending}
            disabled={!digest.has_pdf}
          >
            Отправить повторно
          </Button>
        )}
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Item onClick={() => resend.mutate({ digest, force: true })}>
          Всем активным получателям
        </Menu.Item>
        <Menu.Item onClick={() => resend.mutate({ digest, force: false })}>
          Только тем, кому не доставлено
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );
}

function DigestDrawer({ digestId, onClose }: { digestId: number | null; onClose: () => void }) {
  const fmt = useFormat();
  const { download } = useDigestActions();
  const { data: digest, isLoading } = useQuery({
    queryKey: ['digests', 'detail', digestId],
    queryFn: () => getDigest(digestId as number),
    enabled: digestId !== null,
    refetchInterval: 5000,
  });

  return (
    <Drawer
      opened={digestId !== null}
      onClose={onClose}
      position="right"
      size="xl"
      title={
        digest ? `Дайджест за ${formatPeriod(digest.period_start, digest.period_end)}` : 'Дайджест'
      }
    >
      {isLoading ? <Loader /> : null}
      {digest ? (
        <Stack gap="md">
          <Group gap="sm">
            <DigestStatusBadge status={digest.status} size="lg" />
            <Text size="sm" c="dimmed">
              Статей: {digest.papers_count} · {DIGEST_CONTENT_LABELS[digest.content].toLowerCase()}{' '}
              · обновлён {fmt.dateTime(digest.updated_at)}
            </Text>
          </Group>
          <Group gap="sm">
            <Button
              leftSection={<IconDownload size={16} />}
              loading={download.isPending}
              disabled={!digest.has_pdf}
              onClick={() => download.mutate(digest)}
            >
              Скачать PDF
            </Button>
            <ResendButton digest={digest} />
          </Group>
          {digest.error ? (
            <Alert color="red" title="Ошибка сборки">
              {digest.error}
            </Alert>
          ) : null}
          {!digest.has_pdf ? (
            <Alert color="yellow">
              PDF-файл не найден. Соберите дайджест заново в разделе «Запуски».
            </Alert>
          ) : null}

          <Divider label="Доставка" labelPosition="left" />
          {digest.deliveries.length === 0 ? (
            <Text size="sm" c="dimmed">
              Этот дайджест ещё не рассылался
            </Text>
          ) : (
            <Table verticalSpacing="xs">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Получатель</Table.Th>
                  <Table.Th>Статус</Table.Th>
                  <Table.Th>Попыток</Table.Th>
                  <Table.Th>Отправлено</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {digest.deliveries.map((delivery) => (
                  <Table.Tr key={delivery.id}>
                    <Table.Td>
                      {delivery.email}
                      {delivery.error ? (
                        <Text size="xs" c="red">
                          {delivery.error}
                        </Text>
                      ) : null}
                    </Table.Td>
                    <Table.Td>
                      <DeliveryStatusBadge status={delivery.status} />
                    </Table.Td>
                    <Table.Td>{delivery.attempts}</Table.Td>
                    <Table.Td>{fmt.dateTime(delivery.sent_at)}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}

          <Divider label="Состав" labelPosition="left" />
          {digest.papers.length === 0 ? (
            <Text size="sm" c="dimmed">
              В дайджесте нет статей
            </Text>
          ) : (
            <Table verticalSpacing="xs">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th w={40}>№</Table.Th>
                  <Table.Th>Статья</Table.Th>
                  <Table.Th>Тема</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {digest.papers.map((paper) => (
                  <Table.Tr key={paper.paper_id}>
                    <Table.Td>{paper.position}</Table.Td>
                    <Table.Td>
                      <Anchor href={paper.abs_url} target="_blank" rel="noreferrer" size="sm">
                        {paper.title}
                      </Anchor>
                      <Text size="xs" c="dimmed">
                        arXiv:{paper.arxiv_id}
                      </Text>
                    </Table.Td>
                    <Table.Td>{paper.topic_name ?? '—'}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
        </Stack>
      ) : null}
    </Drawer>
  );
}

export function DigestsPage() {
  const fmt = useFormat();
  const pagination = usePagination();
  const [status, setStatus] = useState<DigestStatus | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const { download } = useDigestActions();
  const queryClient = useQueryClient();

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['digests'] });
    void queryClient.invalidateQueries({ queryKey: ['dashboard'] });
  };
  const remove = useMutation({
    mutationFn: (digest: Digest) => deleteDigest(digest.id),
    onSuccess: (_, digest) => {
      if (selected === digest.id) setSelected(null);
      invalidate();
      notifySuccess('Дайджест удалён');
    },
  });
  const clear = useMutation({
    mutationFn: () => clearDigests({ status: status ?? undefined }),
    onSuccess: ({ deleted }) => {
      setSelected(null);
      pagination.setPage(1);
      invalidate();
      notifySuccess(`Удалено дайджестов: ${deleted}`);
    },
  });

  const { data, isLoading, error } = useQuery({
    queryKey: ['digests', 'list', pagination.page, status],
    queryFn: () =>
      listDigests({
        limit: pagination.limit,
        offset: pagination.offset,
        status: status ?? undefined,
      }),
    refetchInterval: 15_000,
  });

  return (
    <>
      <PageHeader title="Дайджесты">
        <Select
          placeholder="Все статусы"
          data={STATUS_OPTIONS}
          value={status}
          onChange={(value) => {
            setStatus(value as DigestStatus | null);
            pagination.setPage(1);
          }}
          clearable
          w={220}
          aria-label="Статус дайджеста"
        />
        <Button
          color="red"
          variant="light"
          leftSection={<IconTrash size={16} />}
          loading={clear.isPending}
          disabled={!data?.total}
          onClick={() =>
            confirmDelete(
              `${status ? 'дайджесты с выбранным статусом' : 'все дайджесты'} (${data?.total ?? 0}) вместе с PDF-файлами и историей доставки`,
              () => clear.mutate(),
            )
          }
        >
          Очистить
        </Button>
      </PageHeader>

      <QueryState
        isLoading={isLoading}
        error={error}
        isEmpty={data?.items.length === 0}
        emptyText="Дайджестов пока нет: запустите пайплайн или шаг «Сборка дайджеста»"
      >
        <Table highlightOnHover verticalSpacing="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Период (UTC)</Table.Th>
              <Table.Th>Статей</Table.Th>
              <Table.Th>Состав карточек</Table.Th>
              <Table.Th>Статус</Table.Th>
              <Table.Th>Обновлён</Table.Th>
              <Table.Th w={160} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data?.items.map((digest) => (
              <Table.Tr key={digest.id}>
                <Table.Td>
                  <Anchor
                    component="button"
                    type="button"
                    fw={600}
                    onClick={() => setSelected(digest.id)}
                  >
                    {formatPeriod(digest.period_start, digest.period_end)}
                  </Anchor>
                </Table.Td>
                <Table.Td>{digest.papers_count}</Table.Td>
                <Table.Td>{DIGEST_CONTENT_LABELS[digest.content]}</Table.Td>
                <Table.Td>
                  <DigestStatusBadge status={digest.status} />
                </Table.Td>
                <Table.Td>{fmt.dateTime(digest.updated_at)}</Table.Td>
                <Table.Td>
                  <Group gap={4} justify="flex-end" wrap="nowrap">
                    <Tooltip label="Состав и доставка">
                      <ActionIcon
                        variant="subtle"
                        aria-label="Состав и доставка"
                        onClick={() => setSelected(digest.id)}
                      >
                        <IconEye size={16} />
                      </ActionIcon>
                    </Tooltip>
                    <Tooltip label={digest.has_pdf ? 'Скачать PDF' : 'PDF-файл не найден'}>
                      <ActionIcon
                        variant="subtle"
                        aria-label="Скачать PDF"
                        disabled={!digest.has_pdf}
                        loading={download.isPending && download.variables?.id === digest.id}
                        onClick={() => download.mutate(digest)}
                      >
                        <IconDownload size={16} />
                      </ActionIcon>
                    </Tooltip>
                    {digest.has_pdf ? <ResendButton digest={digest} compact /> : null}
                    <Tooltip label="Удалить">
                      <ActionIcon
                        variant="subtle"
                        color="red"
                        aria-label="Удалить"
                        onClick={() =>
                          confirmDelete(
                            `дайджест за ${formatPeriod(digest.period_start, digest.period_end)}`,
                            () => remove.mutate(digest),
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
      </QueryState>

      <DigestDrawer digestId={selected} onClose={() => setSelected(null)} />
    </>
  );
}
