import useActivities from '@/hooks/useActivities';
import styles from './style.module.css';

// only support China for now
const LocationSummary = () => {
  const { years, countries, provinces, cities } = useActivities();
  return (
    <dl className={styles.summary}>
      {[
        [years.length, '年里我跑过'],
        [countries.length, '个国家'],
        [provinces.length, '个省份'],
        [Object.keys(cities).length, '个城市'],
      ].map(([value, label]) => (
        <div key={label} className={styles.metric}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
};

export default LocationSummary;
