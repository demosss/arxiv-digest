import { notifications } from '@mantine/notifications';
import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query';
import axios from 'axios';

import { getErrorMessage, isUnauthorized } from '../api/client';

function notifyError(title: string, error: unknown): void {
  // 401 обрабатывает перехватчик axios: сессия сбрасывается, показывается страница входа
  if (isUnauthorized(error)) {
    return;
  }
  notifications.show({ color: 'red', title, message: getErrorMessage(error), autoClose: 8000 });
}

export const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => {
      // при фоновом обновлении уже показанных данных не засыпаем экран одинаковыми ошибками
      if (query.state.data === undefined) {
        notifyError('Не удалось загрузить данные', error);
      }
    },
  }),
  mutationCache: new MutationCache({
    onError: (error, _variables, _context, mutation) => {
      if (!mutation.meta?.silent) {
        notifyError('Операция не выполнена', error);
      }
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      refetchOnWindowFocus: false,
      // страницы мониторинга (запуск, логи, дашборд) обновляются и в неактивной вкладке
      refetchIntervalInBackground: true,
      retry: (failureCount, error) => {
        const status = axios.isAxiosError(error) ? error.response?.status : undefined;
        // ошибки клиента (4xx) повтор не исправит
        return (status === undefined || status >= 500) && failureCount < 2;
      },
    },
  },
});

export function notifySuccess(message: string, title = 'Готово'): void {
  notifications.show({ color: 'green', title, message });
}
