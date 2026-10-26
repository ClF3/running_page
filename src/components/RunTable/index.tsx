import React, { useState, useMemo, useCallback } from 'react';
import {
  sortDateFunc,
  sortDateFuncReverse,
  convertMovingTime2Sec,
  Activity,
  RunIds,
} from '@/utils/utils';
import { IS_CHINESE, SHOW_ELEVATION_GAIN } from '@/utils/const';
import { DIST_UNIT } from '@/utils/utils';

import RunRow from './RunRow';
import styles from './style.module.css';

interface IRunTableProperties {
  runs: Activity[];
  locateActivity: (_runIds: RunIds) => void;
  runIndex: number;
  setRunIndex: (_index: number) => void;
  sourceRuns?: Activity[];
}

type SortFunc = (_a: Activity, _b: Activity) => number;
type SortDirection = 'ascending' | 'descending';

interface SortState {
  direction: SortDirection;
  key: string;
}

const PAGE_SIZES = [20, 50, 100];

interface PageControlsProps {
  page: number;
  pageCount: number;
  onChange: (_page: number) => void;
  position: 'top' | 'bottom';
}

const PageControls = ({
  page,
  pageCount,
  onChange,
  position,
}: PageControlsProps) => (
  <nav
    className={styles.pageControls}
    aria-label={`${IS_CHINESE ? '运动记录分页' : 'Activity pages'} (${position})`}
  >
    <button
      type="button"
      disabled={page === 1}
      onClick={() => onChange(page - 1)}
      aria-label={IS_CHINESE ? '上一页' : 'Previous page'}
    >
      ←
    </button>
    <label>
      <span className={styles.srOnly}>{IS_CHINESE ? '页码' : 'Page'}</span>
      <select
        value={page}
        onChange={(event) => onChange(Number(event.target.value))}
      >
        {Array.from({ length: pageCount }, (_, index) => (
          <option key={index + 1} value={index + 1}>
            {IS_CHINESE ? `第 ${index + 1} 页` : `Page ${index + 1}`}
          </option>
        ))}
      </select>
      <span className={styles.pageCount}> / {pageCount}</span>
    </label>
    <button
      type="button"
      disabled={page === pageCount}
      onClick={() => onChange(page + 1)}
      aria-label={IS_CHINESE ? '下一页' : 'Next page'}
    >
      →
    </button>
  </nav>
);

const RunTable = ({
  runs,
  locateActivity,
  runIndex,
  setRunIndex,
  sourceRuns,
}: IRunTableProperties) => {
  const [sortState, setSortState] = useState<SortState | null>(null);

  const sortKeys = useMemo(() => {
    const keys = [DIST_UNIT, 'Elev', 'Pace', 'BPM', 'Time', 'Date'];
    return SHOW_ELEVATION_GAIN ? keys : keys.filter((key) => key !== 'Elev');
  }, []);

  const getSortFunction = useCallback(
    (key: string, direction: SortDirection): SortFunc | undefined => {
      const multiplier = direction === 'ascending' ? 1 : -1;

      if (key === DIST_UNIT) {
        return (a, b) => (a.distance - b.distance) * multiplier;
      }
      if (key === 'Elev') {
        return (a, b) =>
          ((a.elevation_gain ?? 0) - (b.elevation_gain ?? 0)) * multiplier;
      }
      if (key === 'Pace') {
        return (a, b) => (a.average_speed - b.average_speed) * multiplier;
      }
      if (key === 'BPM') {
        return (a, b) =>
          ((a.average_heartrate ?? 0) - (b.average_heartrate ?? 0)) *
          multiplier;
      }
      if (key === 'Time') {
        return (a, b) =>
          (convertMovingTime2Sec(a.moving_time) -
            convertMovingTime2Sec(b.moving_time)) *
          multiplier;
      }
      if (key === 'Date') {
        return direction === 'ascending' ? sortDateFuncReverse : sortDateFunc;
      }

      return undefined;
    },
    []
  );

  const displayedRuns = useMemo(() => {
    if (!sortState) return runs;

    const sortFunction = getSortFunction(sortState.key, sortState.direction);
    if (!sortFunction) return runs;

    return runs.slice().sort(sortFunction);
  }, [getSortFunction, runs, sortState]);

  const selectedRunId = (sourceRuns ?? runs)[runIndex]?.run_id ?? null;
  const [pagination, setPagination] = useState({
    runs,
    sortState,
    selectedRunId,
    page: 1,
    pageSize: PAGE_SIZES[0],
  });

  // Reset for new filters/sorts, and reveal selections made through a map or URL.
  if (
    pagination.runs !== runs ||
    pagination.sortState !== sortState ||
    pagination.selectedRunId !== selectedRunId
  ) {
    let nextPage =
      pagination.runs !== runs || pagination.sortState !== sortState
        ? 1
        : pagination.page;
    if (selectedRunId !== null) {
      const selectedIndex = displayedRuns.findIndex(
        (run) => run.run_id === selectedRunId
      );
      if (selectedIndex >= 0)
        nextPage = Math.floor(selectedIndex / pagination.pageSize) + 1;
    }
    setPagination({
      ...pagination,
      runs,
      sortState,
      selectedRunId,
      page: nextPage,
    });
  }

  const { pageSize } = pagination;
  const pageCount = Math.max(1, Math.ceil(displayedRuns.length / pageSize));
  const page = Math.min(pagination.page, pageCount);
  const startIndex = (page - 1) * pageSize;
  const pageRuns = displayedRuns.slice(startIndex, startIndex + pageSize);
  const changePage = (nextPage: number) =>
    setPagination((current) => ({
      ...current,
      page: Math.max(1, Math.min(nextPage, pageCount)),
    }));

  const runIndexById = useMemo(
    () =>
      new Map((sourceRuns ?? runs).map((run, index) => [run.run_id, index])),
    [runs, sourceRuns]
  );

  const handleClick = useCallback(
    (key: string) => {
      setRunIndex(-1);
      setSortState((currentState) => {
        const initialDirection = key === 'Date' ? 'ascending' : 'descending';
        const nextDirection =
          currentState?.key === key
            ? currentState.direction === 'descending'
              ? 'ascending'
              : 'descending'
            : initialDirection;

        return { key, direction: nextDirection };
      });
    },
    [setRunIndex]
  );

  return (
    <section
      className={styles.records}
      aria-label={IS_CHINESE ? '运动记录' : 'Activities'}
    >
      <div className={styles.toolbar}>
        <h2>
          {IS_CHINESE ? '运动记录' : 'Activities'}{' '}
          <span>{displayedRuns.length}</span>
        </h2>
        <div className={styles.tableOptions}>
          <label className={styles.pageSize}>
            {IS_CHINESE ? '每页' : 'Per page'}
            <select
              aria-label={IS_CHINESE ? '每页记录数' : 'Activities per page'}
              value={pageSize}
              onChange={(event) =>
                setPagination((current) => ({
                  ...current,
                  pageSize: Number(event.target.value),
                  page: 1,
                }))
              }
            >
              {PAGE_SIZES.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </label>
          {pageCount > 1 && (
            <PageControls
              page={page}
              pageCount={pageCount}
              onChange={changePage}
              position="top"
            />
          )}
        </div>
      </div>
      <div className={styles.tableContainer}>
        <table className={styles.runTable} cellSpacing="0" cellPadding="0">
          <colgroup>
            <col className={styles.titleColumn} />
            <col className={styles.distanceColumn} />
            {SHOW_ELEVATION_GAIN && <col className={styles.elevationColumn} />}
            <col className={styles.paceColumn} />
            <col className={styles.heartRateColumn} />
            <col className={styles.timeColumn} />
            <col className={styles.dateColumn} />
          </colgroup>
          <thead>
            <tr>
              <th />
              {sortKeys.map((k) => (
                <th
                  key={k}
                  aria-sort={
                    sortState?.key === k ? sortState.direction : undefined
                  }
                  className={styles.sortableHeader}
                >
                  <button type="button" onClick={() => handleClick(k)}>
                    {k}
                    {sortState?.key === k && (
                      <span aria-hidden="true">
                        {' '}
                        {sortState.direction === 'ascending' ? '↑' : '↓'}
                      </span>
                    )}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {pageRuns.map((run) => {
              const sourceIndex = runIndexById.get(run.run_id) ?? -1;
              return (
                <RunRow
                  key={run.run_id}
                  elementIndex={sourceIndex}
                  locateActivity={locateActivity}
                  run={run}
                  runIndex={runIndex}
                  setRunIndex={setRunIndex}
                />
              );
            })}
            {displayedRuns.length === 0 && (
              <tr>
                <td className={styles.empty} colSpan={sortKeys.length + 1}>
                  {IS_CHINESE
                    ? '没有符合条件的运动记录'
                    : 'No activities match this filter.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {pageCount > 1 && (
        <div className={styles.paginationFooter}>
          <span aria-live="polite">
            {startIndex + 1}–
            {Math.min(startIndex + pageSize, displayedRuns.length)} /{' '}
            {displayedRuns.length} {IS_CHINESE ? '条记录' : 'activities'}
          </span>
          <PageControls
            page={page}
            pageCount={pageCount}
            onChange={changePage}
            position="bottom"
          />
        </div>
      )}
    </section>
  );
};

export default RunTable;
