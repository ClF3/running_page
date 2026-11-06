const PageLoadError = () => (
  <main className="mx-auto max-w-xl px-6 py-12 text-center" role="alert">
    <h1 className="mb-3 text-xl font-semibold">Unable to load this page</h1>
    <p>Check your connection and try again.</p>
    <button
      type="button"
      className="mt-4 cursor-pointer underline"
      onClick={() => window.location.reload()}
    >
      Try again
    </button>
  </main>
);

export default PageLoadError;
