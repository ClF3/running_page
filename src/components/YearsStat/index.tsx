import { useMemo } from 'react';
import YearStat from '@/components/YearStat';
import useActivities from '@/hooks/useActivities';
import { INFO_MESSAGE, IS_CHINESE } from '@/utils/const';
import styles from './style.module.css';

const YearsStat = ({
  year,
  onClick,
}: {
  year: string;
  onClick: (_year: string) => void;
}) => {
  const { years } = useActivities();

  // Memoize the years array calculation
  const yearsArrayUpdate = useMemo(() => {
    // make sure the year click on front
    let updatedYears = years.slice();
    updatedYears.push('Total');
    updatedYears = updatedYears.filter((x) => x !== year);
    updatedYears.unshift(year);
    return updatedYears;
  }, [years, year]);

  const infoMessage = useMemo(() => {
    return INFO_MESSAGE(years.length, year);
  }, [years.length, year]);

  // for short solution need to refactor
  return (
    <div>
      <p className={styles.intro}>{infoMessage}</p>
      <YearStat key={year} year={year} onClick={onClick} />
      <details className={styles.history}>
        <summary>
          {IS_CHINESE ? '其他年份与累计' : 'Other years & all time'}
        </summary>
        {yearsArrayUpdate
          .filter((yearItem) => yearItem !== year)
          .map((yearItem) => (
            <YearStat key={yearItem} year={yearItem} onClick={onClick} />
          ))}
      </details>
    </div>
  );
};

export default YearsStat;
