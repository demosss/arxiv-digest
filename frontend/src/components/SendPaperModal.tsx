import { Alert, Anchor, Button, Checkbox, Group, Loader, Modal, Stack, Text } from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';

import { getPaper, listRecipients, sendPaper } from '../api/endpoints';
import type { PaperDetail, Recipient } from '../api/types';
import { notifySuccess } from '../lib/queryClient';

const RECIPIENTS_KEY = 'arxiv-digest.send-recipients';

/** Что отметить при открытии окна; без подсказки отмечаются все доступные документы. */
export interface SendPreset {
  pdf: boolean;
  translationIds: number[];
}

interface Props {
  paperId: number;
  opened: boolean;
  onClose: () => void;
  preset?: SendPreset;
}

function storedRecipients(): number[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(RECIPIENTS_KEY) ?? '[]');
    return Array.isArray(value) ? value.filter((id): id is number => typeof id === 'number') : [];
  } catch {
    return [];
  }
}

function SendForm({
  paper,
  recipients,
  preset,
  onClose,
}: {
  paper: PaperDetail;
  recipients: Recipient[];
  preset?: SendPreset;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const translations = paper.translations.filter((translation) => translation.has_file);
  const [withPdf, setWithPdf] = useState(paper.has_pdf && (preset ? preset.pdf : true));
  const [translationIds, setTranslationIds] = useState<number[]>(() =>
    translations
      .filter((translation) => !preset || preset.translationIds.includes(translation.id))
      .map((translation) => translation.id),
  );
  // по умолчанию — получатели, отмеченные в прошлый раз (кроме уже удалённых)
  const [recipientIds, setRecipientIds] = useState<number[]>(() => {
    const known = new Set(recipients.map((recipient) => recipient.id));
    return storedRecipients().filter((id) => known.has(id));
  });

  const mutation = useMutation({
    mutationFn: () =>
      sendPaper(paper.id, {
        recipient_ids: recipientIds,
        include_pdf: withPdf,
        translation_ids: translationIds,
      }),
    onSuccess: (run) => {
      localStorage.setItem(RECIPIENTS_KEY, JSON.stringify(recipientIds));
      void queryClient.invalidateQueries({ queryKey: ['runs'] });
      notifySuccess(`Отправка поставлена в очередь: запуск №${run.id}`);
      onClose();
    },
  });

  const toggle = (ids: number[], id: number, checked: boolean) =>
    checked ? [...ids, id] : ids.filter((item) => item !== id);
  const hasDocuments = withPdf || translationIds.length > 0;
  const allSelected = recipients.length > 0 && recipientIds.length === recipients.length;

  return (
    <Stack>
      <Stack gap="xs">
        <Text size="sm" fw={600}>
          Что отправить
        </Text>
        <Checkbox
          label="Исходная статья (PDF)"
          description={paper.has_pdf ? undefined : 'PDF-файл статьи не сохранён'}
          checked={withPdf}
          disabled={!paper.has_pdf}
          onChange={(event) => setWithPdf(event.currentTarget.checked)}
        />
        {translations.map((translation) => (
          <Checkbox
            key={translation.id}
            label={`Перевод docx — ${translation.language_label}`}
            checked={translationIds.includes(translation.id)}
            onChange={(event) =>
              setTranslationIds(toggle(translationIds, translation.id, event.currentTarget.checked))
            }
          />
        ))}
        {translations.length === 0 ? (
          <Text size="xs" c="dimmed">
            Готовых переводов у статьи нет: сначала нажмите «Перевести».
          </Text>
        ) : null}
      </Stack>

      <Stack gap="xs">
        <Group justify="space-between">
          <Text size="sm" fw={600}>
            Кому
          </Text>
          {recipients.length > 1 ? (
            <Anchor
              component="button"
              type="button"
              size="xs"
              onClick={() =>
                setRecipientIds(allSelected ? [] : recipients.map((recipient) => recipient.id))
              }
            >
              {allSelected ? 'Снять все' : 'Отметить всех'}
            </Anchor>
          ) : null}
        </Group>
        {recipients.length === 0 ? (
          <Alert color="yellow">
            Получателей пока нет. Добавьте адреса в разделе{' '}
            <Anchor component={Link} to="/recipients" size="sm">
              «Получатели»
            </Anchor>
            .
          </Alert>
        ) : (
          <Stack gap={6} mah={260} style={{ overflowY: 'auto' }}>
            {recipients.map((recipient) => (
              <Checkbox
                key={recipient.id}
                label={recipient.name ? `${recipient.name} <${recipient.email}>` : recipient.email}
                description={recipient.is_active ? undefined : 'не получает дайджесты'}
                checked={recipientIds.includes(recipient.id)}
                onChange={(event) =>
                  setRecipientIds(toggle(recipientIds, recipient.id, event.currentTarget.checked))
                }
              />
            ))}
          </Stack>
        )}
      </Stack>

      <Group justify="flex-end">
        <Button variant="default" onClick={onClose}>
          Отмена
        </Button>
        <Button
          loading={mutation.isPending}
          disabled={!hasDocuments || recipientIds.length === 0}
          onClick={() => mutation.mutate()}
        >
          Отправить
        </Button>
      </Group>
    </Stack>
  );
}

/** Отправка документов статьи по почте: выбор документов и получателей. */
export function SendPaperModal({ paperId, opened, onClose, preset }: Props) {
  const { data: paper } = useQuery({
    queryKey: ['paper', paperId],
    queryFn: () => getPaper(paperId),
    enabled: opened,
  });
  const { data: recipients } = useQuery({
    queryKey: ['recipients', 'all'],
    queryFn: () => listRecipients({ limit: 500, offset: 0 }),
    enabled: opened,
  });

  return (
    <Modal opened={opened} onClose={onClose} title="Отправить статью" centered>
      {opened && paper && recipients ? (
        // форма создаётся при открытии: отметки берутся из текущего состояния статьи
        <SendForm paper={paper} recipients={recipients.items} preset={preset} onClose={onClose} />
      ) : (
        <Loader />
      )}
    </Modal>
  );
}
