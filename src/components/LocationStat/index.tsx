import YearStat from '@/components/YearStat';
import { CHINESE_LOCATION_INFO_MESSAGE } from '@/utils/const';
import CitiesStat from './CitiesStat';
import LocationSummary from './LocationSummary';
import PeriodStat from './PeriodStat';
import styles from './style.module.css';

interface ILocationStatProps {
  changeYear: (_year: string) => void;
  changeCity: (_city: string) => void;
  changeTitle: (_title: string) => void;
}

const LocationStat = ({
  changeYear,
  changeCity,
  changeTitle,
}: ILocationStatProps) => (
  <div>
    <p className={styles.intro}>{CHINESE_LOCATION_INFO_MESSAGE}</p>
    <LocationSummary />
    <CitiesStat onClick={changeCity} />
    <PeriodStat onClick={changeTitle} />
    <YearStat year="Total" onClick={changeYear} />
  </div>
);

export default LocationStat;
