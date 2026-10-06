import type { AxiosError } from 'axios';
import axios from 'axios';

import type { ApiErrorBody } from './types';

const TOKEN_KEY = 'arxiv-digest.token';

// Токен живёт в памяти и дублируется в localStorage, чтобы пережить перезагрузку страницы.
let token: string | null = localStorage.getItem(TOKEN_KEY);
let onUnauthorized: (() => void) | null = null;

export function getToken(): string | null {
  return token;
}

export function setToken(value: string | null): void {
  token = value;
  if (value) {
    localStorage.setItem(TOKEN_KEY, value);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

/** Обработчик ответа 401: провайдер авторизации сбрасывает сессию и уводит на страницу входа. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

export const api = axios.create({ baseURL: '/api', timeout: 60_000 });

api.interceptors.request.use((config) => {
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    const isLogin = error.config?.url?.endsWith('/auth/login') ?? false;
    if (error.response?.status === 401 && !isLogin) {
      setToken(null);
      onUnauthorized?.();
    }
    return Promise.reject(error);
  },
);

export function isUnauthorized(error: unknown): boolean {
  return axios.isAxiosError(error) && error.response?.status === 401;
}

/** Человекочитаемый текст ошибки из ответа API вида { detail, code }. */
export function getErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const body = error.response?.data as Partial<ApiErrorBody> | undefined;
    if (typeof body?.detail === 'string') {
      return body.detail;
    }
    if (Array.isArray(body?.detail)) {
      return body.detail
        .map((item) => {
          const field = item.loc.filter((part) => part !== 'body').join('.');
          return field ? `${field}: ${item.msg}` : item.msg;
        })
        .join('; ');
    }
    if (error.response) {
      return `Ошибка сервера (${error.response.status})`;
    }
    return 'Сервер недоступен: проверьте соединение';
  }
  return error instanceof Error ? error.message : 'Неизвестная ошибка';
}
