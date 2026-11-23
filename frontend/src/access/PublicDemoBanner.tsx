import { Globe2 } from 'lucide-react';

import { useAccess } from '@/access/accessStore';
import { useT } from '@/i18n';

/** A persistent reminder that the public demo is shared and not private. */
export function PublicDemoBanner() {
  const t = useT();
  const demo = useAccess((state) => state.status.public_demo);
  const quota = useAccess((state) => state.status.quota);
  if (!demo) return null;

  const label =
    quota?.enabled
      ? `${t('gate.demoBadge')} · ${t('gate.quotaBadge', {
          ai: quota.ai_requests.remaining,
          aiLimit: quota.ai_requests.limit,
          tokens: quota.tokens.remaining.toLocaleString(),
        })}`
      : t('gate.demoBadge');

  const title =
    quota?.enabled
      ? t('gate.quotaTitle', {
          aiLimit: quota.ai_requests.limit,
          runLimit: quota.runs.limit,
          tokenLimit: quota.tokens.limit.toLocaleString(),
          reset: new Date(quota.resets_at).toLocaleString(),
        })
      : t('gate.demoNote');

  return (
    <span className="public-demo-badge" title={title}>
      <Globe2 size={11} />
      {label}
    </span>
  );
}
