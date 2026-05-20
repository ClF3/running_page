import assert from 'node:assert/strict';
import path from 'node:path';
import { after, before, test } from 'node:test';

import { createServer } from 'vite';

globalThis.window = {
  innerWidth: 1024,
  addEventListener() {},
  removeEventListener() {},
};
globalThis.document = {
  documentElement: {
    getAttribute() {
      return 'dark';
    },
  },
  getElementById() {
    return null;
  },
};
globalThis.localStorage = {
  getItem() {
    return null;
  },
};
Object.defineProperty(globalThis, 'navigator', {
  value: { maxTouchPoints: 0 },
  configurable: true,
});

let server;
let utils;
let geoUtils;
let activitiesHook;

const run = (overrides = {}) => ({
  run_id: 1,
  name: '',
  distance: 5000,
  moving_time: '00:25:00',
  type: 'Run',
  subtype: 'generic',
  start_date: '2026-01-01 00:00:00',
  start_date_local: '2026-01-01 08:00:00',
  location_country: '高能街, 大连市, 辽宁省, 116026, 中国',
  summary_polyline: '_p~iF~ps|U_ulLnnqC_mqNvxq`@',
  average_heartrate: 150,
  elevation_gain: 10,
  average_speed: 3.33,
  streak: 1,
  ...overrides,
});

before(async () => {
  server = await createServer({
    configFile: path.resolve('vite.config.ts'),
    appType: 'custom',
    logLevel: 'error',
    resolve: {
      alias: [
        {
          find: '@mapbox/polyline',
          replacement: path.resolve('tests/frontend/polyline-shim.mjs'),
        },
        { find: '@assets', replacement: path.resolve('assets') },
        { find: '@', replacement: path.resolve('src') },
      ],
    },
    server: { middlewareMode: true, hmr: false, ws: false },
    ssr: { noExternal: ['gcoord', '@math.gl/web-mercator'] },
  });
  utils = await server.ssrLoadModule('/src/utils/utils.ts');
  geoUtils = await server.ssrLoadModule('/src/utils/geoUtils.ts');
  activitiesHook = await server.ssrLoadModule('/src/hooks/useActivities.ts');
});

after(async () => {
  await server?.close();
});

test('formatting helpers parse paces, durations, and large numbers', () => {
  assert.equal(utils.convertMovingTime2Sec('1 days, 01:02:03'), 90123);
  assert.equal(utils.convertMovingTime2Sec('00:25:00'), 1500);
  assert.equal(utils.formatRunTime('00:25:30'), '25min');
  assert.equal(utils.formatPace(3.333333333), '5\'00"');
  assert.equal(utils.intComma(1234567), '1,234,567');
});

test('activity filters and sorting mirror the page contract', () => {
  const activities = [
    run({ run_id: 1, start_date_local: '2026-01-01 08:00:00' }),
    run({
      run_id: 2,
      start_date_local: '2025-12-31 18:00:00',
      location_country: 'Pier, San Francisco, California, United States',
    }),
  ];

  assert.equal(utils.filterYearRuns(activities[0], '2026'), true);
  assert.equal(utils.filterCityRuns(activities[0], '大连市'), true);

  const filtered = utils.filterAndSortRuns(
    activities,
    '2026',
    utils.filterYearRuns,
    utils.sortDateFunc
  );
  assert.deepEqual(
    filtered.map((activity) => activity.run_id),
    [1]
  );

  const sorted = activities.slice().sort(utils.sortDateFunc);
  assert.deepEqual(
    sorted.map((activity) => activity.run_id),
    [1, 2]
  );
});

test('title generation falls back to distance and time-of-day labels', () => {
  assert.equal(utils.titleForRun(run()), '清晨跑步');
  assert.equal(utils.titleForRun(run({ distance: 21_500 })), '半程马拉松');
  assert.equal(utils.titleForRun(run({ distance: 42_500 })), '全程马拉松');
});

test('geo utilities decode routes and produce route feature collections', () => {
  const outdoor = run();
  const indoor = run({ run_id: 2, subtype: 'indoor' });
  const pathForRun = geoUtils.pathForRun(outdoor);
  const geoJson = geoUtils.geoJsonForRuns([outdoor, indoor]);

  assert.deepEqual(pathForRun[0], [-120.2, 38.5]);
  assert.equal(geoJson.type, 'FeatureCollection');
  assert.equal(geoJson.features.length, 2);
  assert.equal(geoJson.features[0].geometry.type, 'LineString');
  assert.equal(geoJson.features[1].properties.indoor, true);

  const bounds = geoUtils.getBoundsForGeoData(geoJson);
  assert.equal(typeof bounds.longitude, 'number');
  assert.equal(typeof bounds.latitude, 'number');
  assert.equal(bounds.zoom, 11.5);
});

const jsonResponse = (body) => ({
  ok: true,
  status: 200,
  json: async () => body,
});

test('activity loader fetches newest chunks first and merges loaded years', async (t) => {
  const loader = activitiesHook.__activityLoaderTest;
  const originalFetch = globalThis.fetch;
  const manifest = {
    version: 1,
    total_count: 3,
    years: [
      {
        year: '2026',
        file: 'year_2026.json',
        count: 1,
        run_ids: [3],
        first_start_date_local: '2026-01-01 08:00:00',
        last_start_date_local: '2026-01-01 08:00:00',
      },
      {
        year: '2025',
        file: 'year_2025.json',
        count: 1,
        run_ids: [2],
        first_start_date_local: '2025-01-01 08:00:00',
        last_start_date_local: '2025-01-01 08:00:00',
      },
      {
        year: '2024',
        file: 'year_2024.json',
        count: 1,
        run_ids: [1],
        first_start_date_local: '2024-01-01 08:00:00',
        last_start_date_local: '2024-01-01 08:00:00',
      },
    ],
  };
  const chunks = {
    'year_2024.json': [
      run({ run_id: 1, start_date_local: '2024-01-01 08:00:00' }),
    ],
    'year_2025.json': [
      run({ run_id: 2, start_date_local: '2025-01-01 08:00:00' }),
    ],
    'year_2026.json': [
      run({ run_id: 3, start_date_local: '2026-01-01 08:00:00' }),
    ],
  };
  const fetchCalls = [];

  loader.reset();
  globalThis.fetch = async (url) => {
    const href = String(url);
    fetchCalls.push(href);
    if (href.includes('manifest.json')) return jsonResponse(manifest);

    const chunkName = Object.keys(chunks).find((name) => href.includes(name));
    if (chunkName) return jsonResponse(chunks[chunkName]);

    return { ok: false, status: 404, json: async () => ({}) };
  };

  t.after(() => {
    globalThis.fetch = originalFetch;
    loader.reset();
  });

  const loadedManifest = await loader.loadActivityManifest();
  await loader.loadActivityYear(loadedManifest.years[0].year);

  assert.match(fetchCalls[0], /manifest\.json/);
  assert.match(fetchCalls[1], /year_2026\.json/);
  assert.deepEqual(
    loader.getLoadedActivityData(loadedManifest).map((activity) => activity.run_id),
    [3]
  );

  const yearPromise = loader.loadActivityYear('2025');
  assert.equal(loader.loadActivityYear('2025'), yearPromise);
  await yearPromise;
  assert.equal(
    fetchCalls.filter((url) => url.includes('year_2025.json')).length,
    1
  );

  await loader.prefetchRemainingActivityYears(loadedManifest);

  assert.deepEqual(
    loader.getLoadedActivityData(loadedManifest).map((activity) => activity.run_id),
    [1, 2, 3]
  );
  assert.equal(loader.findActivityYearForRunId(loadedManifest, 2), '2025');
  assert.equal(loader.findActivityYearForRunId(loadedManifest, 999), null);
});
