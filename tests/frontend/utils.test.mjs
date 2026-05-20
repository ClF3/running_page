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
