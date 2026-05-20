import { Activity, convertMovingTime2Sec, M_TO_DIST, M_TO_ELEV } from './utils';

export interface RunSummary {
  distance: number;
  distanceMeters: number;
  elevation: number;
  movingSeconds: number;
  runCount: number;
}

export interface WeeklyRunSummary extends RunSummary {
  endDate: Date;
  key: string;
  label: string;
  monthLabel: string;
  runIds: number[];
  runs: Activity[];
  startDate: Date;
}

const dateKeyFormatter = new Intl.DateTimeFormat('en-CA', {
  day: '2-digit',
  month: '2-digit',
  year: 'numeric',
});

const parseLocalDate = (dateString: string): Date | null => {
  const [year, month, day] = dateString.slice(0, 10).split('-').map(Number);
  if (!year || !month || !day) return null;
  return new Date(year, month - 1, day);
};

const addDays = (date: Date, days: number): Date =>
  new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);

const formatDateKey = (date: Date): string => dateKeyFormatter.format(date);

const formatShortDate = (date: Date): string =>
  `${date.getMonth() + 1}/${date.getDate()}`;

const getWeekStartDate = (date: Date): Date => {
  const dayOffset = (date.getDay() + 6) % 7;
  return addDays(date, -dayOffset);
};

export const getActivityWeekKey = (activity: Activity): string => {
  const date = parseLocalDate(activity.start_date_local);
  return date ? formatDateKey(getWeekStartDate(date)) : '';
};

export const summarizeRuns = (runs: Activity[]): RunSummary => {
  const distanceMeters = runs.reduce(
    (sum, run) => sum + (run.distance || 0),
    0
  );
  const movingSeconds = runs.reduce(
    (sum, run) => sum + convertMovingTime2Sec(run.moving_time),
    0
  );
  const elevationMeters = runs.reduce(
    (sum, run) => sum + (run.elevation_gain || 0),
    0
  );

  return {
    distance: distanceMeters / M_TO_DIST,
    distanceMeters,
    elevation: elevationMeters * M_TO_ELEV,
    movingSeconds,
    runCount: runs.length,
  };
};

export const formatDurationCompact = (seconds: number): string => {
  const totalSeconds = Math.max(0, Math.round(seconds));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainingSeconds = totalSeconds % 60;

  if (hours > 0) {
    return `${hours}h${minutes ? `${minutes}m` : ''}`;
  }
  if (minutes > 0) {
    return `${minutes}m`;
  }
  return `${remainingSeconds}s`;
};

const monthLabelsByWeek = (year: number): Map<string, string> => {
  const labels = new Map<string, string>();
  for (let month = 0; month < 12; month += 1) {
    const monthStart = new Date(year, month, 1);
    labels.set(
      formatDateKey(getWeekStartDate(monthStart)),
      `${monthStart.getMonth() + 1}月`
    );
  }
  return labels;
};

export const createWeeklyRunSummaries = (
  runs: Activity[],
  year: string
): WeeklyRunSummary[] => {
  const yearNumber = Number(year);
  if (!Number.isInteger(yearNumber)) return [];

  const runsByWeek = new Map<string, Activity[]>();
  runs.forEach((run) => {
    const date = parseLocalDate(run.start_date_local);
    if (!date || date.getFullYear() !== yearNumber) return;

    const key = getActivityWeekKey(run);
    const weekRuns = runsByWeek.get(key) ?? [];
    weekRuns.push(run);
    runsByWeek.set(key, weekRuns);
  });

  const labels = monthLabelsByWeek(yearNumber);
  const yearStart = new Date(yearNumber, 0, 1);
  const yearEnd = new Date(yearNumber, 11, 31);
  const summaries: WeeklyRunSummary[] = [];

  for (
    let cursor = getWeekStartDate(yearStart);
    cursor <= yearEnd;
    cursor = addDays(cursor, 7)
  ) {
    const startDate = new Date(cursor);
    const endDate = addDays(startDate, 6);
    const key = formatDateKey(startDate);
    const weekRuns = (runsByWeek.get(key) ?? [])
      .slice()
      .sort((a, b) => a.start_date_local.localeCompare(b.start_date_local));
    const summary = summarizeRuns(weekRuns);

    summaries.push({
      ...summary,
      endDate,
      key,
      label: `${formatShortDate(startDate)} - ${formatShortDate(endDate)}`,
      monthLabel: labels.get(key) ?? '',
      runIds: weekRuns.map((run) => run.run_id),
      runs: weekRuns,
      startDate,
    });
  }

  return summaries;
};
