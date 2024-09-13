import Stat from '@/components/Stat';
import useActivities from '@/hooks/useActivities';

// only support China for now
const LocationSummary = () => {
  const { years, countries, provinces, cities } = useActivities();
  return (
    <div className="cursor-pointer">
      <section>
        {/* {years ? <Stat value={`${years.length}`} description=" 年里我跑过" /> : null}
        {countries ? <Stat value={countries.length} description=" 个国家" /> : null}
        {provinces ? <Stat value={provinces.length} description=" 个省份" /> : null}
        {cities ? (
          <Stat value={Object.keys(cities).length} description=" 个城市" />
        ) : null} */}
        {years ? <Stat value={`${years.length}`} description=" 年里我跑过" /> : null}
        {/* 中国 新加坡 */}
        {countries ? <Stat value={2} description=" 个国家" /> : null}
        {/* 北京 辽宁 湖南 湖北 新加坡 重庆 */}
        {provinces ? <Stat value={6} description=" 个省份" /> : null}
        {/* 北京 丹东 长沙 武汉 新加坡 重庆 */}
        {cities ? (
          <Stat value={6} description=" 个城市" />
        ) : null}
      </section>
      <hr color="red" />
    </div>
  );
};

export default LocationSummary;
