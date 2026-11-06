import useActivities from '@/hooks/useActivities';
import styles from './style.module.css';

const PeriodStat = ({ onClick }: { onClick: (_period: string) => void }) => {
  const { runPeriod } = useActivities();

  const periodArr = Object.entries(runPeriod);
  periodArr.sort((a, b) => b[1] - a[1]);
  return (
    <section className={styles.periods} aria-label="运动时段">
      {periodArr.map(([period, times]) => (
        <button type="button" key={period} onClick={() => onClick(period)}>
          {period} <span>{times} Runs</span>
        </button>
      ))}
    </section>
  );
};

export default PeriodStat;
