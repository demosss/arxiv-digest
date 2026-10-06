import '@mantine/core/styles.css';
import '@mantine/dates/styles.css';
import '@mantine/notifications/styles.css';
import 'dayjs/locale/ru';

import { MantineProvider, createTheme } from '@mantine/core';
import { DatesProvider } from '@mantine/dates';
import { ModalsProvider } from '@mantine/modals';
import { Notifications } from '@mantine/notifications';
import { QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';

import { AuthProvider, useAuth } from './auth/AuthContext';
import { AppLayout } from './components/AppLayout';
import { FormatProvider } from './lib/format';
import { queryClient } from './lib/queryClient';
import { DashboardPage } from './pages/DashboardPage';
import { DigestsPage } from './pages/DigestsPage';
import { LoginPage } from './pages/LoginPage';
import { PapersPage } from './pages/PapersPage';
import { RecipientsPage } from './pages/RecipientsPage';
import { RunDetailPage } from './pages/RunDetailPage';
import { RunsPage } from './pages/RunsPage';
import { SchedulesPage } from './pages/SchedulesPage';
import { SmtpPage } from './pages/SmtpPage';
import { TopicsPage } from './pages/TopicsPage';

const theme = createTheme({ primaryColor: 'blue', defaultRadius: 'md' });

/** Маршруты админки: без токена — на страницу входа с возвратом на исходный адрес. */
function ProtectedRoutes() {
  const { isAuthenticated } = useAuth();
  const location = useLocation();
  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }
  return (
    <FormatProvider>
      <Outlet />
    </FormatProvider>
  );
}

export function App() {
  return (
    <MantineProvider theme={theme}>
      <DatesProvider settings={{ locale: 'ru', firstDayOfWeek: 1 }}>
        <QueryClientProvider client={queryClient}>
          <ModalsProvider>
            <Notifications position="top-right" />
            <BrowserRouter>
              <AuthProvider>
                <Routes>
                  <Route path="/login" element={<LoginPage />} />
                  <Route element={<ProtectedRoutes />}>
                    <Route element={<AppLayout />}>
                      <Route index element={<DashboardPage />} />
                      <Route path="topics" element={<TopicsPage />} />
                      <Route path="recipients" element={<RecipientsPage />} />
                      <Route path="schedules" element={<SchedulesPage />} />
                      <Route path="smtp" element={<SmtpPage />} />
                      <Route path="runs" element={<RunsPage />} />
                      <Route path="runs/:runId" element={<RunDetailPage />} />
                      <Route path="papers" element={<PapersPage />} />
                      <Route path="digests" element={<DigestsPage />} />
                      <Route path="*" element={<Navigate to="/" replace />} />
                    </Route>
                  </Route>
                </Routes>
              </AuthProvider>
            </BrowserRouter>
          </ModalsProvider>
        </QueryClientProvider>
      </DatesProvider>
    </MantineProvider>
  );
}
