import { formatPace, type Activity } from './utils';
import { summarizeRuns } from './weeklyRuns';

export const summarizeDashboard = (runs: Activity[]) => {
  const totals = summarizeRuns(runs);
  let paceMeters = 0;
  let paceSeconds = 0;
  let heartRateTotal = 0;
  let heartRateCount = 0;
  for (const run of runs) {
    if (run.average_speed > 0) {
      paceMeters += run.distance || 0;
      paceSeconds += (run.distance || 0) / run.average_speed;
    }
    if (run.average_heartrate && run.average_heartrate > 0) {
      heartRateTotal += run.average_heartrate;
      heartRateCount += 1;
    }
  }
  // Count calendar days in the selected scope; a streak inherited from another year
  // or place must not inflate the current summary.
  const days = [
    ...new Set(
      runs.map((run) => Date.parse(run.start_date_local.slice(0, 10)))
    ),
  ]
    .filter(Number.isFinite)
    .sort((a, b) => a - b);
  let streak = 0;
  let longestStreak = 0;
  days.forEach((day, index) => {
    streak = index && day - days[index - 1] === 86_400_000 ? streak + 1 : 1;
    longestStreak = Math.max(longestStreak, streak);
  });
  return {
    ...totals,
    pace: paceSeconds ? formatPace(paceMeters / paceSeconds) : '—',
    heartRate: heartRateCount
      ? String(Math.round(heartRateTotal / heartRateCount))
      : '—',
    longestStreak,
  };
};
