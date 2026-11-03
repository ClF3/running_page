import React, { lazy, Suspense } from 'react';
import ReactDOM from 'react-dom/client';
import { RouterProvider, createBrowserRouter } from 'react-router-dom';
import { HelmetProvider } from 'react-helmet-async';
import {
  initializeGoogleAnalytics,
  USE_GOOGLE_ANALYTICS,
} from './utils/analytics';
import '@/styles/index.css';
import { withOptionalGAPageTracking } from './utils/trackRoute';
import PageLoadError from '@/components/PageLoadError';

const Index = lazy(() => import('./pages'));
const HomePage = lazy(() => import('@/pages/total'));
const NotFound = lazy(() => import('./pages/404'));

const createRouteElement = (element: React.ReactElement) =>
  withOptionalGAPageTracking(<Suspense fallback={null}>{element}</Suspense>);

if (USE_GOOGLE_ANALYTICS) {
  void initializeGoogleAnalytics();
}

const routes = createBrowserRouter(
  [
    {
      path: '/',
      element: createRouteElement(<Index />),
      errorElement: <PageLoadError />,
    },
    {
      path: 'summary',
      element: createRouteElement(<HomePage />),
      errorElement: <PageLoadError />,
    },
    {
      path: '*',
      element: createRouteElement(<NotFound />),
      errorElement: <PageLoadError />,
    },
  ],
  { basename: import.meta.env.BASE_URL }
);

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <HelmetProvider>
      <RouterProvider router={routes} />
    </HelmetProvider>
  </React.StrictMode>
);
