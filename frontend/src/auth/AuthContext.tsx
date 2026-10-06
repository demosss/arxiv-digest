import { useQueryClient } from '@tanstack/react-query';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import { getToken, setToken, setUnauthorizedHandler } from '../api/client';
import { login as loginRequest } from '../api/endpoints';

interface AuthState {
  isAuthenticated: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [isAuthenticated, setAuthenticated] = useState(() => getToken() !== null);

  const logout = useCallback(() => {
    setToken(null);
    setAuthenticated(false);
    queryClient.clear();
  }, [queryClient]);

  // любой ответ 401 завершает сессию: защищённые маршруты сами уведут на страницу входа
  useEffect(() => {
    setUnauthorizedHandler(logout);
    return () => setUnauthorizedHandler(null);
  }, [logout]);

  const login = useCallback(async (username: string, password: string) => {
    const { access_token } = await loginRequest(username, password);
    setToken(access_token);
    setAuthenticated(true);
  }, []);

  const value = useMemo(
    () => ({ isAuthenticated, login, logout }),
    [isAuthenticated, login, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth вызван вне AuthProvider');
  }
  return context;
}
