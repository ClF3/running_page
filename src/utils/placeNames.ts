import { IS_CHINESE } from './const';
import type { PlaceNode } from './locations';

const countryNames = new Intl.DisplayNames(['en'], { type: 'region' });

// Display names are separate from canonical addresses and map boundary names.
const englishPlaceNames: Record<string, string> = {
  北京市: 'Beijing',
  天津市: 'Tianjin',
  河北省: 'Hebei',
  山西省: 'Shanxi',
  内蒙古自治区: 'Inner Mongolia',
  辽宁省: 'Liaoning',
  吉林省: 'Jilin',
  黑龙江省: 'Heilongjiang',
  上海市: 'Shanghai',
  江苏省: 'Jiangsu',
  浙江省: 'Zhejiang',
  安徽省: 'Anhui',
  福建省: 'Fujian',
  江西省: 'Jiangxi',
  山东省: 'Shandong',
  河南省: 'Henan',
  湖北省: 'Hubei',
  湖南省: 'Hunan',
  广东省: 'Guangdong',
  广西壮族自治区: 'Guangxi',
  海南省: 'Hainan',
  重庆市: 'Chongqing',
  四川省: 'Sichuan',
  贵州省: 'Guizhou',
  云南省: 'Yunnan',
  西藏自治区: 'Tibet',
  陕西省: 'Shaanxi',
  甘肃省: 'Gansu',
  青海省: 'Qinghai',
  宁夏回族自治区: 'Ningxia',
  新疆维吾尔自治区: 'Xinjiang',
  香港特别行政区: 'Hong Kong',
  澳门特别行政区: 'Macao',
  台湾省: 'Taiwan',
  丹东市: 'Dandong',
  东港市: 'Donggang',
  盘锦市: 'Panjin',
  大连市: 'Dalian',
  南京市: 'Nanjing',
  扬州市: 'Yangzhou',
  石家庄市: 'Shijiazhuang',
  张家口市: 'Zhangjiakou',
  成都市: 'Chengdu',
  厦门市: 'Xiamen',
  西安市: "Xi'an",
  华阴市: 'Huayin',
  太原市: 'Taiyuan',
  忻州市: 'Xinzhou',
  武汉市: 'Wuhan',
  杭州市: 'Hangzhou',
  佛山市: 'Foshan',
  芜湖市: 'Wuhu',
  长沙市: 'Changsha',
  玉溪市: 'Yuxi',
  呼伦贝尔市: 'Hulunbuir',
};

export const getPlaceDisplayName = (
  place: Pick<PlaceNode, 'name' | 'level' | 'countryCode'>
): string => {
  if (!place.name) return IS_CHINESE ? '地点未细分' : 'Unspecified';
  if (IS_CHINESE) return place.name;
  if (place.level === 'country' && place.countryCode) {
    return countryNames.of(place.countryCode) ?? place.name;
  }
  return englishPlaceNames[place.name] ?? place.name;
};
