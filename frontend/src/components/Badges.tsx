import { Badge, type BadgeProps } from '@mantine/core';

import type { DeliveryStatus, DigestStatus, PaperStatus, RunStatus } from '../api/types';
import { DELIVERY_STATUS, DIGEST_STATUS, PAPER_STATUS, RUN_STATUS } from '../lib/labels';

type Props = Omit<BadgeProps, 'color' | 'children'>;

export function RunStatusBadge({ status, ...props }: Props & { status: RunStatus }) {
  const view = RUN_STATUS[status];
  return (
    <Badge color={view.color} variant={status === 'running' ? 'filled' : 'light'} {...props}>
      {view.label}
    </Badge>
  );
}

export function PaperStatusBadge({ status, ...props }: Props & { status: PaperStatus }) {
  const view = PAPER_STATUS[status];
  return (
    <Badge color={view.color} variant="light" {...props}>
      {view.label}
    </Badge>
  );
}

export function DigestStatusBadge({ status, ...props }: Props & { status: DigestStatus }) {
  const view = DIGEST_STATUS[status];
  return (
    <Badge color={view.color} variant="light" {...props}>
      {view.label}
    </Badge>
  );
}

export function DeliveryStatusBadge({ status, ...props }: Props & { status: DeliveryStatus }) {
  const view = DELIVERY_STATUS[status];
  return (
    <Badge color={view.color} variant="light" {...props}>
      {view.label}
    </Badge>
  );
}

/** Индикатор «зелёный / красный» для состояния внешних сервисов. */
export function HealthBadge({
  ok,
  okLabel,
  failLabel,
}: {
  ok: boolean;
  okLabel: string;
  failLabel: string;
}) {
  return (
    <Badge color={ok ? 'green' : 'red'} variant="filled" size="lg">
      {ok ? okLabel : failLabel}
    </Badge>
  );
}
