import countryNames from '@/static/country_names.json';
import { chinaCities, COUNTRY_STANDARDIZATION } from '@/static/city';
import type { Activity } from './utils';

export interface PlaceNode {
  key: string;
  name: string;
  level: 'country' | 'province' | 'city';
  countryCode: string | null;
  known: boolean;
  distanceMeters: number;
  runCount: number;
  children: PlaceNode[];
}

export interface LocationHierarchy {
  countries: PlaceNode[];
  countryCount: number;
  provinceCount: number;
  cityCount: number;
  unlocatedCount: number;
  unlocatedDistanceMeters: number;
}

interface RunLocation {
  countryCode: string | null;
  countryName: string;
  province: string;
  city: string;
  keys: string[];
}

// Metadata mirrors world.zh.json, keeping polygon coordinates out of the initial bundle.
const countriesByName = new Map<string, { code: string; name: string }>();
const englishNames = new Intl.DisplayNames(['en'], { type: 'region' });
for (const [code, name, fullName] of countryNames) {
  if (!/^[A-Z]{2}$/.test(code)) continue;
  const country = { code, name };
  for (const alias of [code, name, fullName, englishNames.of(code)]) {
    if (alias) countriesByName.set(alias.toLowerCase(), country);
  }
}
for (const [alias, name] of [
  ...COUNTRY_STANDARDIZATION,
  ['中國', '中国'],
  ['法國', '法国'],
  ['比利時', '比利时'],
  ['荷蘭', '荷兰'],
  ['United States of America', '美国'],
] as const) {
  const country = countriesByName.get(name.toLowerCase());
  if (country) countriesByName.set(alias.toLowerCase(), country);
}

const provinces = chinaCities.filter((place) => !place.city);
const cities = chinaCities.filter((place) => place.city);
const municipalities = new Set(['北京市', '上海市', '天津市', '重庆市']);
const locationCache = new Map<string, RunLocation>();

export const parseActivityLocation = (
  run: Pick<Activity, 'location_country'>
): RunLocation => {
  const address = run.location_country?.trim() ?? '';
  const cached = locationCache.get(address);
  if (cached) return cached;

  const parts = address
    .split(/[,，]/)
    .map((part) => part.trim())
    .filter(Boolean);
  // Geocoded addresses end with the country, occasionally followed by a postal code.
  let country: { code: string; name: string } | undefined;
  for (const part of parts.slice(-2).reverse()) {
    country = part
      .split(/[;；/／]/)
      .map((alias) => countriesByName.get(alias.trim().toLowerCase()))
      .find(Boolean);
    if (country) break;
  }
  const countryName = country?.name ?? '';
  let province = '';
  let city = '';
  if (country?.code === 'CN') {
    // Prefer the explicitly named prefecture over a district/county in the same address.
    const cityInfo = cities.find((place) => parts.includes(place.name));
    const provinceInfo =
      provinces.find((place) => parts.includes(place.name)) ??
      provinces.find(
        (place) => cityInfo && place.province === cityInfo.province
      );
    province = provinceInfo?.name ?? '';
    city = municipalities.has(province)
      ? province
      : (cityInfo?.name ??
        parts
          .slice()
          .reverse()
          .find((part) =>
            /^[\u4e00-\u9fff]+(?:市|自治州|地区|盟)$/.test(part)
          ) ??
        '');
  }
  const keys: string[] = [];
  if (country) {
    keys.push(country.code);
    if (country.code === 'CN') {
      keys.push(`${country.code}/${encodeURIComponent(province || '?')}`);
      keys.push(`${keys[1]}/${encodeURIComponent(city || '?')}`);
    }
  }
  const location = {
    countryCode: country?.code ?? null,
    countryName,
    province,
    city,
    keys,
  };
  locationCache.set(address, location);
  return location;
};

export const filterLocationRuns = (run: Activity, key: string): boolean =>
  parseActivityLocation(run).keys.includes(key);

export const buildLocationHierarchy = (
  activities: Activity[]
): LocationHierarchy => {
  const result: LocationHierarchy = {
    countries: [],
    countryCount: 0,
    provinceCount: 0,
    cityCount: 0,
    unlocatedCount: 0,
    unlocatedDistanceMeters: 0,
  };
  const nodes = new Map<string, PlaceNode>();
  for (const run of activities) {
    const location = parseActivityLocation(run);
    const distance = run.distance || 0;
    if (!location.countryCode) {
      result.unlocatedCount += 1;
      result.unlocatedDistanceMeters += distance;
      continue;
    }
    const names = [location.countryName, location.province, location.city];
    const levels = ['country', 'province', 'city'] as const;
    let siblings = result.countries;
    location.keys.forEach((key, index) => {
      let node = nodes.get(key);
      if (!node) {
        node = {
          key,
          name: names[index],
          level: levels[index],
          countryCode: location.countryCode,
          known: Boolean(names[index]),
          distanceMeters: 0,
          runCount: 0,
          children: [],
        };
        nodes.set(key, node);
        siblings.push(node);
        if (node.known) result[`${levels[index]}Count`] += 1;
      }
      node.distanceMeters += distance;
      node.runCount += 1;
      siblings = node.children;
    });
  }
  const sortNodes = (siblings: PlaceNode[]) => {
    siblings.sort(
      (a, b) =>
        Number(b.known) - Number(a.known) ||
        b.distanceMeters - a.distanceMeters ||
        a.name.localeCompare(b.name)
    );
    siblings.forEach((node) => sortNodes(node.children));
  };
  sortNodes(result.countries);
  return result;
};
