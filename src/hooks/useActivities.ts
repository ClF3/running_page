import { useEffect, useMemo, useSyncExternalStore } from 'react';
import type { Activity } from '@/utils/utils';
import { locationForRun, titleForRun } from '@/utils/utils';
import manifestUrl from '@/static/activity_chunks/manifest.json?url';
import { COUNTRY_STANDARDIZATION } from '@/static/city';

interface ActivityChunkYear {
  year: string;
  file: string;
  count: number;
  run_ids: number[];
  first_start_date_local: string | null;
  last_start_date_local: string | null;
}

interface ActivityChunkManifest {
  version: number;
  total_count: number;
  years: ActivityChunkYear[];
}

interface ProcessedActivities {
  activities: Activity[];
  years: string[];
  countries: string[];
  provinces: string[];
  cities: Record<string, number>;
  runPeriod: Record<string, number>;
  thisYear: string;
  loadedYears: string[];
  loadingYears: string[];
  isComplete: boolean;
  loadYear: (_year: string) => Promise<Activity[]>;
  loadAll: () => Promise<void>;
  yearForRunId: (_runId: number) => string | null;
}

type UseActivitiesMode = 'progressive' | 'all';

const activityChunkUrls = import.meta.glob<string>(
  '../static/activity_chunks/year_*.json',
  {
    query: '?url',
    import: 'default',
    eager: true,
  }
);

const standardizeCountryName = (country: string): string => {
  for (const [pattern, standardName] of COUNTRY_STANDARDIZATION) {
    if (country.includes(pattern)) {
      return standardName;
    }
  }
  return country;
};

const sortActivitiesByLocalStartTime = (activities: Activity[]) =>
  activities.slice().sort((a, b) => {
    return (
      new Date(a.start_date_local.replace(' ', 'T')).getTime() -
      new Date(b.start_date_local.replace(' ', 'T')).getTime()
    );
  });

let activityManifestCache: ActivityChunkManifest | null = null;
let activityManifestError: unknown = null;
let activityManifestPromise: Promise<ActivityChunkManifest> | null = null;

let loadedActivityChunks = new Map<string, Activity[]>();
let activityChunkPromises = new Map<string, Promise<Activity[]>>();
let activityChunkErrors = new Map<string, unknown>();
let backgroundPrefetchPromise: Promise<void> | null = null;
let mergedActivitiesCache: {
  cacheKey: string;
  activities: Activity[];
} | null = null;
let processedActivitiesCache: {
  activityData: Activity[];
  manifest: ActivityChunkManifest;
  processedActivities: ProcessedActivities;
} | null = null;

let activityStoreVersion = 0;
const activityStoreListeners = new Set<() => void>();

const notifyActivityStore = () => {
  processedActivitiesCache = null;
  activityStoreVersion += 1;
  activityStoreListeners.forEach((listener) => listener());
};

const subscribeToActivityStore = (listener: () => void) => {
  activityStoreListeners.add(listener);
  return () => activityStoreListeners.delete(listener);
};

const getActivityStoreSnapshot = () => activityStoreVersion;

const loadActivityManifest = () => {
  activityManifestPromise ??= fetch(manifestUrl)
    .then((response) => {
      if (!response.ok) {
        throw new Error(
          `Failed to load activity chunks manifest: ${response.status}`
        );
      }
      return response.json() as Promise<ActivityChunkManifest>;
    })
    .then((manifest) => {
      activityManifestCache = manifest;
      activityManifestError = null;
      notifyActivityStore();
      return manifest;
    })
    .catch((error: unknown) => {
      activityManifestError = error;
      notifyActivityStore();
      throw error;
    });

  return activityManifestPromise;
};

const getActivityManifest = () => {
  if (activityManifestError) throw activityManifestError;
  if (activityManifestCache) return activityManifestCache;
  throw loadActivityManifest();
};

const chunkUrlForYear = (year: string) => {
  const chunkFile =
    activityManifestCache?.years.find((yearInfo) => yearInfo.year === year)
      ?.file ?? `year_${year}.json`;
  const chunkPath = `../static/activity_chunks/${chunkFile}`;
  const chunkUrl = activityChunkUrls[chunkPath];
  if (!chunkUrl) {
    throw new Error(`Missing activity chunk for year ${year}`);
  }
  return chunkUrl;
};

const loadActivityYear = (year: string) => {
  const loadedActivities = loadedActivityChunks.get(year);
  if (loadedActivities) return Promise.resolve(loadedActivities);

  const existingPromise = activityChunkPromises.get(year);
  if (existingPromise) return existingPromise;

  const existingError = activityChunkErrors.get(year);
  if (existingError) return Promise.reject(existingError);

  const chunkUrl = chunkUrlForYear(year);
  const activityChunkPromise = fetch(chunkUrl)
    .then((response) => {
      if (!response.ok) {
        throw new Error(
          `Failed to load activity chunk ${year}: ${response.status}`
        );
      }
      return response.json() as Promise<Activity[]>;
    })
    .then((activityData) => {
      loadedActivityChunks.set(year, activityData);
      activityChunkPromises.delete(year);
      activityChunkErrors.delete(year);
      mergedActivitiesCache = null;
      notifyActivityStore();
      return activityData;
    })
    .catch((error: unknown) => {
      activityChunkPromises.delete(year);
      activityChunkErrors.set(year, error);
      notifyActivityStore();
      throw error;
    });

  activityChunkPromises.set(year, activityChunkPromise);
  notifyActivityStore();
  return activityChunkPromise;
};

const loadedYearsForManifest = (manifest: ActivityChunkManifest) =>
  manifest.years
    .map((yearInfo) => yearInfo.year)
    .filter((year) => loadedActivityChunks.has(year));

const loadingYearsForManifest = (manifest: ActivityChunkManifest) =>
  manifest.years
    .map((yearInfo) => yearInfo.year)
    .filter((year) => activityChunkPromises.has(year));

const isManifestComplete = (manifest: ActivityChunkManifest) =>
  manifest.years.every((yearInfo) => loadedActivityChunks.has(yearInfo.year));

const firstChunkErrorForManifest = (manifest: ActivityChunkManifest) => {
  for (const yearInfo of manifest.years) {
    const error = activityChunkErrors.get(yearInfo.year);
    if (error) return error;
  }
  return null;
};

const loadAllActivityYears = (manifest: ActivityChunkManifest) => {
  const chunkError = firstChunkErrorForManifest(manifest);
  if (chunkError) return Promise.reject(chunkError);

  return Promise.all(
    manifest.years.map((yearInfo) => loadActivityYear(yearInfo.year))
  ).then(() => undefined);
};

const prefetchRemainingActivityYears = async (
  manifest: ActivityChunkManifest
) => {
  for (const yearInfo of manifest.years.slice(1)) {
    if (loadedActivityChunks.has(yearInfo.year)) continue;
    await loadActivityYear(yearInfo.year);
  }
};

const startBackgroundPrefetch = (manifest: ActivityChunkManifest) => {
  backgroundPrefetchPromise ??= prefetchRemainingActivityYears(manifest).catch(
    () => undefined
  );
  return backgroundPrefetchPromise;
};

const getLoadedActivityData = (manifest: ActivityChunkManifest) => {
  const cacheKey = manifest.years
    .map((yearInfo) => {
      const yearActivities = loadedActivityChunks.get(yearInfo.year);
      return `${yearInfo.year}:${yearActivities?.length ?? 0}`;
    })
    .join('|');

  if (mergedActivitiesCache?.cacheKey === cacheKey) {
    return mergedActivitiesCache.activities;
  }

  const activities = sortActivitiesByLocalStartTime(
    manifest.years.flatMap(
      (yearInfo) => loadedActivityChunks.get(yearInfo.year) ?? []
    )
  );
  mergedActivitiesCache = { cacheKey, activities };
  return activities;
};

export const findActivityYearForRunId = (
  manifest: ActivityChunkManifest,
  runId: number
) => {
  const yearInfo = manifest.years.find((item) => item.run_ids.includes(runId));
  return yearInfo?.year ?? null;
};

const processActivities = (
  activityData: Activity[],
  manifest: ActivityChunkManifest
): ProcessedActivities => {
  const cities: Record<string, number> = {};
  const runPeriod: Record<string, number> = {};
  const provinces: Set<string> = new Set();
  const countries: Set<string> = new Set();
  const yearsArray = manifest.years.map((yearInfo) => yearInfo.year);

  activityData.forEach((run) => {
    const location = locationForRun(run);

    const periodName = titleForRun(run);
    if (periodName) {
      runPeriod[periodName] = runPeriod[periodName]
        ? runPeriod[periodName] + 1
        : 1;
    }

    const { city, province, country } = location;
    // drop only one char city
    if (city.length > 1) {
      cities[city] = cities[city] ? cities[city] + run.distance : run.distance;
    }
    if (province) provinces.add(province);
    if (country) countries.add(standardizeCountryName(country));
  });

  return {
    activities: activityData,
    years: yearsArray,
    countries: [...countries],
    provinces: [...provinces],
    cities,
    runPeriod,
    thisYear: yearsArray[0] || '',
    loadedYears: loadedYearsForManifest(manifest),
    loadingYears: loadingYearsForManifest(manifest),
    isComplete: isManifestComplete(manifest),
    loadYear: loadActivityYear,
    loadAll: () => loadAllActivityYears(manifest),
    yearForRunId: (runId: number) => findActivityYearForRunId(manifest, runId),
  };
};

const getProcessedActivities = (
  activityData: Activity[],
  manifest: ActivityChunkManifest
) => {
  if (
    processedActivitiesCache?.activityData === activityData &&
    processedActivitiesCache.manifest === manifest
  ) {
    return processedActivitiesCache.processedActivities;
  }

  const processedActivities = processActivities(activityData, manifest);
  processedActivitiesCache = { activityData, manifest, processedActivities };
  return processedActivities;
};

const ensureInitialActivitiesLoaded = (
  manifest: ActivityChunkManifest,
  mode: UseActivitiesMode
) => {
  if (mode === 'all') {
    const chunkError = firstChunkErrorForManifest(manifest);
    if (chunkError) throw chunkError;
    if (!isManifestComplete(manifest)) {
      throw loadAllActivityYears(manifest);
    }
    return;
  }

  const latestYear = manifest.years[0]?.year;
  const latestYearError = latestYear
    ? activityChunkErrors.get(latestYear)
    : null;
  if (latestYearError) throw latestYearError;
  if (latestYear && !loadedActivityChunks.has(latestYear)) {
    throw loadActivityYear(latestYear);
  }
};

const useActivities = (mode: UseActivitiesMode = 'progressive') => {
  const storeVersion = useSyncExternalStore(
    subscribeToActivityStore,
    getActivityStoreSnapshot,
    getActivityStoreSnapshot
  );
  const manifest = getActivityManifest();
  ensureInitialActivitiesLoaded(manifest, mode);

  useEffect(() => {
    if (mode === 'progressive') {
      startBackgroundPrefetch(manifest);
    }
  }, [manifest, mode]);

  const activityData = getLoadedActivityData(manifest);
  return useMemo(() => {
    void storeVersion;
    return getProcessedActivities(activityData, manifest);
  }, [activityData, manifest, storeVersion]);
};

export const __activityLoaderTest = {
  findActivityYearForRunId,
  getLoadedActivityData,
  loadActivityManifest,
  loadActivityYear,
  loadAllActivityYears,
  prefetchRemainingActivityYears,
  reset: () => {
    activityManifestCache = null;
    activityManifestError = null;
    activityManifestPromise = null;
    loadedActivityChunks = new Map<string, Activity[]>();
    activityChunkPromises = new Map<string, Promise<Activity[]>>();
    activityChunkErrors = new Map<string, unknown>();
    backgroundPrefetchPromise = null;
    mergedActivitiesCache = null;
    processedActivitiesCache = null;
    notifyActivityStore();
  },
};

export default useActivities;
