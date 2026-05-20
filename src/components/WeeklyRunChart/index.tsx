import { useMemo, type KeyboardEvent } from 'react';
import { DIST_UNIT, ELEV_UNIT } from '@/utils/utils';
import {
  createWeeklyRunSummaries,
  formatDurationCompact,
  summarizeRuns,
  type WeeklyRunSummary,
} from '@/utils/weeklyRuns';
import type { Activity } from '@/utils/utils';
import styles from './style.module.css';

interface WeeklyRunChartProps {
  onSelectWeek: (_weekKey: string | null) => void;
  runs: Activity[];
  selectedWeekKey: string | null;
  year: string;
}

const CHART_WIDTH = 720;
const CHART_HEIGHT = 160;
const PLOT_LEFT = 34;
const PLOT_RIGHT = 8;
const PLOT_TOP = 10;
const PLOT_BOTTOM = 102;
const MONTH_LABEL_Y = 126;
const YEAR_LABEL_Y = 148;

const PLOT_WIDTH = CHART_WIDTH - PLOT_LEFT - PLOT_RIGHT;
const PLOT_HEIGHT = PLOT_BOTTOM - PLOT_TOP;

const getNiceAxisMax = (value: number): number => {
  if (value <= 0) return 1;

  const targetStep = (value * 1.05) / 4;
  const magnitude = 10 ** Math.floor(Math.log10(targetStep));
  const normalizedStep = targetStep / magnitude;
  const niceSteps = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
  const niceStep =
    niceSteps.find((step) => normalizedStep <= step) ?? niceSteps.at(-1)!;

  return niceStep * magnitude * 4;
};

const formatTick = (value: number): string =>
  value >= 10 ? value.toFixed(0) : value.toFixed(1);

const formatDistance = (value: number): string => value.toFixed(1);
const MS_PER_DAY = 24 * 60 * 60 * 1000;

interface MonthAxisLabel {
  label: string;
  x: number;
}

const utcDayForDate = (date: Date): number =>
  Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / MS_PER_DAY;

const utcDayForParts = (
  yearNumber: number,
  month: number,
  day: number
): number => Date.UTC(yearNumber, month, day) / MS_PER_DAY;

const getPlotDayRange = (
  weeks: WeeklyRunSummary[]
): { endDay: number; startDay: number } | null => {
  if (!weeks.length) return null;

  return {
    endDay: utcDayForDate(weeks[weeks.length - 1].endDate) + 1,
    startDay: utcDayForDate(weeks[0].startDate),
  };
};

const xForDay = (day: number, startDay: number, endDay: number): number =>
  PLOT_LEFT + ((day - startDay) / (endDay - startDay)) * PLOT_WIDTH;

const createMonthAxisLabels = (
  yearNumber: number,
  weeks: WeeklyRunSummary[]
): MonthAxisLabel[] => {
  const plotRange = getPlotDayRange(weeks);
  if (!Number.isInteger(yearNumber) || !plotRange) return [];
  const { startDay, endDay } = plotRange;

  return Array.from({ length: 12 }, (_, month) => {
    const monthStartDay = utcDayForParts(yearNumber, month, 1);
    const monthEndDay = utcDayForParts(yearNumber, month + 1, 1);
    const monthCenterDay = monthStartDay + (monthEndDay - monthStartDay) / 2;

    return {
      label: `${month + 1}月`,
      x: xForDay(monthCenterDay, startDay, endDay),
    };
  });
};

const createYearAxisX = (
  yearNumber: number,
  weeks: WeeklyRunSummary[]
): number | null => {
  const plotRange = getPlotDayRange(weeks);
  if (!Number.isInteger(yearNumber) || !plotRange) return null;

  const yearStartDay = utcDayForParts(yearNumber, 0, 1);
  const nextYearStartDay = utcDayForParts(yearNumber + 1, 0, 1);
  const yearCenterDay = yearStartDay + (nextYearStartDay - yearStartDay) / 2;

  return xForDay(yearCenterDay, plotRange.startDay, plotRange.endDay);
};

const WeeklyRunChart = ({
  runs,
  selectedWeekKey,
  onSelectWeek,
  year,
}: WeeklyRunChartProps) => {
  const weeks = useMemo(
    () => createWeeklyRunSummaries(runs, year),
    [runs, year]
  );
  const yearNumber = Number(year);
  const monthAxisLabels = useMemo(
    () => createMonthAxisLabels(yearNumber, weeks),
    [yearNumber, weeks]
  );
  const yearLabelX = useMemo(
    () => createYearAxisX(yearNumber, weeks),
    [yearNumber, weeks]
  );
  const selectedWeek = useMemo(
    () => weeks.find((week) => week.key === selectedWeekKey) ?? null,
    [selectedWeekKey, weeks]
  );
  const yearlySummary = useMemo(() => summarizeRuns(runs), [runs]);
  const summary = selectedWeek ?? yearlySummary;
  const summaryLabel = selectedWeek?.label ?? 'Total';
  const axisMax = getNiceAxisMax(
    Math.max(...weeks.map((week) => week.distance), 0)
  );
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((ratio) => axisMax * ratio);
  const slotWidth = weeks.length ? PLOT_WIDTH / weeks.length : PLOT_WIDTH;
  const barWidth = Math.max(2, Math.min(12, slotWidth * 0.88));

  const selectWeek = (week: WeeklyRunSummary) => {
    if (!week.runCount) return;
    onSelectWeek(selectedWeekKey === week.key ? null : week.key);
  };

  const handleKeyDown = (
    event: KeyboardEvent<SVGGElement>,
    week: WeeklyRunSummary
  ) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    selectWeek(week);
  };

  if (!weeks.length) {
    return null;
  }

  return (
    <section className={styles.chart} aria-label={`${year} weekly volume`}>
      <div className={styles.summary} aria-live="polite">
        <span className={`${styles.summaryItem} ${styles.period}`}>
          {summaryLabel}
        </span>
        <span className={styles.divider} />
        <strong className={`${styles.summaryItem} ${styles.metric}`}>
          {formatDistance(summary.distance)} {DIST_UNIT}
        </strong>
        <span className={styles.divider} />
        <strong className={`${styles.summaryItem} ${styles.metric}`}>
          {formatDurationCompact(summary.movingSeconds)}
        </strong>
        <span className={styles.divider} />
        <strong className={`${styles.summaryItem} ${styles.metric}`}>
          {summary.elevation.toFixed(0)} {ELEV_UNIT}
        </strong>
        <span className={styles.divider} />
        <strong className={`${styles.summaryItem} ${styles.metric}`}>
          {summary.runCount} Runs
        </strong>
      </div>
      <svg
        className={styles.svg}
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        role="img"
      >
        {ticks.map((tick) => {
          const y = PLOT_BOTTOM - (tick / axisMax) * PLOT_HEIGHT;
          return (
            <g key={tick}>
              <line
                className={styles.gridLine}
                x1={PLOT_LEFT}
                x2={CHART_WIDTH - PLOT_RIGHT}
                y1={y}
                y2={y}
              />
              <text className={styles.axisLabel} x={PLOT_LEFT - 6} y={y + 4}>
                {formatTick(tick)}
              </text>
            </g>
          );
        })}
        {weeks.map((week, index) => {
          const x = PLOT_LEFT + index * slotWidth + (slotWidth - barWidth) / 2;
          const rawHeight = (week.distance / axisMax) * PLOT_HEIGHT;
          const barHeight = week.distance > 0 ? Math.max(2, rawHeight) : 0;
          const y = PLOT_BOTTOM - barHeight;
          const isSelected = selectedWeekKey === week.key;
          const canSelect = week.runCount > 0;

          return (
            <g
              key={week.key}
              aria-label={`${week.label}: ${formatDistance(
                week.distance
              )} ${DIST_UNIT}, ${formatDurationCompact(week.movingSeconds)}`}
              className={`${styles.week} ${canSelect ? styles.selectable : ''} ${
                isSelected ? styles.selected : ''
              }`}
              onClick={() => selectWeek(week)}
              onKeyDown={(event) => handleKeyDown(event, week)}
              role={canSelect ? 'button' : undefined}
              tabIndex={canSelect ? 0 : undefined}
            >
              <title>
                {week.label}: {formatDistance(week.distance)} {DIST_UNIT}
              </title>
              <rect
                className={styles.hitArea}
                x={PLOT_LEFT + index * slotWidth}
                y={PLOT_TOP}
                width={slotWidth}
                height={PLOT_HEIGHT}
              />
              {barHeight > 0 && (
                <rect
                  className={styles.bar}
                  x={x}
                  y={y}
                  width={barWidth}
                  height={barHeight}
                />
              )}
            </g>
          );
        })}
        {monthAxisLabels.map((month) => (
          <text
            key={month.label}
            className={styles.monthLabel}
            x={month.x}
            y={MONTH_LABEL_Y}
          >
            {month.label}
          </text>
        ))}
        {yearLabelX !== null && (
          <text className={styles.yearLabel} x={yearLabelX} y={YEAR_LABEL_Y}>
            {yearNumber}
          </text>
        )}
        <line
          className={styles.axisLine}
          x1={PLOT_LEFT}
          x2={CHART_WIDTH - PLOT_RIGHT}
          y1={PLOT_BOTTOM}
          y2={PLOT_BOTTOM}
        />
      </svg>
    </section>
  );
};

export default WeeklyRunChart;
