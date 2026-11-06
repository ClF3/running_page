import type { Plugin, Rolldown } from 'vite';

type ChunkWithCss = Rolldown.OutputChunk & {
  viteMetadata?: { importedCss: Set<string> };
};

// Generate hints from the actual build, so hashed URLs and the newest year stay
// in sync with automated activity updates. Do not traverse dynamic imports:
// summary charts, yearly SVGs and world boundaries remain demand-loaded.
export const dashboardPreloadScript = (
  bundle: Rolldown.OutputBundle,
  base: string
): string | undefined => {
  const files = Object.values(bundle);
  const page = files.find(
    (file): file is Rolldown.OutputChunk =>
      file.type === 'chunk' &&
      file.facadeModuleId
        ?.replaceAll('\\', '/')
        .endsWith('/src/pages/index.tsx') === true
  );
  if (!page) return;

  const scripts = new Set<string>();
  const styles = new Set<string>();
  const collect = (name: string) => {
    if (scripts.has(name)) return;
    const chunk = bundle[name] as ChunkWithCss;
    if (chunk?.type !== 'chunk') return;
    scripts.add(name);
    chunk.viteMetadata?.importedCss.forEach((css) => styles.add(css));
    chunk.imports.forEach(collect);
  };
  collect(page.fileName);

  const asset = (source: string) =>
    files.find(
      (file): file is Rolldown.OutputAsset =>
        file.type === 'asset' &&
        file.originalFileNames.some((name) =>
          name.replaceAll('\\', '/').endsWith(source)
        )
    );
  const manifest = asset('src/static/activity_chunks/manifest.json');
  const latestFile: string | undefined =
    manifest &&
    JSON.parse(
      typeof manifest.source === 'string'
        ? manifest.source
        : new TextDecoder().decode(manifest.source)
    ).years[0]?.file;
  const latestYear =
    latestFile && asset(`src/static/activity_chunks/${latestFile}`);
  const map = files.find(
    (file) =>
      file.type === 'chunk' &&
      Object.keys(file.modules).some((name) =>
        name.replaceAll('\\', '/').endsWith('/mapbox-gl/dist/mapbox-gl.js')
      )
  );

  const hints = [
    ...[manifest, latestYear].flatMap((file) =>
      file ? [['preload', file.fileName, 'fetch', 'high']] : []
    ),
    ...[...styles].map((file) => ['preload', file, 'style', 'high']),
    ...[...scripts].map((file) => ['modulepreload', file, '', 'auto']),
    ...(map ? [['modulepreload', map.fileName, '', 'low']] : []),
  ];
  // Run after the existing GitHub Pages redirect restoration in <head>. A visit
  // to /summary or a 404 must not download the homepage's map engine and data.
  return `(() => {
    const base = new URL(${JSON.stringify(base)}, location.href);
    if (![base.pathname, base.pathname + 'index.html'].includes(location.pathname)) return;
    for (const [rel, file, as, priority] of ${JSON.stringify(hints)}) {
      const href = new URL(file, base).href;
      if ([...document.querySelectorAll('link')].some(link => link.href === href)) continue;
      const link = document.createElement('link');
      link.rel = rel;
      link.href = href;
      link.crossOrigin = 'anonymous';
      if (as) link.as = as;
      link.fetchPriority = priority;
      document.head.appendChild(link);
    }
  })();`;
};

export const preloadDashboard = (): Plugin => {
  let base = '/';
  return {
    name: 'preload-dashboard',
    apply: 'build',
    configResolved(config) {
      base = config.base;
    },
    transformIndexHtml: {
      order: 'post',
      handler(html, { bundle }) {
        if (!bundle) return;
        const script = dashboardPreloadScript(bundle, base);
        // Place before Vite's stylesheets: classic scripts placed after CSS
        // wait for that CSS before executing, recreating a request waterfall.
        return html.replace(
          '<!-- dashboard-preloads -->',
          script ? `<script data-dashboard-preloads>${script}</script>` : ''
        );
      },
    },
  };
};
