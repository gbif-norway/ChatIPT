const config = {
  baseUrl: process.env.NEXT_PUBLIC_BASE_API_URL || 'http://localhost:8000',
  // Optional: expose a max upload size hint (in MB) to the UI
  // This does not enforce the limit; it only informs messages/pre-checks.
  maxUploadMB: parseInt(process.env.NEXT_PUBLIC_MAX_UPLOAD_MB || '200', 10),
};

export default config;
