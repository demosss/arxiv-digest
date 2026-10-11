import { Center, Loader } from '@mantine/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import { getProfileId, setProfileId, setProfileNotFoundHandler } from '../api/client';
import { listProfiles } from '../api/endpoints';
import type { Profile } from '../api/types';
import { QueryState } from '../components/common';

// данные, общие для всех профилей: при смене профиля остаются в кеше
const SHARED_QUERIES = new Set(['profiles', 'me', 'settings', 'smtp']);

interface ProfileState {
  profiles: Profile[];
  /** Профиль, с данными которого работает админка. */
  current: Profile;
  select: (profileId: number) => void;
}

const ProfileContext = createContext<ProfileState | null>(null);

export function ProfileProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<number | null>(() => getProfileId());
  const { data, isLoading, error } = useQuery({ queryKey: ['profiles'], queryFn: listProfiles });

  // выбранный профиль могли удалить (например, в другой вкладке): тогда берётся первый
  const current = data?.find((profile) => profile.id === selected) ?? data?.[0] ?? null;
  if (current && getProfileId() !== current.id) {
    // до первого запроса страницы: заголовок X-Profile-Id должен указывать на текущий профиль
    setProfileId(current.id);
  }

  useEffect(() => {
    setProfileNotFoundHandler(() => void queryClient.invalidateQueries({ queryKey: ['profiles'] }));
    return () => setProfileNotFoundHandler(null);
  }, [queryClient]);

  const select = useCallback(
    (profileId: number) => {
      if (profileId === getProfileId()) {
        return;
      }
      setProfileId(profileId);
      // ключи запросов профиль не содержат: данные прежнего профиля убираются из кеша целиком
      const ofProfile = {
        predicate: (query: { queryKey: readonly unknown[] }) =>
          !SHARED_QUERIES.has(String(query.queryKey[0])),
      };
      void queryClient.cancelQueries(ofProfile);
      queryClient.removeQueries(ofProfile);
      setSelected(profileId);
    },
    [queryClient],
  );

  const value = useMemo(
    () => (data && current ? { profiles: data, current, select } : null),
    [data, current, select],
  );

  if (!value) {
    return (
      <Center mih="100vh">
        <QueryState isLoading={isLoading} error={error}>
          <Loader />
        </QueryState>
      </Center>
    );
  }
  return <ProfileContext.Provider value={value}>{children}</ProfileContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useProfile(): ProfileState {
  const context = useContext(ProfileContext);
  if (!context) {
    throw new Error('useProfile вызван вне ProfileProvider');
  }
  return context;
}
