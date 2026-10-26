import assert from 'node:assert/strict';
import path from 'node:path';
import { readFileSync, readdirSync } from 'node:fs';
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
let weeklyRuns;
let locations;
let dashboardStats;

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
  weeklyRuns = await server.ssrLoadModule('/src/utils/weeklyRuns.ts');
  locations = await server.ssrLoadModule('/src/utils/locations.ts');
  dashboardStats = await server.ssrLoadModule('/src/utils/dashboardStats.ts');
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
  const indoorWithoutRoute = run({
    run_id: 3,
    subtype: 'indoor',
    summary_polyline: null,
  });
  const pathForRun = geoUtils.pathForRun(outdoor);
  const geoJson = geoUtils.geoJsonForRuns([
    outdoor,
    indoor,
    indoorWithoutRoute,
  ]);

  assert.deepEqual(pathForRun[0], [-120.2, 38.5]);
  assert.equal(geoJson.type, 'FeatureCollection');
  assert.equal(geoJson.features.length, 2);
  assert.equal(geoJson.features[0].geometry.type, 'LineString');
  assert.equal(geoJson.features[1].properties.indoor, true);

  const bounds = geoUtils.getBoundsForGeoData(geoJson);
  assert.equal(typeof bounds.longitude, 'number');
  assert.equal(typeof bounds.latitude, 'number');
  assert.equal(bounds.zoom, 11.5);

  const emptyGeoJson = geoUtils.geoJsonForRuns([indoorWithoutRoute]);
  assert.equal(emptyGeoJson.features.length, 0);
  assert.deepEqual(geoUtils.getBoundsForGeoData(emptyGeoJson), {
    longitude: 20,
    latitude: 20,
    zoom: 3,
  });
});

test('weekly run summaries group activities by monday-starting weeks', () => {
  const activities = [
    run({
      run_id: 1,
      distance: 10_000,
      moving_time: '1:00:00',
      elevation_gain: 10,
      start_date_local: '2026-01-01 08:00:00',
    }),
    run({
      run_id: 2,
      distance: 5_000,
      moving_time: '0:30:00',
      elevation_gain: 5,
      start_date_local: '2026-01-04 08:00:00',
    }),
    run({
      run_id: 3,
      distance: 7_000,
      moving_time: '0:42:00',
      elevation_gain: 12,
      start_date_local: '2026-01-05 08:00:00',
    }),
  ];

  const weeks = weeklyRuns.createWeeklyRunSummaries(activities, '2026');
  const activeWeeks = weeks.filter((week) => week.runCount > 0);

  assert.equal(weeklyRuns.getActivityWeekKey(activities[0]), '2025-12-29');
  assert.deepEqual(
    activeWeeks.map((week) => week.runIds),
    [[1, 2], [3]]
  );
  assert.equal(activeWeeks[0].distanceMeters, 15_000);
  assert.equal(
    weeklyRuns.formatDurationCompact(activeWeeks[0].movingSeconds),
    '1h30m'
  );
});

const jsonResponse = (body) => ({
  ok: true,
  status: 200,
  json: async () => body,
});

test('places normalize country aliases and group municipality districts as one city', () => {
  const activities = [
    run({ location_country: '海淀区, 北京市, 100084, 中国' }),
    run({ location_country: '朝阳区, 北京市, 中国' }),
    run({ location_country: '公园, 南京市, 江苏省, 中国' }),
    run({ location_country: 'Brussels, 1060, 比利时;比利時' }),
    run({ location_country: 'Brussels, Belgium' }),
    run({ location_country: '巴黎, 法國本土, 75001, 法国;法國' }),
    run({ location_country: '海牙, 荷兰 / 荷蘭' }),
    run({ location_country: '新加坡, 12345, 新加坡' }),
    run({ location_country: '' }),
  ];
  const hierarchy = locations.buildLocationHierarchy(activities);
  assert.equal(hierarchy.countryCount, 5);
  assert.equal(hierarchy.provinceCount, 2);
  assert.equal(hierarchy.cityCount, 2);
  assert.equal(hierarchy.unlocatedCount, 1);
  assert.equal(hierarchy.unlocatedDistanceMeters, 5000);
  const china = hierarchy.countries.find(
    (country) => country.countryCode === 'CN'
  );
  assert.equal(china.distanceMeters, 15000);
  assert.equal(china.children[0].name, '北京市');
  assert.equal(china.children[0].children[0].name, '北京市');
  assert.equal(china.children[0].children[0].distanceMeters, 10000);
  assert.equal(locations.filterLocationRuns(activities[0], china.key), true);
  assert.equal(
    locations.filterLocationRuns(activities[2], china.children[0].key),
    false
  );
  assert.equal(locations.filterLocationRuns(activities[4], 'BE'), true);
  assert.equal(
    hierarchy.countries.find((country) => country.countryCode === 'BE')
      .distanceMeters,
    10000
  );
});

test('place aggregation retains partially located mileage without counting unknown cities', () => {
  const hierarchy = locations.buildLocationHierarchy([
    run({ location_country: '辽宁省, 中国', distance: 1000 }),
    run({ location_country: '东港市, 丹东市, 辽宁省, 中国', distance: 2000 }),
    run({ location_country: '东港市, 辽宁省, 中国', distance: 3000 }),
    run({ location_country: '中国', distance: 4000 }),
  ]);
  assert.equal(hierarchy.countryCount, 1);
  assert.equal(hierarchy.provinceCount, 1);
  assert.equal(hierarchy.cityCount, 2);
  const china = hierarchy.countries[0];
  assert.equal(china.distanceMeters, 10000);
  assert.equal(
    china.children.reduce((sum, province) => sum + province.distanceMeters, 0),
    10000
  );
  assert.deepEqual(
    china.children[0].children.map((city) => city.name),
    ['东港市', '丹东市', '']
  );
  assert.equal(
    china.children[0].children.reduce(
      (sum, city) => sum + city.distanceMeters,
      0
    ),
    6000
  );
});

test('all visited countries in the real data match unique world map polygons', () => {
  const root = path.resolve('src/static/activity_chunks');
  const activities = readdirSync(root)
    .filter((file) => /^year_.*\.json$/.test(file))
    .flatMap((file) => JSON.parse(readFileSync(path.join(root, file), 'utf8')));
  const world = JSON.parse(readFileSync('src/static/world.zh.json', 'utf8'));
  const hierarchy = locations.buildLocationHierarchy(activities);
  for (const country of hierarchy.countries) {
    assert.equal(
      world.features.filter(
        (feature) => feature.properties.iso_a2 === country.countryCode
      ).length,
      1,
      country.name
    );
  }
  const totalDistance = activities.reduce(
    (sum, activity) => sum + activity.distance,
    0
  );
  const locatedDistance = hierarchy.countries.reduce(
    (sum, country) => sum + country.distanceMeters,
    0
  );
  assert.ok(
    Math.abs(
      totalDistance - locatedDistance - hierarchy.unlocatedDistanceMeters
    ) < 0.001
  );
  assert.equal(
    hierarchy.unlocatedCount,
    activities.filter((activity) => !activity.location_country).length
  );
});

test('dashboard summaries exclude missing sensors and count consecutive calendar days in scope', () => {
  const summary = dashboardStats.summarizeDashboard([
    run({
      average_speed: 5,
      distance: 6000,
      average_heartrate: 160,
      streak: 99,
    }),
    run({
      start_date_local: '2026-01-01 18:00:00',
      average_speed: 0,
      average_heartrate: null,
    }),
    run({
      start_date_local: '2026-01-02 08:00:00',
      average_speed: 2.5,
      distance: 3000,
      average_heartrate: 140,
    }),
    run({
      start_date_local: '2026-01-04 08:00:00',
      average_speed: 0,
      average_heartrate: 0,
    }),
  ]);
  assert.equal(summary.runCount, 4);
  assert.equal(summary.longestStreak, 2);
  assert.equal(summary.heartRate, '150');
  assert.equal(summary.pace, utils.formatPace(9000 / 2400));
  const empty = dashboardStats.summarizeDashboard([]);
  assert.equal(empty.pace, '—');
  assert.equal(empty.heartRate, '—');
  assert.equal(empty.longestStreak, 0);
});

test('tiny-country markers keep Singapore visible without using overseas territory centroids', () => {
  const world = JSON.parse(readFileSync('src/static/world.zh.json', 'utf8'));
  world.features.forEach((feature) => {
    feature.properties.boundary = 'country';
  });
  const markers = geoUtils.smallCountryMarkers(world);
  const singapore = markers.features.find(
    (feature) => feature.properties.iso_a2 === 'SG'
  );
  assert.ok(singapore);
  assert.ok(
    singapore.geometry.coordinates[0] > 103 &&
      singapore.geometry.coordinates[0] < 104
  );
  assert.ok(
    singapore.geometry.coordinates[1] > 1 &&
      singapore.geometry.coordinates[1] < 2
  );
  assert.equal(
    markers.features.some((feature) => feature.properties.iso_a2 === 'FR'),
    false
  );
  const metadata = JSON.parse(
    readFileSync('src/static/country_names.json', 'utf8')
  );
  assert.deepEqual(
    metadata,
    world.features.map(({ properties }) => [
      properties.iso_a2,
      properties.name,
      properties.full_name,
    ])
  );
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
    loader
      .getLoadedActivityData(loadedManifest)
      .map((activity) => activity.run_id),
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
    loader
      .getLoadedActivityData(loadedManifest)
      .map((activity) => activity.run_id),
    [1, 2, 3]
  );
  assert.equal(loader.findActivityYearForRunId(loadedManifest, 2), '2025');
  assert.equal(loader.findActivityYearForRunId(loadedManifest, 999), null);
});
