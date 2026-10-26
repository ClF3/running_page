import { useMemo } from 'react';
import useActivities from '@/hooks/useActivities';
import getSiteMetadata from '@/hooks/useSiteMetadata';
import { IS_CHINESE, SHOW_ELEVATION_GAIN } from '@/utils/const';
import { DIST_UNIT, M_TO_DIST, type Activity } from '@/utils/utils';
import { summarizeDashboard } from '@/utils/dashboardStats';
import { formatDurationCompact } from '@/utils/weeklyRuns';
import type { PlaceNode } from '@/utils/locations';
import styles from './style.module.css';

const label = (zh: string, en: string) => (IS_CHINESE ? zh : en);
const distance = (meters: number) =>
  (meters / M_TO_DIST).toLocaleString(undefined, {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });

const PlaceRows = ({
  nodes,
  selectedKey,
  onSelect,
}: {
  nodes: PlaceNode[];
  selectedKey?: string;
  onSelect: (_node: PlaceNode) => void;
}) => (
  <ul className={styles.places}>
    {nodes.map((node) => {
      const name = node.name || label('地点未细分', 'Unspecified');
      const content = (
        <>
          <span className={styles.placeName}>{name}</span>
          <span className={styles.placeDistance}>
            {distance(node.distanceMeters)} <small>{DIST_UNIT}</small>
          </span>
        </>
      );
      const selectButton = (
        <button
          type="button"
          className={styles.viewRecords}
          aria-pressed={selectedKey === node.key}
          onClick={() => onSelect(node)}
        >
          {label(
            `查看${name}的 ${node.runCount} 次记录`,
            `View ${node.runCount} activities in ${name}`
          )}
        </button>
      );
      return (
        <li key={node.key}>
          {node.children.length ? (
            <details>
              <summary className={styles.placeRow}>{content}</summary>
              <div className={styles.placeChildren}>
                {selectButton}
                <PlaceRows
                  nodes={node.children}
                  selectedKey={selectedKey}
                  onSelect={onSelect}
                />
              </div>
            </details>
          ) : (
            <button
              type="button"
              className={`${styles.placeRow} ${styles.placeLeaf}`}
              aria-pressed={selectedKey === node.key}
              title={label(`查看${name}的记录`, `View activities in ${name}`)}
              onClick={() => onSelect(node)}
            >
              {content}
            </button>
          )}
        </li>
      );
    })}
  </ul>
);

const DashboardSidebar = ({
  runs,
  year,
  selectedPlace,
  onChangeYear,
  onSelectPlace,
}: {
  runs: Activity[];
  year: string;
  selectedPlace?: { key: string; name: string };
  onChangeYear: (_year: string) => void;
  onSelectPlace: (_node: PlaceNode) => void;
}) => {
  const { siteTitle, siteUrl } = getSiteMetadata();
  const { years, locations } = useActivities();
  const stats = useMemo(() => summarizeDashboard(runs), [runs]);
  const metrics = [
    [
      label('运动次数', 'Activities'),
      stats.runCount.toLocaleString(),
      label('次', 'runs'),
    ],
    [label('平均配速', 'Average pace'), stats.pace, `/${DIST_UNIT}`],
    [
      label('最长连续', 'Longest streak'),
      stats.longestStreak,
      label('天', 'days'),
    ],
    [label('平均心率', 'Average heart rate'), stats.heartRate, 'bpm'],
    ...(SHOW_ELEVATION_GAIN
      ? [
          [
            label('累计爬升', 'Elevation gain'),
            Math.round(stats.elevation).toLocaleString(),
            DIST_UNIT === 'km' ? 'm' : 'ft',
          ],
        ]
      : []),
  ];

  return (
    <div className={styles.root}>
      <header className={styles.identity}>
        <h1>
          <a href={siteUrl}>{siteTitle}</a>
        </h1>
      </header>
      <div className={styles.scope}>
        <span>
          {selectedPlace
            ? label('地点统计', 'Place summary')
            : label('年度统计', 'Year summary')}
        </span>
        <select
          aria-label={label('统计范围', 'Summary scope')}
          value={selectedPlace ? '' : year}
          onChange={(event) => onChangeYear(event.target.value)}
        >
          {selectedPlace && (
            <option value="" disabled>
              {selectedPlace.name || label('地点未细分', 'Unspecified')}
            </option>
          )}
          {years.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
          <option value="Total">{label('全部年份', 'All years')}</option>
        </select>
      </div>
      <section
        className={styles.hero}
        aria-label={label('里程与用时', 'Distance and time')}
      >
        <p>{label('总里程', 'Total distance')}</p>
        <div className={styles.distance}>
          <strong>{distance(stats.distanceMeters)}</strong>
          <span>{DIST_UNIT}</span>
        </div>
        <div className={styles.heroFoot}>
          <span>{label('累计用时', 'Moving time')}</span>
          <strong>{formatDurationCompact(stats.movingSeconds)}</strong>
        </div>
      </section>
      <dl className={styles.metrics}>
        {metrics.map(([name, value, unit]) => (
          <div key={name}>
            <dt>{name}</dt>
            <dd>
              <strong>{value}</strong>
              <span>{unit}</span>
            </dd>
          </div>
        ))}
      </dl>
      <section className={styles.locations} aria-labelledby="places-heading">
        <div className={styles.scope}>
          <h2 id="places-heading">{label('跑过的地方', 'Places visited')}</h2>
        </div>
        <dl className={styles.coverage}>
          {[
            [label('国家', 'Countries'), locations.countryCount],
            [label('国内省份', 'China provinces'), locations.provinceCount],
            [label('国内城市', 'China cities'), locations.cityCount],
          ].map(([name, count]) => (
            <div key={name}>
              <dd>{count}</dd>
              <dt>{name}</dt>
            </div>
          ))}
        </dl>
        <div className={styles.placeList}>
          <PlaceRows
            nodes={locations.countries}
            selectedKey={selectedPlace?.key}
            onSelect={onSelectPlace}
          />
        </div>
      </section>
    </div>
  );
};

export default DashboardSidebar;
