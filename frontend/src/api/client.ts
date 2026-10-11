import type { AxiosError } from 'axios';
import axios from 'axios';

import type { ApiErrorBody } from './types';

const TOKEN_KEY = 'arxiv-digest.token';
const PROFILE_KEY = 'arxiv-digest.profile';

// Токен живёт в памяти и дублируется в localStorage, чтобы пережить перезагрузку страницы.
let token: string | null = localStorage.getItem(TOKEN_KEY);
let onUnauthorized: (() => void) | null = null;

function storedProfileId(): number | null {
  const value = Number(localStorage.getItem(PROFILE_KEY));
  return Number.isInteger(value) && value > 0 ? value : null;
}

// Выбранный профиль уходит в каждом запросе заголовком X-Profile-Id: списки, создание
// и очистка на backend работают только с его данными.
let profileId: number | null = storedProfileId();
let onProfileNotFound: (() => void) | null = null;

export function getProfileId(): number | null {
  return profileId;
}

export function setProfileId(value: number): void {
  profileId = value;
  localStorage.setItem(PROFILE_KEY, String(value));
}

/** Обработчик ответа «профиль не найден»: выбранный профиль удалён, список нужно перечитать. */
export function setProfileNotFoundHandler(handler: (() => void) | null): void {
  onProfileNotFound = handler;
}

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
  if (profileId !== null) {
    config.headers['X-Profile-Id'] = String(profileId);
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
    const body = error.response?.data as Partial<ApiErrorBody> | undefined;
    if (error.response?.status === 404 && body?.code === 'profile_not_found') {
      onProfileNotFound?.();
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
