/**
 * Public-access state: whether a gate is required, and which capabilities the
 * deployment exposes. Loaded before the editor mounts so the first render does
 * not fire a burst of 403s.
 */

import { create } from 'zustand';

export interface AccessCapabilities {
  public_demo: boolean;
  code_nodes: boolean;
  custom_tools: boolean;
  settings: boolean;
  schedules: boolean;
  exports: boolean;
  workflow_delete: boolean;
  metrics: boolean;
  admin: boolean;
}

export interface QuotaBucket {
  used: number;
  limit: number;
  remaining: number;
}

export interface AccessQuota {
  enabled: boolean;
  per: 'visitor' | 'ip';
  ai_requests: QuotaBucket;
  runs: QuotaBucket;
  tokens: QuotaBucket;
  resets_at: string;
}

export interface AccessStatus {
  required: boolean;
  verified: boolean;
  public_demo: boolean;
  turnstile_site_key: string;
  pow_difficulty: number;
  pow_algorithm: string;
  capabilities: AccessCapabilities;
  /** Present in public mode; absent on a local/dev backend. */
  quota?: AccessQuota;
}

const OPEN: AccessStatus = {
  required: false,
  verified: true,
  public_demo: false,
  turnstile_site_key: '',
  pow_difficulty: 16,
  pow_algorithm: 'sha256-leading-zero-bits',
  capabilities: {
    public_demo: false,
    code_nodes: true,
    custom_tools: true,
    settings: true,
    schedules: true,
    exports: true,
    workflow_delete: true,
    metrics: true,
    admin: false,
  },
};

interface AccessState {
  status: AccessStatus;
  loading: boolean;
  error: string | null;
  load: () => Promise<AccessStatus>;
  markVerified: () => void;
  setStatus: (status: AccessStatus) => void;
}

export const useAccess = create<AccessState>((set) => ({
  status: OPEN,
  loading: true,
  error: null,
  load: async () => {
    set({ loading: true, error: null });
    try {
      const response = await fetch('/api/access/status', {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'X-Scaffold-Client': 'web' },
      });
      if (!response.ok) throw new Error(`Access status failed (${response.status}).`);
      const status = (await response.json()) as AccessStatus;
      set({ status, loading: false, error: null });
      return status;
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : String(caught);
      set({ loading: false, error: message });
      throw caught;
    }
  },
  markVerified: () =>
    set((state) => ({ status: { ...state.status, verified: true } })),
  setStatus: (status) => set({ status, loading: false, error: null }),
}));

export function dispatchGateRequired(): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent('scaffold:gate-required'));
}

export function isGateRequired(payload: unknown): boolean {
  return (
    typeof payload === 'object' &&
    payload !== null &&
    (payload as { code?: unknown }).code === 'gate_required'
  );
}
