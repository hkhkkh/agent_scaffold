/**
 * Dynamic icon rendering.
 *
 * The backend names an icon per node type (a Lucide name), so the palette and
 * node cards stay in sync with the backend catalogue without a hard-coded map on
 * the client. Importing the whole icon set would bloat the bundle, so the icons
 * that node types actually use are listed explicitly and anything unrecognised
 * falls back to a neutral glyph rather than crashing.
 */

import {
  AlertCircle,
  Bot,
  Box,
  Boxes,
  Braces,
  Brain,
  Combine,
  Database,
  FileSearch,
  FileText,
  GitBranch,
  GitFork,
  Globe,
  HeartPulse,
  ListChecks,
  Mail,
  Play,
  PlayCircle,
  Repeat,
  Search,
  Sparkles,
  Square,
  StopCircle,
  Terminal,
  UserCheck,
  Wrench,
  type LucideIcon,
} from 'lucide-react';

const ICONS: Record<string, LucideIcon> = {
  'play-circle': PlayCircle,
  'stop-circle': StopCircle,
  boxes: Boxes,
  play: Play,
  square: Square,
  sparkles: Sparkles,
  bot: Bot,
  'file-text': FileText,
  'git-branch': GitBranch,
  'git-fork': GitFork,
  globe: Globe,
  database: Database,
  mail: Mail,
  'heart-pulse': HeartPulse,
  'file-search': FileSearch,
  terminal: Terminal,
  wrench: Wrench,
  search: Search,
  repeat: Repeat,
  combine: Combine,
  'user-check': UserCheck,
  braces: Braces,
  box: Box,
  'alert-circle': AlertCircle,
  brain: Brain,
  'list-checks': ListChecks,
};

export function iconFor(name: string): LucideIcon {
  return ICONS[name] ?? Box;
}

export const Icon = ({
  name,
  size = 14,
  strokeWidth = 2,
  className,
}: {
  name: string;
  size?: number;
  strokeWidth?: number;
  className?: string;
}) => {
  const Component = iconFor(name);
  return <Component size={size} strokeWidth={strokeWidth} className={className} />;
};
