import { Autocomplete, Button, Group, Modal, Stack, Text } from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { listTranslationLanguages, translatePaper } from '../api/endpoints';
import { notifySuccess } from '../lib/queryClient';

const LANGUAGE_KEY = 'arxiv-digest.translate-language';
const DEFAULT_LANGUAGE = 'Русский';

interface Props {
  paperId: number;
  opened: boolean;
  onClose: () => void;
}

/** Выбор языка перевода: язык из списка или любой другой, введённый названием. */
export function TranslateModal({ paperId, opened, onClose }: Props) {
  const queryClient = useQueryClient();
  // по умолчанию — язык, выбранный в прошлый раз
  const [language, setLanguage] = useState(
    () => localStorage.getItem(LANGUAGE_KEY) ?? DEFAULT_LANGUAGE,
  );
  const { data: languages } = useQuery({
    queryKey: ['translations', 'languages'],
    queryFn: listTranslationLanguages,
    staleTime: Infinity,
  });

  const mutation = useMutation({
    mutationFn: (value: string) => translatePaper(paperId, value),
    onSuccess: (translation) => {
      void queryClient.invalidateQueries({ queryKey: ['paper', paperId] });
      void queryClient.invalidateQueries({ queryKey: ['translations'] });
      void queryClient.invalidateQueries({ queryKey: ['runs'] });
      notifySuccess(
        `Перевод на язык «${translation.language_label}» поставлен в очередь: запуск №${translation.run_id}`,
      );
      onClose();
    },
  });

  const value = language.trim();
  const submit = () => {
    const known = languages?.find((item) => item.label.toLowerCase() === value.toLowerCase());
    localStorage.setItem(LANGUAGE_KEY, known ? known.label : value);
    mutation.mutate(known ? known.code : value);
  };

  return (
    <Modal opened={opened} onClose={onClose} title="Перевод статьи" centered>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (value) submit();
        }}
      >
        <Stack>
          <Autocomplete
            label="Язык перевода"
            description="Выберите язык из списка или впишите любой другой"
            data={(languages ?? []).map((item) => item.label)}
            value={language}
            onChange={setLanguage}
            maxLength={64}
            data-autofocus
          />
          <Text size="sm" c="dimmed">
            Перевод выполняет локальная модель и занимает от нескольких минут до часа. Готовый файл
            docx появится в карточке статьи и в разделе «Переводы»; прежний перевод на этот язык
            будет заменён.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>
              Отмена
            </Button>
            <Button type="submit" loading={mutation.isPending} disabled={!value}>
              Перевести
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}
