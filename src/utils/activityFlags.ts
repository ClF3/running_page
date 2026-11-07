import type { Activity } from './utils';

// New exports use the shared Python classifier. Only old, already-normalized
// exports need this compatibility fallback; GPS never determines indoor status.
export const isIndoorRun = (activity: Activity): boolean =>
  activity.is_indoor ??
  (activity.subtype === 'indoor' || activity.subtype === 'treadmill');
