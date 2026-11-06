import { useId, useState } from 'react';
import useActivities from '@/hooks/useActivities';
import { DIST_UNIT, M_TO_DIST } from '@/utils/utils';
import styles from './style.module.css';

const VISIBLE_CITY_COUNT = 6;

// only support China for now
const CitiesStat = ({ onClick }: { onClick: (_city: string) => void }) => {
  const { cities } = useActivities();
  const [expanded, setExpanded] = useState(false);
  const listId = useId();

  const citiesArr = Object.entries(cities);
  citiesArr.sort((a, b) => b[1] - a[1]);
  const visibleCities = expanded
    ? citiesArr
    : citiesArr.slice(0, VISIBLE_CITY_COUNT);
  return (
    <section className={styles.cities} aria-label="跑过的地方">
      <h2 className={styles.heading}>
        跑过的地方 <span>{citiesArr.length} 个城市</span>
      </h2>
      <div id={listId} className={styles.cityList}>
        {visibleCities.map(([city, distance]) => (
          <button
            type="button"
            key={city}
            className={styles.cityButton}
            onClick={() => onClick(city)}
          >
            <span>{city}</span>
            <span>
              {(distance / M_TO_DIST).toFixed(0)} {DIST_UNIT}
            </span>
          </button>
        ))}
      </div>
      {citiesArr.length > VISIBLE_CITY_COUNT && (
        <button
          type="button"
          className={styles.toggle}
          aria-expanded={expanded}
          aria-controls={listId}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded
            ? '收起城市列表 ↑'
            : `展开全部 ${citiesArr.length} 个城市 ↓`}
        </button>
      )}
    </section>
  );
};

export default CitiesStat;
